"""SQL for writing change requests, shared by the app and the import job.

The app runs SQL on a warehouse (Statement Execution API) and the import job
runs it with Spark. Both accept `:name` parameter markers, so these helpers
only build the statement text and its parameters; each caller executes them
its own way. Pure Python: no Databricks imports, so jobs can load it too.
"""
import fnmatch

OPEN = ("PENDING", "APPROVED")

COLUMNS = """request_id, batch_id, entity_type, catalog_name, schema_name, table_name,
    column_name, table_type, field, previous_value, proposed_value, note, status,
    requested_by, requested_at, reviewed_by, reviewed_at, review_note, force_apply,
    apply_attempts, deferrals, last_attempt_at, apply_message, applied_at, updated_at"""


def schema_in_scope(scope: list[str], catalog: str, schema: str) -> bool:
    """`scope` entries are catalog | catalog.schema | catalog.schema_glob*."""
    if schema == "information_schema":
        return False
    for entry in scope:
        parts = entry.split(".", 1)
        if parts[0] != catalog:
            continue
        if len(parts) == 1 or fnmatch.fnmatch(schema, parts[1]):
            return True
    return False


def parse_scope(value: str) -> list[str]:
    return [s.strip() for s in (value or "").split(",") if s.strip()]


def supersede(queue: str, fields: list[tuple[str, str, str, str | None]],
              message: str = "Superseded by a newer request") -> tuple[str, dict]:
    """Mark open requests for these (catalog, schema, table, column) fields
    SUPERSEDED. A None column is the table description."""
    preds, params = [], {"sup_msg": message}
    for i, (c, s, t, col) in enumerate(fields):
        params.update({f"f{i}c": c, f"f{i}s": s, f"f{i}t": t})
        if col is None:
            col_pred = "column_name IS NULL"
        else:
            col_pred = f"column_name = :f{i}k"
            params[f"f{i}k"] = col
        preds.append(f"(catalog_name = :f{i}c AND schema_name = :f{i}s AND table_name = :f{i}t AND {col_pred})")
    statement = f"""
        UPDATE {queue}
        SET status = 'SUPERSEDED', updated_at = current_timestamp(), apply_message = :sup_msg
        WHERE status IN ('PENDING', 'APPROVED') AND ({' OR '.join(preds)})
    """
    return statement, params


def insert(queue: str, rows: list[dict]) -> tuple[str, dict]:
    """One INSERT for a set of new PENDING requests. Each row needs request_id,
    batch_id, catalog, schema, table, column (None = table), table_type,
    previous_value, proposed_value, note, requested_by."""
    values, params = [], {}

    def m(name: str, value) -> str:
        # NULLs are written inline: an untyped NULL parameter isn't accepted everywhere.
        if value is None:
            return "NULL"
        params[name] = value
        return f":{name}"

    for i, r in enumerate(rows):
        p = f"r{i}_"
        fields = [
            m(f"{p}id", r["request_id"]), m(f"{p}batch", r["batch_id"]),
            m(f"{p}type", "COLUMN" if r["column"] else "TABLE"),
            m(f"{p}cat", r["catalog"]), m(f"{p}sch", r["schema"]), m(f"{p}tbl", r["table"]),
            m(f"{p}col", r["column"]), m(f"{p}ttype", r["table_type"]), "'COMMENT'",
            m(f"{p}prev", r["previous_value"]), m(f"{p}new", r["proposed_value"]),
            m(f"{p}note", r["note"]), "'PENDING'", m(f"{p}user", r["requested_by"]),
        ]
        values.append(
            f"({', '.join(fields)}, current_timestamp(), NULL, NULL, NULL, "
            "false, 0, 0, NULL, NULL, NULL, current_timestamp())"
        )
    return f"INSERT INTO {queue} ({COLUMNS}) VALUES {', '.join(values)}", params


def submit_statements(queue: str, rows: list[dict]) -> list[tuple[str, dict]]:
    """Supersede-then-insert for a set of new requests: what both the app's
    submit and the dictionary import run, in this order."""
    if not rows:
        return []
    fields = [(r["catalog"], r["schema"], r["table"], r["column"]) for r in rows]
    return [supersede(queue, fields), insert(queue, rows)]


def review_batch(queue: str, batch_id: str, status: str, user: str, note: str | None,
                 allow_self_approval: bool) -> tuple[str, dict]:
    """Approve or reject every PENDING request in one batch, in one UPDATE."""
    if status not in ("APPROVED", "REJECTED"):
        raise ValueError("status must be APPROVED or REJECTED")
    self_guard = "" if allow_self_approval else "AND requested_by <> :user"
    params = {"batch": batch_id, "user": user}
    if note is not None:
        params["note"] = note
    statement = f"""
        UPDATE {queue}
        SET status = '{status}', reviewed_by = :user, reviewed_at = current_timestamp(),
            review_note = {':note' if note is not None else 'NULL'}, updated_at = current_timestamp()
        WHERE batch_id = :batch AND status = 'PENDING' {self_guard}
    """
    return statement, params
