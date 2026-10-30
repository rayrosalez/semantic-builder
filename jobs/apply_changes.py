"""Apply approved metadata changes to Unity Catalog without disrupting writers.

Why this exists: every COMMENT / ALTER COLUMN on a Delta table is a metadata
commit to the table's transaction log. A job or pipeline that commits to the
same table at the same moment can fail with MetadataChangedException.

Two modes (--mode):

  defer (default)  For each table, find its writers (jobs + pipelines) from
                   system.access.table_lineage and DESCRIBE HISTORY, and skip
                   (defer) the table while any of them is running, unless an
                   approver set force_apply. Fine when writers have gaps.

  window           A maintenance window for writers that are never idle:
                   pause the writer jobs (recording their original state in
                   paused_writers first), wait up to --max_wait_minutes for
                   runs already in flight, apply, then restore every job to
                   exactly how it was, in a finally block. Running jobs are
                   never cancelled; tables whose writers are still running at
                   the deadline are deferred to the next window. The job's
                   restore_writers task (run_if ALL_DONE) is the backstop if
                   this process dies mid-window.

Either way, everything for a table is applied in as few commits as possible
(one ALTER TABLE ... ALTER COLUMN a COMMENT .., b COMMENT .. plus one COMMENT ON
TABLE), retried on commit conflicts, and the outcome written back to the queue.

--dry_run=true prints the planned DDL, the jobs/pipelines that would be paused
and the tables that would be deferred, and changes nothing.

Materialized views and streaming tables: comments apply and survive REFRESH,
but a pipeline that redefines the object (CREATE OR REPLACE / redeploy) resets
them to whatever its source code says. Those requests are applied with a
warning so the owner can copy the text into the pipeline definition; the app
flags any later reversion as drift.
"""
import argparse
import inspect
import os
import sys
import time
import uuid
from collections import defaultdict

# Serverless runs the file via exec(compile(...)) with no __file__; the code
# object still carries the real path.
sys.path.insert(0, os.path.dirname(os.path.abspath(inspect.currentframe().f_code.co_filename)))
import writers as wr  # noqa: E402

DELTA_TYPES = {"MANAGED", "EXTERNAL"}
PIPELINE_TYPES = {"MATERIALIZED_VIEW", "STREAMING_TABLE"}
CONFLICT_MARKERS = ("Concurrent", "MetadataChanged", "DELTA_CONCURRENT", "ProtocolChanged")
POLL_SECONDS = 30


