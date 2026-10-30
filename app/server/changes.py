"""Write side: the change-request queue (a Delta table).

Lifecycle:  PENDING -> APPROVED -> APPLIED
            PENDING -> REJECTED;  APPROVED -> FAILED (an approver can retry)
            PENDING -> WITHDRAWN (by the requester)
            PENDING/APPROVED -> SUPERSEDED (a newer request for the same field)

The app never runs DDL on business tables. It only writes requests; the apply
job (jobs/apply_changes.py) is the single writer of real metadata.
"""
import uuid

from . import catalog as cat
from . import queue_sql, sql
from .config import ALLOW_SELF_APPROVAL, STAGING_TABLE, schema_in_scope

OPEN = queue_sql.OPEN

_COLUMNS = queue_sql.COLUMNS


def _in_list(prefix: str, values: list[str]) -> tuple[str, dict]:
    names = [f"{prefix}{i}" for i in range(len(values))]
    return ", ".join(f":{n}" for n in names), dict(zip(names, values))


def submit(user: str, changes: list[dict], note: str | None) -> dict:
    """Queue edits for one table. Unchanged values are dropped; any open request
    for the same field is superseded so only the newest proposal is live."""
    if not changes:
        return {"submitted": 0, "request_ids": []}
    tables = {(c["catalog"], c["schema"], c["table"]) for c in changes}
    if len(tables) != 1:
        raise ValueError("Submit changes one table at a time")
    catalog, schema, table = tables.pop()
    if not schema_in_scope(catalog, schema):
        raise PermissionError(f"{catalog}.{schema} is outside this app's scope")

    detail = cat.table_detail(catalog, schema, table)
    live = {None: detail["comment"], **{c["column_name"]: c["comment"] for c in detail["columns"]}}

    batch_id = str(uuid.uuid4())
    note = (note or "").strip() or None
    rows = []
    for ch in changes:
        column = ch.get("column") or None
        if column not in live:
            raise LookupError(f"Column {column} not found on {table}")
        proposed = (ch.get("proposed_value") or "").strip()
        if proposed == (live[column] or "").strip():
            continue
        rows.append({
            "request_id": str(uuid.uuid4()), "batch_id": batch_id,
            "catalog": catalog, "schema": schema, "table": table, "column": column,
            "table_type": detail["table_type"], "previous_value": live[column],
            "proposed_value": proposed, "note": note, "requested_by": user,
        })
    if not rows:
        return {"submitted": 0, "request_ids": []}

    for statement, params in queue_sql.submit_statements(STAGING_TABLE, rows):
        sql.query(statement, params)
    cat.invalidate("open_counts")
    return {"submitted": len(rows), "batch_id": batch_id, "request_ids": [r["request_id"] for r in rows]}


def review(user: str, ids: list[str], decision: str, note: str | None) -> dict:
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    if not ids:
        return {"updated": 0, "skipped_self": 0}
    in_sql, params = _in_list("id", ids)
    params.update({"user": user, "note": (note or "").strip() or None})
    self_guard = "" if ALLOW_SELF_APPROVAL else "AND requested_by <> :user"

    skipped = 0
    if not ALLOW_SELF_APPROVAL:
        skipped = sql.query(f"""
            SELECT count(*) AS n FROM {STAGING_TABLE}
            WHERE request_id IN ({in_sql}) AND status = 'PENDING' AND requested_by = :user
        """, params)[0]["n"]

    status = "APPROVED" if decision == "approve" else "REJECTED"
    res = sql.query(f"""
        UPDATE {STAGING_TABLE}
        SET status = '{status}', reviewed_by = :user, reviewed_at = current_timestamp(),
            review_note = :note, updated_at = current_timestamp()
        WHERE request_id IN ({in_sql}) AND status = 'PENDING' {self_guard}
    """, params)
    cat.invalidate("open_counts")
    return {"updated": res[0].get("num_affected_rows", 0) if res else 0, "skipped_self": skipped}


