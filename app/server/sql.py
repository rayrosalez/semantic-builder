"""Statement Execution API wrapper: rows come back as dicts, with retries for
Delta write conflicts on the staging table."""
import time
from typing import Any

from databricks.sdk.service.sql import StatementParameterListItem, StatementState

from .config import WAREHOUSE_ID, get_workspace_client

_RETRYABLE = ("ConcurrentAppend", "ConcurrentDelete", "ConcurrentTransaction",
              "ConcurrentModification", "DELTA_CONCURRENT", "MetadataChanged")


def _coerce(value: Any, type_name: str) -> Any:
    if value is None:
        return None
    if type_name in ("INT", "LONG", "BIGINT", "SHORT", "SMALLINT", "BYTE"):
        return int(value)
    if type_name in ("DOUBLE", "FLOAT", "DECIMAL"):
        return float(value)
    if type_name == "BOOLEAN":
        return str(value).lower() == "true"
    return value


def _execute(statement: str, params: dict[str, Any] | None):
    w = get_workspace_client()
    sdk_params = [
        StatementParameterListItem(name=k, value=None if v is None else str(v))
        for k, v in (params or {}).items()
    ]
    resp = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID,
        statement=statement,
        parameters=sdk_params or None,
        wait_timeout="50s",
    )
    while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(1)
        resp = w.statement_execution.get_statement(resp.statement_id)
    return resp


def query(statement: str, params: dict[str, Any] | None = None, retries: int = 3) -> list[dict]:
    """Run SQL; `params` are bound as named markers (:name), always as strings."""
    for attempt in range(retries + 1):
        resp = _execute(statement, params)
        if resp.status.state == StatementState.SUCCEEDED:
            break
        msg = (resp.status.error.message if resp.status.error else "") or ""
        if attempt < retries and any(k in msg for k in _RETRYABLE):
            time.sleep(1.5 * (attempt + 1))
            continue
        raise RuntimeError(f"SQL {resp.status.state.value}: {msg[:500]}")

    result, schema = resp.result, resp.manifest.schema if resp.manifest else None
    if not result or not result.data_array or not schema:
        return []
    cols = [(c.name, getattr(c.type_name, "value", str(c.type_name))) for c in schema.columns]
    return [{n: _coerce(row[i], t) for i, (n, t) in enumerate(cols)} for row in result.data_array]