def lit(s: str | None) -> str:
    """SQL string literal (DDL comments can't take parameter markers). Databricks
    SQL unescapes backslash sequences in literals, so backslashes, quotes and
    control characters are all written as escapes."""
    if s is None:
        return "NULL"
    out = (s.replace("\\", "\\\\").replace("'", "\\'")
           .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return "'" + out + "'"


def q(*parts: str) -> str:
    return ".".join(f"`{x.replace('`', '``')}`" for x in parts)


def flag(v: str) -> bool:
    return str(v).strip().lower() == "true"


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--staging_catalog", required=True)
    p.add_argument("--staging_schema", required=True)
    p.add_argument("--check_writers", default="true")
    p.add_argument("--lineage_lookback_days", default="30")
    p.add_argument("--self_job_id", default="")
    p.add_argument("--run_id", default="")
    p.add_argument("--mode", default="defer", choices=["defer", "window"])
    p.add_argument("--max_wait_minutes", default="30")
    p.add_argument("--dry_run", default="false")
    p.add_argument("--pause_pipelines", default="false")
    return p.parse_args(argv)


class Applier:
    def __init__(self, spark, w, args, ledger=None, sleep=time.sleep, clock=time.monotonic):
        self.spark, self.w, self.args = spark, w, args
        self.sleep, self.clock = sleep, clock
        self.queue = f"{q(args.staging_catalog, args.staging_schema)}.`metadata_change_requests`"
        self.ledger = ledger or wr.SparkLedger(
            spark, f"{q(args.staging_catalog, args.staging_schema)}.`paused_writers`")
        self.check_writers = flag(args.check_writers)
        self.lookback = int(args.lineage_lookback_days)
        self.self_job_id = str(args.self_job_id).strip()
        self.run_id = str(args.run_id).strip() or f"manual-{uuid.uuid4()}"
        self.window = args.mode == "window"
        self.dry = flag(args.dry_run)
        self.pause_pipelines = flag(args.pause_pipelines)
        self.max_wait = float(args.max_wait_minutes) * 60
        self.summary = defaultdict(int)
        self._activity: dict[tuple, str | None] = {}

    # ---------- sql helpers ----------

    def read(self, statement: str, params: dict | None = None) -> list:
        return (self.spark.sql(statement, args=params) if params else self.spark.sql(statement)).collect()

    def write(self, statement: str, params: dict | None = None) -> None:
        """Queue/DDL writes. In a dry run they're printed instead."""
        if self.dry:
            print("  [dry run] would run:", " ".join(statement.split())[:400])
            return
        self.spark.sql(statement, args=params) if params else self.spark.sql(statement)

    def update(self, ids: list[str], status: str | None, message: str, *, applied=False,
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
        markers = ", ".join(":" + k for k in params)
        params["msg"] = message[:2000]
        self.write(f"UPDATE {self.queue} SET {', '.join(sets)} "
                   f"WHERE request_id IN ({markers}) AND status = 'APPROVED'", params)

    # ---------- 1. approved work, newest request per field ----------

    def load_approved(self) -> dict[tuple, list]:
        rows = self.read(f"""
            SELECT * FROM (
              SELECT *, row_number() OVER (
                PARTITION BY catalog_name, schema_name, table_name, coalesce(column_name, '<table>'), field
                ORDER BY requested_at DESC) AS rn
              FROM {self.queue} WHERE status = 'APPROVED'
            )
        """)
        stale = [r.request_id for r in rows if r.rn > 1]
        if stale:
            params = {f"s{i}": v for i, v in enumerate(stale)}
            self.write(
                f"UPDATE {self.queue} SET status = 'SUPERSEDED', updated_at = current_timestamp(), "
                "apply_message = 'Superseded by a newer approved request' "
                f"WHERE request_id IN ({', '.join(':' + k for k in params)})", params)
        by_table: dict[tuple, list] = defaultdict(list)
        for r in rows:
            if r.rn == 1:
                by_table[(r.catalog_name, r.schema_name, r.table_name)].append(r)
        return by_table

    # ---------- 2. who writes these tables, and is any of it running? ----------

    def find_writers(self, tables) -> dict[str, set[tuple[str, str]]]:
        writers: dict[str, set[tuple[str, str]]] = defaultdict(set)
        if not self.check_writers or not tables:
            return writers
        names = [f"{c}.{s}.{t}" for c, s, t in tables]
        try:
            params = {f"n{i}": n for i, n in enumerate(names)}
            for r in self.read(
                f"""SELECT DISTINCT target_table_full_name AS t, entity_type, entity_id
                    FROM system.access.table_lineage
                    WHERE event_date >= date_sub(current_date(), {self.lookback})
                      AND entity_type IN ('JOB', 'PIPELINE')
                      AND target_table_full_name IN ({', '.join(':' + k for k in params)})""", params):
                writers[r.t].add((r.entity_type, str(r.entity_id)))
        except Exception as e:  # lineage not enabled / not granted: Delta history below still runs
            print("Lineage lookup unavailable:", str(e).splitlines()[0][:200])

        # Delta history names the job behind every recent commit. It has no lag and
        # also catches writes lineage misses (e.g. inserts with no source table).
        for c, s, t in tables:
            try:
                for r in self.read(
                    f"""SELECT DISTINCT CAST(job.jobId AS STRING) AS job_id
                        FROM (DESCRIBE HISTORY {q(c, s, t)})
                        WHERE timestamp >= current_timestamp() - INTERVAL {self.lookback} DAYS
                          AND job.jobId IS NOT NULL"""):
                    writers[f"{c}.{s}.{t}"].add(("JOB", str(r.job_id)))
            except Exception:  # views and some pipeline-managed objects have no Delta history
                pass

        # The apply job's own COMMENT commits show up in history; never wait on ourselves.
        for k in writers:
            writers[k].discard(("JOB", self.self_job_id))
        return writers

    def activity(self, kind: str, entity_id: str, fresh: bool = False) -> str | None:
        """A human-readable reason if the writer is active right now, else None.
        In window mode a writer we can't check counts as busy."""
        key = (kind, entity_id)
        if not fresh and key in self._activity:
            return self._activity[key]
        reason = None
        try:
            if kind == "JOB":
                for run in self.w.jobs.list_runs(job_id=int(entity_id), active_only=True):
                    reason = f"job {entity_id} ({run.run_name or 'run'}) is running"
                    break
            else:
                pl = self.w.pipelines.get(entity_id)
                if wr.pipeline_running(pl):
                    reason = f"pipeline {pl.name or entity_id} is running"
        except Exception as e:
            msg = str(e).splitlines()[0][:150]
            print(f"  could not check {kind} {entity_id}: {msg}")
            if self.window:
                reason = f"could not check {kind.lower()} {entity_id} ({msg})"
        self._activity[key] = reason
        return reason

    # ---------- 3. apply one table ----------

    def run_with_retry(self, statement: str, tries: int = 4) -> None:
        if self.dry:
            print("  [dry run] DDL:", statement)
            return
        for attempt in range(tries):
            try:
                self.spark.sql(statement)
                return
            except Exception as e:
                if attempt < tries - 1 and any(m in str(e) for m in CONFLICT_MARKERS):
                    self.sleep(10 * (attempt + 1))
                    continue
                raise

    def defer(self, reqs: list, reason: str) -> list:
        """Defer the non-forced requests; return the forced ones (still to apply)."""
        waiting = [r for r in reqs if not r.force_apply]
        if waiting:
            self.update([r.request_id for r in waiting], None, reason, deferred=True)
            self.summary["deferred"] += len(waiting)
            print("  deferred:", reason)
        return [r for r in reqs if r.force_apply]

    def apply_table(self, key: tuple, reqs: list, applied_message: str = "Applied") -> None:
        catalog, schema, table = key
        target = q(catalog, schema, table)
        info = self.read(
            f"SELECT table_type, comment FROM {q(catalog)}.information_schema.tables "
            "WHERE table_schema = :s AND table_name = :t", {"s": schema, "t": table})
        if not info:
            self.update([r.request_id for r in reqs], "FAILED", "Table no longer exists or is not visible",
                        attempt=True)
            self.summary["failed"] += len(reqs)
            return
        table_type = info[0].table_type
        live = {None: info[0].comment}
        for c in self.read(
                f"SELECT column_name, comment FROM {q(catalog)}.information_schema.columns "
                "WHERE table_schema = :s AND table_name = :t", {"s": schema, "t": table}):
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
                clauses = ", ".join(f"{q(r.column_name)} COMMENT {lit(r.proposed_value)}" for r in col_reqs)
                steps.append((col_reqs, [f"ALTER TABLE {target} ALTER COLUMN {clauses}"]))
            else:
                steps.append((col_reqs, [
                    f"COMMENT ON COLUMN {target}.{q(r.column_name)} IS {lit(r.proposed_value)}"
                    for r in col_reqs]))
        for r in tbl_reqs:
            steps.append(([r], [f"COMMENT ON TABLE {target} IS {lit(r.proposed_value)}"]))

        for group, statements in steps:
            try:
                for st in statements:
                    self.run_with_retry(st)
            except Exception as e:
                msg = str(e).splitlines()[0][:500]
                self.update([r.request_id for r in group], "FAILED", f"Apply failed: {msg}", attempt=True)
                self.summary["failed"] += len(group)
                print("  FAILED:", msg)
                continue
            for r in group:
                note = notes.get(r.request_id)
                if applied_message == "Applied":
                    message = note or "Applied"
                else:
                    message = f"{applied_message}. {note}" if note else applied_message
                self.update([r.request_id], "APPLIED", message, applied=True)
            self.summary["applied"] += len(group)

        if missing:
            self.update([r.request_id for r in missing], "FAILED", "Column no longer exists on the table",
                        attempt=True)
            self.summary["failed"] += len(missing)

    def apply_safely(self, key: tuple, reqs: list, applied_message: str = "Applied") -> None:
        """One table's failure (e.g. its catalog became unreadable) must not
        stop the rest of the queue."""
        try:
            self.apply_table(key, reqs, applied_message)
        except Exception as e:
            msg = str(e).splitlines()[0][:500]
            print(f"  FAILED {'.'.join(key)}: {msg}")
            self.update([r.request_id for r in reqs], "FAILED", f"Apply failed: {msg}", attempt=True)
            self.summary["failed"] += len(reqs)

    # ---------- defer mode ----------

    def run_defer(self, by_table: dict, writers: dict) -> None:
        for key, reqs in by_table.items():
            full = ".".join(key)
            print(f"- {full}: {len(reqs)} change(s)")
            busy = [r for kind, eid in sorted(writers.get(full, ())) if (r := self.activity(kind, eid))]
            if busy:
                reqs = self.defer(reqs, "Waiting: " + "; ".join(busy) + ". Will retry next run.")
                if not reqs:
                    continue
            self.apply_safely(key, reqs)

    # ---------- window mode ----------

    def _pipeline_jobs(self, pipeline_ids: set[str]) -> dict[str, set[str]]:
        """pipeline id -> ids of jobs that run it (pausing those covers the pipeline)."""
        out: dict[str, set[str]] = defaultdict(set)
        for job in self.w.jobs.list(expand_tasks=True):
            for task in (job.settings.tasks or []) if job.settings else []:
                pid = task.pipeline_task.pipeline_id if task.pipeline_task else None
                if pid in pipeline_ids:
                    out[pid].add(str(job.job_id))
        return out

    def drop_deleted(self, writers: dict) -> None:
        """Delta history keeps naming jobs for 30 days after they're deleted. A
        deleted job can't run, so it isn't a writer; only a job we can see but
        not manage (or check) holds its tables back."""
        from databricks.sdk.errors import NotFound

        gone = set()
        for kind, eid in sorted({w_ for ws in writers.values() for w_ in ws}):
            try:
                self.w.jobs.get(int(eid)) if kind == "JOB" else self.w.pipelines.get(eid)
            except NotFound:
                gone.add((kind, eid))
                print(f"  {kind.lower()} {eid} no longer exists; not a writer")
            except Exception:
                pass  # handled where it matters (pause / activity check)
        for ws in writers.values():
            ws -= gone

    def run_window(self, by_table: dict, writers: dict) -> None:
        blocked: dict[str, str] = {}  # table -> deferral reason (forced requests still apply)
        self.drop_deleted(writers)

        # Pipelines: triggered ones are covered by pausing the jobs that run them;
        # continuous ones must be stopped, which is opt-in.
        pipeline_ids = {eid for ws in writers.values() for kind, eid in ws if kind == "PIPELINE"}
        pipelines, continuous = {}, set()
        for pid in sorted(pipeline_ids):
            try:
                pipelines[pid] = self.w.pipelines.get(pid)
                if pipelines[pid].spec and pipelines[pid].spec.continuous:
                    continuous.add(pid)
            except Exception as e:
                print(f"  could not read pipeline {pid}: {str(e).splitlines()[0][:150]}")
        triggered = pipeline_ids - continuous
        if triggered:
            for pid, job_ids in self._pipeline_jobs(triggered).items():
                for ws in writers.values():
                    if ("PIPELINE", pid) in ws:
                        ws.update(("JOB", j) for j in job_ids if j != self.self_job_id)
        if continuous and not self.pause_pipelines:
            for full, ws in writers.items():
                for pid in sorted(continuous):
                    if ("PIPELINE", pid) in ws:
                        name = pipelines[pid].name or pid
                        blocked[full] = (f"Waiting: continuous pipeline {name} must be stopped manually "
                                         "or enable pause_pipelines")

        def live_tables():
            return [k for k, reqs in by_table.items()
                    if ".".join(k) not in blocked or any(r.force_apply for r in reqs)]

        active = live_tables()
        jobs = sorted({eid for k in active for kind, eid in writers.get(".".join(k), ()) if kind == "JOB"})
        to_stop = sorted({eid for k in active for kind, eid in writers.get(".".join(k), ())
                          if kind == "PIPELINE" and eid in continuous}) if self.pause_pipelines else []

        if self.dry:
            self._print_plan(by_table, writers, blocked, jobs, to_stop)
            for key in live_tables():
                print(f"- {'.'.join(key)}: {len(by_table[key])} change(s)")
                self.apply_safely(key, by_table[key], "Applied in maintenance window")
            return

        paused: dict[str, list[str]] = {}
        try:
            for job_id in jobs:
                try:
                    kinds = wr.pause_job(self.w, self.ledger, self.run_id, job_id)
                    if kinds:
                        paused[job_id] = kinds
                        print(f"  paused job {job_id} ({', '.join(kinds)})")
                except Exception as e:
                    msg = str(e).splitlines()[0][:150]
                    print(f"  could not pause job {job_id}: {msg}")
                    for full, ws in writers.items():
                        if ("JOB", job_id) in ws:
                            blocked[full] = f"Waiting: could not pause job {job_id} ({msg}). Will retry next window."
            for pid in to_stop:
                try:
                    if wr.stop_pipeline(self.w, self.ledger, self.run_id, pid):
                        print(f"  stopped continuous pipeline {pid}")
                except Exception as e:
                    msg = str(e).splitlines()[0][:150]
                    for full, ws in writers.items():
                        if ("PIPELINE", pid) in ws:
                            blocked[full] = f"Waiting: could not stop pipeline {pid} ({msg}). Will retry next window."

            # Wait for runs already in flight. Never cancel them.
            watch = sorted({w_ for k in live_tables() for w_ in writers.get(".".join(k), ())})
            deadline = self.clock() + self.max_wait
            busy: dict[tuple, str] = {}
            while True:
                busy = {w_: r for w_ in watch if (r := self.activity(*w_, fresh=True))}
                if not busy or self.clock() >= deadline:
                    break
                print(f"  waiting on {len(busy)} writer(s): " + "; ".join(busy.values()))
                self.sleep(POLL_SECONDS)
            minutes = int(self.max_wait // 60)
            for full, ws in writers.items():
                still = [busy[w_] for w_ in sorted(ws) if w_ in busy]
                if still and full not in blocked:
                    blocked[full] = (f"Waiting: {'; '.join(still)} after {minutes} min. "
                                     "Will retry next window.")

            for key, reqs in by_table.items():
                full = ".".join(key)
                print(f"- {full}: {len(reqs)} change(s)")
                if full not in blocked:  # last look right before the commit
                    now_busy = [r for w_ in sorted(writers.get(full, ())) if (r := self.activity(*w_, fresh=True))]
                    if now_busy:
                        blocked[full] = "Waiting: " + "; ".join(now_busy) + ". Will retry next window."
                if full in blocked:
                    reqs = self.defer(reqs, blocked[full])
                    if not reqs:
                        continue
                mine = sorted((j for kind, j in writers.get(full, ()) if kind == "JOB" and j in paused), key=int)
                message = ("Applied in maintenance window; " +
                           (f"paused jobs {', '.join(mine)}" if mine else "no writer jobs needed pausing"))
                self.apply_safely(key, reqs, message)
        finally:
            restored, errors = wr.restore(self.w, self.ledger, self.run_id)
            for line in restored:
                print("  restored", line)
            if errors:
                raise RuntimeError("Could not restore: " + "; ".join(errors) +
                                   " (the restore_writers task will retry)")

    def _print_plan(self, by_table, writers, blocked, jobs, to_stop) -> None:
        print("[dry run] maintenance window plan")
        for job_id in jobs:
            try:
                state = wr.job_pause_state(self.w, job_id)
            except Exception as e:
                state = {"error": str(e).splitlines()[0][:100]}
            would = [k for k, v in state.items() if v != "PAUSED" and k != "error"]
            print(f"  job {job_id}: {state or 'no schedule/trigger'} -> would pause {would or 'nothing'}")
        for pid in to_stop:
            print(f"  continuous pipeline {pid}: would stop and restart afterwards")
        for w_ in sorted({w_ for ws in writers.values() for w_ in ws}):
            if r := self.activity(*w_, fresh=True):
                print(f"  running now (would wait up to {int(self.max_wait // 60)} min): {r}")
        for full, reason in sorted(blocked.items()):
            print(f"  would defer {full}: {reason}")

    # ---------- entry point ----------

    def run(self) -> dict:
        by_table = self.load_approved()
        n = sum(len(v) for v in by_table.values())
        print(f"{n} approved change(s) across {len(by_table)} table(s)"
              f" [mode={self.args.mode}{', dry run' if self.dry else ''}]")
        if not by_table:  # e.g. the second 1:00 AM on a DST fall-back Sunday
            print("Nothing to apply")
            return dict(self.summary)
        writers = self.find_writers(list(by_table))
        if self.window:
            self.run_window(by_table, writers)
        else:
            self.run_defer(by_table, writers)
        print("Done:", dict(self.summary))
        return dict(self.summary)


def main(argv=None) -> None:
    from databricks.sdk import WorkspaceClient
    from pyspark.sql import SparkSession

    Applier(SparkSession.builder.getOrCreate(), WorkspaceClient(), parse_args(argv)).run()


if __name__ == "__main__":
    main()
