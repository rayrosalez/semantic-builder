"""Apply approved metadata changes to Unity Catalog without disrupting writers.

Why this exists: every COMMENT / ALTER COLUMN on a Delta table is a metadata
commit to the table's transaction log. A job or pipeline that commits to the
same table at the same moment can fail with MetadataChangedException. So:

  1. Collect APPROVED requests, grouped by table.
  2. Find each table's writers (jobs + pipelines) from system.access.table_lineage
     and skip (defer) tables that have a writer running right now — unless an
     approver set force_apply on the request.
  3. Apply everything for a table in as few commits as possible: one
     ALTER TABLE ... ALTER COLUMN a COMMENT .., b COMMENT .. for all columns,
     plus one COMMENT ON TABLE. Retry on commit conflicts.
  4. Write the outcome back to the queue (APPLIED / FAILED / deferral note).

Materialized views and streaming tables: comments apply and survive REFRESH,
but a pipeline that redefines the object (CREATE OR REPLACE / redeploy) resets
them to whatever its source code says. Those requests are applied with a
warning so the owner can copy the text into the pipeline definition; the app
flags any later reversion as drift.
"""
import argparse
import time
from collections import defaultdict

from databricks.sdk import WorkspaceClient
from pyspark.sql import SparkSession

p = argparse.ArgumentParser()
p.add_argument("--staging_catalog", required=True)
p.add_argument("--staging_schema", required=True)
p.add_argument("--check_writers", default="true")
p.add_argument("--lineage_lookback_days", default="30")
p.add_argument("--self_job_id", default="")
args = p.parse_args()

spark = SparkSession.builder.getOrCreate()
w = WorkspaceClient()
QUEUE = f"`{args.staging_catalog}`.`{args.staging_schema}`.`metadata_change_requests`"
CHECK_WRITERS = args.check_writers.lower() == "true"
LOOKBACK = int(args.lineage_lookback_days)
SELF_JOB_ID = args.self_job_id.strip()

DELTA_TYPES = {"MANAGED", "EXTERNAL"}
PIPELINE_TYPES = {"MATERIALIZED_VIEW", "STREAMING_TABLE"}
CONFLICT_MARKERS = ("Concurrent", "MetadataChanged", "DELTA_CONCURRENT", "ProtocolChanged")
ACTIVE_PIPELINE_UPDATE_STATES = {"QUEUED", "CREATED", "WAITING_FOR_RESOURCES", "INITIALIZING",
                                 "RESETTING", "SETTING_UP_TABLES", "RUNNING", "STOPPING"}


def lit(s: str | None) -> str:
    """SQL string literal (DDL comments can't take parameter markers)."""
    if s is None:
        return "NULL"
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def q(*parts: str) -> str:
    return ".".join(f"`{x.replace('`', '``')}`" for x in parts)


def update(ids: list[str], status: str | None, message: str, *, applied=False,
           attempt=False, deferred=False) -> None:
    if not ids:
        return
    sets = ["apply_message = :msg", "last_attempt_at = current_timestamp()",
            "updated_at = current_timestamp()"]
    if status:
        sets.append(f"status = '{status}'")
    if applied:
        sets.append("applied_at = current_timestamp()")
    if attempt:
        sets.append("apply_attempts = coalesce(apply_attempts, 0) + 1")
    if deferred:
        sets.append("deferrals = coalesce(deferrals, 0) + 1")
    params = {f"id{i}": v for i, v in enumerate(ids)}
    params["msg"] = message[:2000]
    spark.sql(
        f"UPDATE {QUEUE} SET {', '.join(sets)} "
        f"WHERE request_id IN ({', '.join(':' + k for k in params if k != 'msg')}) "
        "AND status = 'APPROVED'",
        args=params,
    )


# ---------- 1. approved work, newest request per field ----------

rows = spark.sql(f"""
    SELECT * FROM (
      SELECT *, row_number() OVER (
        PARTITION BY catalog_name, schema_name, table_name, coalesce(column_name, '<table>'), field
        ORDER BY requested_at DESC) AS rn
      FROM {QUEUE} WHERE status = 'APPROVED'
    )
""").collect()

stale = [r.request_id for r in rows if r.rn > 1]
if stale:
    spark.sql(
        f"UPDATE {QUEUE} SET status = 'SUPERSEDED', updated_at = current_timestamp(), "
        "apply_message = 'Superseded by a newer approved request' "
        f"WHERE request_id IN ({', '.join(lit(x) for x in stale)})"
    )

by_table: dict[tuple, list] = defaultdict(list)
for r in rows:
    if r.rn == 1:
        by_table[(r.catalog_name, r.schema_name, r.table_name)].append(r)

print(f"{sum(len(v) for v in by_table.values())} approved change(s) across {len(by_table)} table(s)")


# ---------- 2. who writes these tables, and is any of it running? ----------

writers: dict[str, set[tuple[str, str]]] = defaultdict(set)
if CHECK_WRITERS and by_table:
    names = [f"{c}.{s}.{t}" for c, s, t in by_table]
    try:
        lineage = spark.sql(
            f"""SELECT DISTINCT target_table_full_name AS t, entity_type, entity_id
                FROM system.access.table_lineage
                WHERE event_date >= date_sub(current_date(), {LOOKBACK})
                  AND entity_type IN ('JOB', 'PIPELINE')
                  AND target_table_full_name IN ({', '.join(lit(n) for n in names)})"""
        ).collect()
        for r in lineage:
            writers[r.t].add((r.entity_type, r.entity_id))
    except Exception as e:  # lineage not enabled / not granted: Delta history below still runs
        print("Lineage lookup unavailable:", str(e).splitlines()[0][:200])

    # Delta history names the job behind every recent commit. It has no lag and
    # also catches writes lineage misses (e.g. inserts with no source table).
    for c, s, t in by_table:
        try:
            for r in spark.sql(
                f"""SELECT DISTINCT CAST(job.jobId AS STRING) AS job_id
                    FROM (DESCRIBE HISTORY {q(c, s, t)})
                    WHERE timestamp >= current_timestamp() - INTERVAL {LOOKBACK} DAYS
                      AND job.jobId IS NOT NULL"""
            ).collect():
                writers[f"{c}.{s}.{t}"].add(("JOB", r.job_id))
        except Exception:  # views and some pipeline-managed objects have no Delta history
            pass

    # The apply job's own COMMENT commits show up in history; never wait on ourselves.
    for k in writers:
        writers[k].discard(("JOB", SELF_JOB_ID))

