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
from . import sql
from .config import ALLOW_SELF_APPROVAL, STAGING_TABLE, schema_in_scope

OPEN = ("PENDING", "APPROVED")

_COLUMNS = """request_id, batch_id, entity_type, catalog_name, schema_name, table_name,
    column_name, table_type, field, previous_value, proposed_value, note, status,
    requested_by, requested_at, reviewed_by, reviewed_at, review_note, force_apply,
    apply_attempts, deferrals, last_attempt_at, apply_message, applied_at, updated_at"""


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
    rows, params, request_ids = [], {}, []
    for i, ch in enumerate(changes):
        column = ch.get("column") or None
        if column not in live:
            raise LookupError(f"Column {column} not found on {table}")
        proposed = (ch.get("proposed_value") or "").strip()
        if proposed == (live[column] or "").strip():
            continue
        rid = str(uuid.uuid4())
        request_ids.append(rid)
        p = f"r{i}_"
        params.update({
            f"{p}id": rid, f"{p}type": "COLUMN" if column else "TABLE", f"{p}col": column,
            f"{p}prev": live[column], f"{p}new": proposed,
        })
        rows.append(
            f"(:{p}id, :batch, :{p}type, :catalog, :schema, :table, :{p}col, :ttype, 'COMMENT', "
            f":{p}prev, :{p}new, :note, 'PENDING', :user, current_timestamp(), NULL, NULL, NULL, "
            f"false, 0, 0, NULL, NULL, NULL, current_timestamp())"
        )
    if not rows:
        return {"submitted": 0, "request_ids": []}

    params.update({"batch": batch_id, "catalog": catalog, "schema": schema, "table": table,
                   "ttype": detail["table_type"], "note": (note or "").strip() or None, "user": user})

    cols = [params[f"r{i}_col"] for i in range(len(changes)) if f"r{i}_col" in params]
    col_filter = " OR ".join(
        "column_name IS NULL" if c is None else f"column_name = :sc{j}" for j, c in enumerate(cols)
    )
    sql.query(f"""
        UPDATE {STAGING_TABLE}
        SET status = 'SUPERSEDED', updated_at = current_timestamp(),
            apply_message = 'Superseded by a newer request'
        WHERE catalog_name = :catalog AND schema_name = :schema AND table_name = :table
          AND status IN ('PENDING', 'APPROVED') AND ({col_filter})
    """, {"catalog": catalog, "schema": schema, "table": table,
          **{f"sc{j}": c for j, c in enumerate(cols) if c is not None}})

    sql.query(f"INSERT INTO {STAGING_TABLE} ({_COLUMNS}) VALUES {', '.join(rows)}", params)
    cat.invalidate("open_counts")
    return {"submitted": len(rows), "batch_id": batch_id, "request_ids": request_ids}


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


def list_requests(statuses: list[str] | None, requested_by: str | None, limit: int = 200) -> list[dict]:
    where, params = [], {"limit": str(limit)}
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
