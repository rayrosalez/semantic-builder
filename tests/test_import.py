import json
import re
from types import SimpleNamespace

import import_dictionary as imp
from conftest import Result, Row

SCOPE = "fa.sales_*"
DICT = [
    # table description (column NULL), a changed column, an unchanged one, an apostrophe
    {"catalog_name": "FA", "schema_name": "sales_core", "table_name": "Accounts", "column_name": None,
     "description": "One row per customer account.", "plan_id": "plan-42", "week_of": "2026-10-26",
     "source": "excel-v3"},
    {"catalog_name": "fa", "schema_name": "sales_core", "table_name": "accounts", "column_name": "Name",
     "description": "  Account holder's legal name.  ", "plan_id": "plan-42", "week_of": "2026-10-26",
     "source": "excel-v3"},
    {"catalog_name": "fa", "schema_name": "sales_core", "table_name": "accounts", "column_name": "region",
     "description": "Sales region", "plan_id": "plan-42", "week_of": "2026-10-26", "source": "ai"},
    # older week for the same field: the newer one wins
    {"catalog_name": "fa", "schema_name": "sales_core", "table_name": "accounts", "column_name": "name",
     "description": "stale text", "plan_id": "plan-41", "week_of": "2026-10-19", "source": "x"},
    # gone from UC
    {"catalog_name": "fa", "schema_name": "sales_core", "table_name": "accounts", "column_name": "dropped_col",
     "description": "whatever", "plan_id": "plan-42", "week_of": "2026-10-26", "source": "x"},
    {"catalog_name": "fa", "schema_name": "sales_core", "table_name": "no_such_table", "column_name": None,
     "description": "whatever", "plan_id": "plan-42", "week_of": "2026-10-26", "source": "x"},
    # out of scope (schema, then catalog)
    {"catalog_name": "fa", "schema_name": "hr", "table_name": "people", "column_name": None,
     "description": "People", "plan_id": "plan-42", "week_of": "2026-10-26", "source": "x"},
    {"catalog_name": "other", "schema_name": "sales_core", "table_name": "x", "column_name": None,
     "description": "X", "plan_id": "plan-42", "week_of": "2026-10-26", "source": "x"},
]


class DF:
    def __init__(self, rows):
        self.rows = rows
        self.columns = list(rows[0])
        self.selected = None

    def select(self, *cols):
        self.selected = cols
        return SimpleNamespace(collect=lambda: [Row(**{c: r.get(c) for c in cols}) for r in self.rows])


class ImportSpark:
    """Dictionary + information_schema from fixtures; queue SQL really runs on DuckDB."""

    def __init__(self, duck, rows):
        self.duck, self.df = duck, DF(rows)

    def table(self, name):
        return self.df

    def sql(self, statement, args=None):
        s = " ".join(statement.split())
        if "information_schema.tables" in s:
            return Result([Row(table_schema="sales_core", table_name="accounts", table_type="MANAGED",
                               comment=None)])
        if "information_schema.columns" in s:
            return Result([Row(table_schema="sales_core", table_name="accounts", column_name=c, comment=v)
                           for c, v in (("name", None), ("region", "Sales region"))])
        return Result(Row(**r) for r in self.duck.query(statement, args))


def run(duck, rows=DICT, **kw):
    a = imp.parse_args(["--staging_catalog=gov", "--staging_schema=sb", "--dictionary_table=fa.gov.dictionary",
                        f"--scope={SCOPE}", *[f"--{k}={v}" for k, v in kw.items()]])
    spark = ImportSpark(duck, rows)
    return imp.run(spark, a), spark


def test_import_creates_requests_once(duck):
    report, spark = run(duck)
    rows = duck.rows()
    assert len(rows) == 2
    by_col = {r["column_name"]: r for r in rows}
    assert by_col[None]["proposed_value"] == "One row per customer account."
    assert by_col[None]["entity_type"] == "TABLE" and by_col[None]["table_name"] == "accounts"
    assert by_col["name"]["proposed_value"] == "Account holder's legal name."
    for r in rows:
        assert r["status"] == "PENDING" and r["requested_by"] == "dictionary-import"
        assert r["batch_id"] == "plan-42" and r["note"] == "Dictionary import, week_of 2026-10-26"
        assert r["field"] == "COMMENT" and r["previous_value"] is None
    t = report["totals"]
    assert t["new"] == 2 and t["unchanged"] == 1
    assert t["missing_column"] == 1 and t["missing_table"] == 1 and t["out_of_scope"] == 2
    assert "source" not in spark.df.selected  # the source tag is never read

    report2, _ = run(duck)
    assert len(duck.rows()) == 2, "re-running on the same input must not create requests"
    assert report2["totals"].get("new", 0) == 0 and report2["totals"]["already_queued"] == 2


def test_rejected_value_is_not_re_proposed(duck):
    run(duck)
    duck.query("UPDATE gov.sb.metadata_change_requests SET status = 'REJECTED'")
    run(duck)
    assert len(duck.rows()) == 2


def test_new_text_supersedes_open_request(duck):
    run(duck)
    changed = [dict(r, description="Legal name of the account holder.", week_of="2026-11-02", plan_id="plan-43")
               if r["column_name"] in ("Name",) else r for r in DICT]
    run(duck, changed)
    rows = duck.rows("column_name = 'name'")
    assert sorted(r["status"] for r in rows) == ["PENDING", "SUPERSEDED"]
    live = next(r for r in rows if r["status"] == "PENDING")
    assert live["batch_id"] == "plan-43" and live["proposed_value"] == "Legal name of the account holder."


def test_dry_run_inserts_nothing(duck):
    report, _ = run(duck, dry_run="true")
    assert duck.rows() == [] and report["totals"]["new"] == 2


def test_column_map_and_chunking(duck, monkeypatch):
    renamed = [{"cat": r["catalog_name"], "sch": r["schema_name"], "tbl": r["table_name"],
                "col": r["column_name"], "text": r["description"]} for r in DICT]
    cmap = {"catalog": "cat", "schema": "sch", "table": "tbl", "column": "col", "description": "text",
            "plan_id": "", "week_of": ""}
    monkeypatch.setattr(imp, "CHUNK", 1)
    run(duck, renamed, column_map=json.dumps(cmap))
    rows = duck.rows()
    assert len(rows) == 2
    assert len({r["batch_id"] for r in rows}) == 1  # no plan_id -> one generated batch per run
    assert re.fullmatch(r"[0-9a-f-]{36}", rows[0]["batch_id"])
    assert rows[0]["note"] == "Dictionary import, week_of unknown"
