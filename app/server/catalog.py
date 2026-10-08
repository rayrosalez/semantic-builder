"""Read side: the metadata estate, from each catalog's information_schema.

One query per catalog returns every table with its column-documentation
counts; catalog/schema/table nodes for the graph are all derived from that,
so drilling down never waits on another warehouse round trip.
"""
import threading
import time

from . import sql
from .config import (
    COVERAGE_TARGET,
    STAGING_TABLE,
    ident,
    schema_in_scope,
    scope_catalogs,
    table_visible,
)

_TTL = 60
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def _cached(key: str, ttl: int, fn):
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def invalidate(prefix: str = "") -> None:
    with _lock:
        for k in [k for k in _cache if k.startswith(prefix)]:
            _cache.pop(k, None)


def _documented(text) -> bool:
    return bool(text and str(text).strip())


def _catalog_tables(catalog: str) -> list[dict]:
    def load():
        c = ident(catalog)
        rows = sql.query(f"""
            WITH cols AS (
              SELECT table_schema, table_name, count(*) AS n_cols,
                     count_if(nullif(trim(comment), '') IS NOT NULL) AS doc_cols
              FROM {c}.information_schema.columns
              GROUP BY ALL
            )
            SELECT t.table_schema AS schema_name, t.table_name, t.table_type, t.comment,
                   coalesce(cols.n_cols, 0) AS n_cols, coalesce(cols.doc_cols, 0) AS doc_cols
            FROM {c}.information_schema.tables t
            LEFT JOIN cols USING (table_schema, table_name)
            WHERE t.table_schema <> 'information_schema'
        """)
        return [
            r for r in rows
            if schema_in_scope(catalog, r["schema_name"]) and table_visible(r["table_name"])
        ]
    return _cached(f"tables:{catalog}", _TTL, load)


def _schema_comments(catalog: str) -> dict[str, str | None]:
    def load():
        rows = sql.query(
            f"SELECT schema_name, comment FROM {ident(catalog)}.information_schema.schemata"
        )
        return {r["schema_name"]: r["comment"] for r in rows}
    return _cached(f"schemata:{catalog}", _TTL, load)


def open_counts() -> dict[tuple, int]:
    """Open (pending + approved) change requests per table."""
    def load():
        rows = sql.query(f"""
            SELECT catalog_name, schema_name, table_name, count(*) AS n
            FROM {STAGING_TABLE}
            WHERE status IN ('PENDING', 'APPROVED')
            GROUP BY ALL
        """)
        return {(r["catalog_name"], r["schema_name"], r["table_name"]): r["n"] for r in rows}
    return _cached("open_counts", 10, load)


def _table_node(t: dict, catalog: str, pending: dict) -> dict:
    documented = int(_documented(t["comment"])) + t["doc_cols"]
    total = 1 + t["n_cols"]
    return {
        "name": t["table_name"],
        "table_type": t["table_type"],
        "comment": t["comment"],
        "column_count": t["n_cols"],
        "documented_columns": t["doc_cols"],
        "has_comment": _documented(t["comment"]),
        "documented": documented,
        "total": total,
        "coverage": documented / total,
        "open_changes": pending.get((catalog, t["schema_name"], t["table_name"]), 0),
    }


def _rollup(nodes: list[dict]) -> dict:
    documented = sum(n["documented"] for n in nodes)
    total = sum(n["total"] for n in nodes)
    return {
        "documented": documented,
        "total": total,
        "coverage": documented / total if total else 1.0,
        "open_changes": sum(n["open_changes"] for n in nodes),
        "needs_attention": sum(1 for n in nodes if n["coverage"] < COVERAGE_TARGET),
    }


def list_tables(catalog: str, schema: str) -> list[dict]:
    pending = open_counts()
    rows = [t for t in _catalog_tables(catalog) if t["schema_name"] == schema]
    return sorted((_table_node(t, catalog, pending) for t in rows), key=lambda n: n["name"])