def review_batch(user: str, batch_id: str, decision: str, note: str | None) -> dict:
    """Approve or reject every PENDING request in one batch (e.g. a dictionary import)."""
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    if not batch_id:
        raise ValueError("batch_id is required")
    note = (note or "").strip() or None
    skipped = 0
    if not ALLOW_SELF_APPROVAL:
        skipped = sql.query(f"""
            SELECT count(*) AS n FROM {STAGING_TABLE}
            WHERE batch_id = :batch AND status = 'PENDING' AND requested_by = :user
        """, {"batch": batch_id, "user": user})[0]["n"]
    statement, params = queue_sql.review_batch(
        STAGING_TABLE, batch_id, "APPROVED" if decision == "approve" else "REJECTED",
        user, note, ALLOW_SELF_APPROVAL)
    res = sql.query(statement, params)
    cat.invalidate("open_counts")
    return {"updated": res[0].get("num_affected_rows", 0) if res else 0, "skipped_self": skipped}


def batches(status: str = "PENDING") -> list[dict]:
    """One row per batch with open requests: who/what it came from and its size.
    Counts come from the whole queue, not the (limited) request list."""
    return sql.query(f"""
        SELECT batch_id, min(requested_by) AS requested_by, max(note) AS note,
               count(*) AS requests,
               count(DISTINCT catalog_name, schema_name, table_name) AS tables,
               count_if(column_name IS NOT NULL) AS columns,
               count_if(column_name IS NULL) AS table_descriptions,
               min(requested_at) AS first_requested_at
        FROM {STAGING_TABLE}
        WHERE status = :status
        GROUP BY batch_id
        ORDER BY first_requested_at DESC
        LIMIT 100
    """, {"status": status})


def withdraw(user: str, request_id: str) -> int:
    res = sql.query(f"""
        UPDATE {STAGING_TABLE}
        SET status = 'WITHDRAWN', updated_at = current_timestamp()
        WHERE request_id = :id AND requested_by = :user AND status = 'PENDING'
    """, {"id": request_id, "user": user})
    cat.invalidate("open_counts")
    return res[0].get("num_affected_rows", 0) if res else 0


def force(request_id: str) -> int:
    """Let an approved change apply even while a writer to its table is running."""
    res = sql.query(f"""
        UPDATE {STAGING_TABLE}
        SET force_apply = true, updated_at = current_timestamp()
        WHERE request_id = :id AND status = 'APPROVED'
    """, {"id": request_id})
    return res[0].get("num_affected_rows", 0) if res else 0


def retry(request_id: str) -> int:
    res = sql.query(f"""
        UPDATE {STAGING_TABLE}
        SET status = 'APPROVED', apply_message = 'Re-queued for apply', updated_at = current_timestamp()
        WHERE request_id = :id AND status = 'FAILED'
    """, {"id": request_id})
    cat.invalidate("open_counts")
    return res[0].get("num_affected_rows", 0) if res else 0


def list_requests(statuses: list[str] | None, requested_by: str | None, limit: int = 200,
                  batch_id: str | None = None) -> list[dict]:
    where, params = [], {"limit": str(limit)}
    if batch_id:
        where.append("batch_id = :batch")
        params["batch"] = batch_id
    if statuses:
        in_sql, p = _in_list("st", statuses)
        where.append(f"status IN ({in_sql})")
        params.update(p)
    if requested_by:
        where.append("requested_by = :me")
        params["me"] = requested_by
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return sql.query(f"""
        SELECT {_COLUMNS} FROM {STAGING_TABLE} {clause}
        ORDER BY coalesce(updated_at, requested_at) DESC
        LIMIT {int(limit)}
    """, {k: v for k, v in params.items() if k != "limit"})


def history(catalog: str, schema: str, table: str, column: str | None) -> list[dict]:
    """Every request ever made against one table description or column."""
    col_clause = "column_name = :column" if column else "column_name IS NULL"
    params = {"catalog": catalog, "schema": schema, "table": table}
    if column:
        params["column"] = column
    return sql.query(f"""
        SELECT {_COLUMNS} FROM {STAGING_TABLE}
        WHERE catalog_name = :catalog AND schema_name = :schema AND table_name = :table
          AND {col_clause}
        ORDER BY requested_at DESC
    """, params)


def summary() -> dict:
    rows = sql.query(f"SELECT status, count(*) AS n FROM {STAGING_TABLE} GROUP BY status")
    return {r["status"]: r["n"] for r in rows}
