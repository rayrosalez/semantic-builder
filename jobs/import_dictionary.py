"""Load the customer's data-dictionary Delta table into the change queue.

Each dictionary row is a table description (column is NULL) or a column
description. Rows become PENDING change requests, exactly as if someone had
typed them in the app, so approvers review them (one batch per plan_id) and
the apply job lands them in its window.

  - Only the mapped columns are read. Anything else in the dictionary,
    including its source / source-tag field, is ignored on purpose.
  - Out-of-scope rows (SB_SCOPE), blank descriptions, and tables/columns that
    no longer exist are skipped and counted.
  - Rows whose trimmed text already matches what's live are skipped.
  - Idempotent: a field is skipped if a request with the same text is already
    open (PENDING/APPROVED) or was REJECTED or FAILED, so re-running on the same
    input creates nothing new and a rejected value doesn't come back daily.
  - When several dictionary rows target one field, the latest week_of wins.

--column_map maps our field names to the dictionary's column names (JSON).
--dry_run=true prints per-table counts and inserts nothing.
"""
import argparse
import json
import inspect
import os
import sys
import uuid
from collections import Counter, defaultdict

# Serverless runs the file via exec(compile(...)) with no __file__; the code
# object still carries the real path.
_HERE = os.path.dirname(os.path.abspath(inspect.currentframe().f_code.co_filename))
sys.path.insert(0, os.path.join(_HERE, "..", "app"))
from server import queue_sql  # noqa: E402

REQUESTED_BY = "dictionary-import"
CHUNK = 500
DEFAULT_MAP = {"catalog": "catalog_name", "schema": "schema_name", "table": "table_name",
               "column": "column_name", "description": "description", "plan_id": "plan_id",
               "week_of": "week_of"}
REQUIRED = ("catalog", "schema", "table", "column", "description")
# Statuses that mean "this exact text was already proposed; don't propose it again".
SEEN_STATUSES = ("PENDING", "APPROVED", "REJECTED", "FAILED")


def _s(v) -> str | None:
    return None if v is None else str(v)


def normalize(rows: list[dict], cmap: dict) -> list[dict]:
    """Dictionary rows -> {catalog, schema, table, column, description, plan_id,
    week_of}, one per field (latest week_of wins). Unmapped columns are dropped."""
    latest: dict[tuple, dict] = {}
    for raw in rows:
        e = {k: _s(raw.get(cmap[k])) if cmap.get(k) else None for k in DEFAULT_MAP}
        for k in ("catalog", "schema", "table"):
            e[k] = (e[k] or "").strip().lower()
        e["column"] = (e["column"] or "").strip() or None
        e["description"] = (e["description"] or "").strip()
        key = (e["catalog"], e["schema"], e["table"], (e["column"] or "").lower())
        prev = latest.get(key)
        if prev is None or (e["week_of"] or "") >= (prev["week_of"] or ""):
            latest[key] = e
    return list(latest.values())


def plan(entries: list[dict], scope: list[str], live_tables: dict, live_cols: dict,
         seen: set, fallback_batch: str) -> tuple[list[dict], dict]:
    """Decide which entries become requests.

    live_tables: {(catalog, schema, table): (table_type, comment)}
    live_cols:   {(catalog, schema, table): {column_lower: (column_name, comment)}}
    seen:        {(catalog, schema, table, column_lower_or_'', proposed_text)}
    """
    rows: list[dict] = []
    summary = Counter()
    per_table: dict[str, Counter] = defaultdict(Counter)
    for e in entries:
        c, s, t, col = e["catalog"], e["schema"], e["table"], e["column"]
        tkey = (c, s, t)
        name = f"{c}.{s}.{t}"
        if not c or not s or not t:
            summary["invalid"] += 1
            continue
        if not queue_sql.schema_in_scope(scope, c, s):
            summary["out_of_scope"] += 1
            continue
        if not e["description"]:
            summary["blank"] += 1
            per_table[name]["blank"] += 1
            continue
        if tkey not in live_tables:
            summary["missing_table"] += 1
            per_table[name]["missing"] += 1
            continue
        table_type, table_comment = live_tables[tkey]
        if col is None:
            live_name, live = None, table_comment
        else:
            hit = live_cols.get(tkey, {}).get(col.lower())
            if hit is None:
                summary["missing_column"] += 1
                per_table[name]["missing"] += 1
                continue
            live_name, live = hit
        proposed = e["description"]
        if proposed == (live or "").strip():
            summary["unchanged"] += 1
            per_table[name]["unchanged"] += 1
            continue
        if (c, s, t, (live_name or "").lower(), proposed) in seen:
            summary["already_queued"] += 1
            per_table[name]["already_queued"] += 1
            continue
        rows.append({
            "request_id": str(uuid.uuid4()), "batch_id": e["plan_id"] or fallback_batch,
            "catalog": c, "schema": s, "table": t, "column": live_name, "table_type": table_type,
            "previous_value": live, "proposed_value": proposed,
            "note": f"Dictionary import, week_of {e['week_of'] or 'unknown'}",
            "requested_by": REQUESTED_BY,
        })
        summary["new"] += 1
        per_table[name]["new"] += 1
    return rows, {"totals": dict(summary), "per_table": {k: dict(v) for k, v in sorted(per_table.items())}}