def list_schemas(catalog: str) -> list[dict]:
    pending = open_counts()
    comments = _schema_comments(catalog)
    by_schema: dict[str, list[dict]] = {}
    for t in _catalog_tables(catalog):
        by_schema.setdefault(t["schema_name"], []).append(_table_node(t, catalog, pending))
    out = []
    for name, tables in sorted(by_schema.items()):
        out.append({"name": name, "comment": comments.get(name), "table_count": len(tables),
                    "column_count": sum(t["column_count"] for t in tables), **_rollup(tables)})
    return out


def list_catalogs() -> list[dict]:
    out = []
    for catalog in scope_catalogs():
        try:
            schemas = list_schemas(catalog)
        except Exception as e:  # catalog missing or not visible to the app
            out.append({"name": catalog, "error": str(e)[:200], "schema_count": 0,
                        "table_count": 0, "documented": 0, "total": 0, "coverage": 1.0,
                        "open_changes": 0, "needs_attention": 0})
            continue
        documented = sum(s["documented"] for s in schemas)
        total = sum(s["total"] for s in schemas)
        out.append({
            "name": catalog,
            "schema_count": len(schemas),
            "table_count": sum(s["table_count"] for s in schemas),
            "documented": documented,
            "total": total,
            "coverage": documented / total if total else 1.0,
            "open_changes": sum(s["open_changes"] for s in schemas),
            "needs_attention": sum(s["needs_attention"] for s in schemas),
        })
    return out


def table_detail(catalog: str, schema: str, table: str) -> dict:
    """Live table + column metadata, with open requests and drift per entity."""
    c = ident(catalog)
    params = {"schema": schema, "table": table}
    trows = sql.query(f"""
        SELECT table_type, comment, table_owner, created, last_altered, data_source_format
        FROM {c}.information_schema.tables
        WHERE table_schema = :schema AND table_name = :table
    """, params)
    if not trows:
        raise LookupError(f"{catalog}.{schema}.{table} not found or not visible")
    cols = sql.query(f"""
        SELECT column_name, full_data_type AS data_type, comment, ordinal_position
        FROM {c}.information_schema.columns
        WHERE table_schema = :schema AND table_name = :table
        ORDER BY ordinal_position
    """, params)
    reqs = sql.query(f"""
        SELECT column_name, request_id, status, proposed_value, requested_by, requested_at,
               applied_at, apply_message,
               row_number() OVER (PARTITION BY column_name, status IN ('PENDING','APPROVED')
                                  ORDER BY requested_at DESC) AS rn
        FROM {STAGING_TABLE}
        WHERE catalog_name = :catalog AND schema_name = :schema AND table_name = :table
          AND status IN ('PENDING', 'APPROVED', 'APPLIED')
    """, {"catalog": catalog, **params})

    open_by: dict = {}
    applied_by: dict = {}
    for r in reqs:
        if r["rn"] != 1:
            continue
        key = r["column_name"]
        if r["status"] in ("PENDING", "APPROVED"):
            open_by[key] = r
        elif key not in applied_by or (r["applied_at"] or "") > (applied_by[key]["applied_at"] or ""):
            applied_by[key] = r

    def entity(key, live_comment):
        applied = applied_by.get(key)
        drift = bool(applied and (applied["proposed_value"] or "") != (live_comment or ""))
        return {"open_request": open_by.get(key), "drifted": drift,
                "last_applied_value": applied["proposed_value"] if applied else None}

    t = trows[0]
    return {
        "catalog": catalog, "schema": schema, "name": table,
        **t,
        **entity(None, t["comment"]),
        "columns": [{**col, **entity(col["column_name"], col["comment"])} for col in cols],
    }


def search(q: str, limit: int = 25) -> list[dict]:
    """Name/description search across in-scope tables and columns."""
    q = q.strip().lower()
    if len(q) < 2:
        return []
    hits = []
    for catalog in scope_catalogs():
        try:
            tables = _catalog_tables(catalog)
        except Exception:
            continue
        for t in tables:
            if q in t["table_name"].lower() or q in (t["comment"] or "").lower():
                hits.append({"kind": "table", "catalog": catalog, "schema": t["schema_name"],
                             "table": t["table_name"], "comment": t["comment"]})
        if len(hits) >= limit:
            break
    return hits[:limit]