_activity: dict[tuple, str | None] = {}


def running(kind: str, entity_id: str) -> str | None:
    """A human-readable reason if the writer is active right now, else None."""
    key = (kind, entity_id)
    if key in _activity:
        return _activity[key]
    reason = None
    try:
        if kind == "JOB":
            for run in w.jobs.list_runs(job_id=int(entity_id), active_only=True):
                reason = f"job {entity_id} ({run.run_name or 'run'}) is running"
                break
        else:
            pl = w.pipelines.get(entity_id)
            latest = (pl.latest_updates or [None])[0]
            state = latest.state.value if latest and latest.state else None
            if state in ACTIVE_PIPELINE_UPDATE_STATES:
                reason = f"pipeline {pl.name or entity_id} is {state.lower()}"
    except Exception as e:
        print(f"  could not check {kind} {entity_id}: {str(e).splitlines()[0][:150]}")
    _activity[key] = reason
    return reason


# ---------- 3. apply ----------

def run_with_retry(statement: str, tries: int = 4) -> None:
    for attempt in range(tries):
        try:
            spark.sql(statement)
            return
        except Exception as e:
            if attempt < tries - 1 and any(m in str(e) for m in CONFLICT_MARKERS):
                time.sleep(10 * (attempt + 1))
                continue
            raise


def main() -> None:
    summary = defaultdict(int)
    for (catalog, schema, table), reqs in by_table.items():
        full = f"{catalog}.{schema}.{table}"
        target = q(catalog, schema, table)
        print(f"- {full}: {len(reqs)} change(s)")

        busy = [r for kind, eid in writers.get(full, ()) if (r := running(kind, eid))]
        if busy:
            waiting = [r for r in reqs if not r.force_apply]
            reqs = [r for r in reqs if r.force_apply]
            if waiting:
                update([r.request_id for r in waiting], None,
                       "Waiting: " + "; ".join(busy) + ". Will retry next run.", deferred=True)
                summary["deferred"] += len(waiting)
                print("  deferred:", "; ".join(busy))
            if not reqs:
                continue

        info = spark.sql(
            f"SELECT table_type, comment FROM {q(catalog)}.information_schema.tables "
            "WHERE table_schema = :s AND table_name = :t", args={"s": schema, "t": table}).collect()
        if not info:
            update([r.request_id for r in reqs], "FAILED", "Table no longer exists or is not visible",
                   attempt=True)
            summary["failed"] += len(reqs)
            continue
        table_type = info[0].table_type
        live = {None: info[0].comment}
        for c in spark.sql(
                f"SELECT column_name, comment FROM {q(catalog)}.information_schema.columns "
                "WHERE table_schema = :s AND table_name = :t", args={"s": schema, "t": table}).collect():
            live[c.column_name] = c.comment

        notes = {}
        for r in reqs:
            if (live.get(r.column_name) or "") != (r.previous_value or ""):
                notes[r.request_id] = "Note: the description was edited outside Semantic Builder after this request."
        if table_type in PIPELINE_TYPES:
            warn = (f"Applied to a {table_type.lower().replace('_', ' ')}. Add this text to the pipeline "
                    "definition too, or the next redeploy resets it.")
            for r in reqs:
                notes[r.request_id] = (notes.get(r.request_id, "") + " " + warn).strip()

        col_reqs = [r for r in reqs if r.column_name and r.column_name in live]
        tbl_reqs = [r for r in reqs if r.column_name is None]
        missing = [r for r in reqs if r.column_name and r.column_name not in live]

        steps: list[tuple[list, list[str]]] = []
        if col_reqs:
            if table_type in DELTA_TYPES:
                clauses = ", ".join(f"`{r.column_name}` COMMENT {lit(r.proposed_value)}" for r in col_reqs)
                steps.append((col_reqs, [f"ALTER TABLE {target} ALTER COLUMN {clauses}"]))
            else:
                steps.append((col_reqs, [
                    f"COMMENT ON COLUMN {target}.`{r.column_name}` IS {lit(r.proposed_value)}"
                    for r in col_reqs]))
        for r in tbl_reqs:
            steps.append(([r], [f"COMMENT ON TABLE {target} IS {lit(r.proposed_value)}"]))

        for group, statements in steps:
            try:
                for st in statements:
                    run_with_retry(st)
            except Exception as e:
                msg = str(e).splitlines()[0][:500]
                update([r.request_id for r in group], "FAILED", f"Apply failed: {msg}", attempt=True)
                summary["failed"] += len(group)
                print("  FAILED:", msg)
                continue
            for r in group:
                update([r.request_id], "APPLIED", notes.get(r.request_id, "Applied"), applied=True)
            summary["applied"] += len(group)

        if missing:
            update([r.request_id for r in missing], "FAILED", "Column no longer exists on the table",
                   attempt=True)
            summary["failed"] += len(missing)

    print("Done:", dict(summary))


if by_table:
    main()
else:
    print("Nothing to apply")