def chunks(rows: list, n: int = CHUNK):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


# ---------- Spark side ----------

def q(*parts: str) -> str:
    return ".".join(f"`{x.replace('`', '``')}`" for x in parts)


def load_live(spark, catalog: str, schemas: set[str]) -> tuple[dict, dict]:
    if not schemas:
        return {}, {}
    params = {f"s{i}": v for i, v in enumerate(sorted(schemas))}
    in_sql = ", ".join(":" + k for k in params)
    tables = {(catalog, r.table_schema, r.table_name): (r.table_type, r.comment) for r in spark.sql(
        f"SELECT table_schema, table_name, table_type, comment FROM {q(catalog)}.information_schema.tables "
        f"WHERE table_schema IN ({in_sql})", args=params).collect()}
    cols: dict[tuple, dict] = defaultdict(dict)
    for r in spark.sql(
            f"SELECT table_schema, table_name, column_name, comment FROM {q(catalog)}.information_schema.columns "
            f"WHERE table_schema IN ({in_sql})", args=params).collect():
        cols[(catalog, r.table_schema, r.table_name)][r.column_name.lower()] = (r.column_name, r.comment)
    return tables, cols


def load_seen(spark, queue: str) -> set:
    statuses = ", ".join(f"'{s}'" for s in SEEN_STATUSES)
    return {(r.catalog_name, r.schema_name, r.table_name, (r.column_name or "").lower(),
             (r.proposed_value or "").strip())
            for r in spark.sql(
                f"SELECT catalog_name, schema_name, table_name, column_name, proposed_value FROM {queue} "
                f"WHERE status IN ({statuses})").collect()}


def run(spark, args) -> dict:
    if not args.dictionary_table.strip():
        print("No dictionary_table configured; nothing to import.")
        return {}
    cmap = {**DEFAULT_MAP, **json.loads(args.column_map or "{}")}
    missing = [k for k in REQUIRED if not cmap.get(k)]
    if missing:
        raise ValueError(f"column_map is missing {missing}")
    scope = queue_sql.parse_scope(args.scope)
    queue = f"{q(args.staging_catalog, args.staging_schema)}.`metadata_change_requests`"

    wanted = [v for k, v in cmap.items() if v]
    df = spark.table(args.dictionary_table)
    absent = [c for c in wanted if c not in df.columns]
    for k in REQUIRED:
        if cmap[k] in absent:
            raise ValueError(f"{args.dictionary_table} has no column {cmap[k]!r} (mapped from {k!r})")
    present = [c for c in wanted if c not in absent]
    entries = normalize([r.asDict() for r in df.select(*present).collect()], cmap)

    by_catalog: dict[str, set[str]] = defaultdict(set)
    for e in entries:
        if e["catalog"] and queue_sql.schema_in_scope(scope, e["catalog"], e["schema"]):
            by_catalog[e["catalog"]].add(e["schema"])
    live_tables, live_cols = {}, {}
    for catalog, schemas in by_catalog.items():
        t, c = load_live(spark, catalog, schemas)
        live_tables.update(t)
        live_cols.update(c)

    rows, report = plan(entries, scope, live_tables, live_cols, load_seen(spark, queue), str(uuid.uuid4()))
    print(f"{len(entries)} dictionary field(s) read from {args.dictionary_table}")
    for name, counts in report["per_table"].items():
        print(f"  {name}: {counts}")
    print("Totals:", report["totals"])

    if flag(args.dry_run):
        print(f"[dry run] would queue {len(rows)} request(s); nothing written")
        return report
    for chunk in chunks(rows):
        for statement, params in queue_sql.submit_statements(queue, chunk):
            spark.sql(statement, args=params)
    print(f"Queued {len(rows)} request(s)")
    return report


def flag(v: str) -> bool:
    return str(v).strip().lower() == "true"


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--staging_catalog", required=True)
    p.add_argument("--staging_schema", required=True)
    p.add_argument("--dictionary_table", default="")
    p.add_argument("--column_map", default="")
    p.add_argument("--scope", default="")
    p.add_argument("--dry_run", default="false")
    return p.parse_args(argv)


if __name__ == "__main__":
    from pyspark.sql import SparkSession

    run(SparkSession.builder.getOrCreate(), parse_args())
