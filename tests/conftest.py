"""Shared fakes. Nothing here talks to Databricks: Spark and the SDK are
replaced by small in-memory stand-ins, and queue SQL that must really run is
executed on DuckDB after a light dialect translation."""
import copy
import os
import re
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "app"), os.path.join(ROOT, "jobs")]
os.environ.setdefault("SB_STAGING_CATALOG", "gov")
os.environ.setdefault("SB_STAGING_SCHEMA", "sb")
os.environ.setdefault("SB_SCOPE", "fa.sales_*")

from databricks.sdk.errors import NotFound, PermissionDenied  # noqa: E402
from databricks.sdk.service.jobs import (  # noqa: E402
    Continuous, CronSchedule, JobSettings, PauseStatus, TriggerSettings)

WRITES = ("UPDATE", "INSERT", "ALTER", "COMMENT", "DELETE", "MERGE")


# ---------- Spark ----------

class Row(SimpleNamespace):
    def asDict(self):
        return dict(vars(self))


class Result(list):
    def collect(self):
        return list(self)


class FakeSpark:
    """Answers the apply job's reads from fixtures and records every statement."""

    def __init__(self, approved=(), tables=None, history=None, raise_on=None):
        self.approved = list(approved)
        self.tables = tables or {}      # (c, s, t) -> (table_type, comment, {column: comment})
        self.history = history or {}    # "c.s.t" -> [job ids]
        self.raise_on = raise_on        # substring of a statement that should blow up
        self.statements: list[tuple[str, dict | None]] = []

    def sql(self, statement, args=None):
        s = " ".join(statement.split())
        self.statements.append((s, args))
        if self.raise_on and self.raise_on in s:
            raise RuntimeError(f"boom: {self.raise_on}")
        if "row_number() OVER" in s:
            return Result(self.approved)
        if "table_lineage" in s:
            return Result([])
        if "DESCRIBE HISTORY" in s:
            name = ".".join(re.findall(r"`([^`]+)`", s.split("DESCRIBE HISTORY")[1])[:3])
            return Result(Row(job_id=j) for j in self.history.get(name, []))
        m = re.search(r"`([^`]+)`\.information_schema\.(tables|columns)", s)
        if m:
            key = (m.group(1), args["s"], args["t"])
            if key not in self.tables:
                return Result([])
            ttype, comment, cols = self.tables[key]
            if m.group(2) == "tables":
                return Result([Row(table_type=ttype, comment=comment)])
            return Result(Row(column_name=c, comment=v) for c, v in cols.items())
        return Result([])

    def writes(self):
        return [s for s, _ in self.statements if s.split(" ", 1)[0] in WRITES]


def request(rid, table=("fa", "sales_core", "accounts"), column="name", value="New text",
            previous=None, force=False):
    c, s, t = table
    return Row(request_id=rid, catalog_name=c, schema_name=s, table_name=t, column_name=column,
               previous_value=previous, proposed_value=value, force_apply=force, rn=1,
               requested_at="2026-10-01", field="COMMENT")


# ---------- SDK ----------

def schedule(status="UNPAUSED"):
    return JobSettings(schedule=CronSchedule(quartz_cron_expression="0 0/15 * * * ?", timezone_id="UTC",
                                             pause_status=PauseStatus(status)))


def continuous(status="UNPAUSED"):
    return JobSettings(continuous=Continuous(pause_status=PauseStatus(status)))


def trigger(status="UNPAUSED"):
    return JobSettings(trigger=TriggerSettings(pause_status=PauseStatus(status)))


def pause_state(settings: JobSettings) -> dict:
    return {k: getattr(settings, k).pause_status.value
            for k in ("schedule", "trigger", "continuous") if getattr(settings, k)}


class FakeJobs:
    def __init__(self, events, jobs: dict[int, JobSettings], running=None, denied=()):
        self.events, self.jobs, self.denied = events, jobs, set(denied)
        self.running = running or {}  # job id -> number of polls it stays running (-1 = forever)
        self.updates = []

    def get(self, job_id):
        if job_id in self.denied:
            raise PermissionDenied(f"no access to job {job_id}")
        if job_id not in self.jobs:
            raise NotFound(f"job {job_id} not found")
        return SimpleNamespace(job_id=job_id, settings=copy.deepcopy(self.jobs[job_id]))

    def update(self, job_id, new_settings):
        if job_id in self.denied:
            raise PermissionDenied(f"no CAN_MANAGE on job {job_id}")
        changed = {k: v for k, v in vars(new_settings).items() if v is not None}
        self.updates.append((job_id, {k: v.pause_status.value for k, v in changed.items()}))
        self.events.append(("update", job_id, tuple(sorted(changed))))
        for k, v in changed.items():
            setattr(self.jobs[job_id], k, v)

    def list_runs(self, job_id, active_only=False):
        n = self.running.get(job_id, 0)
        if n == 0:
            return []
        if n > 0:
            self.running[job_id] = n - 1
        return [SimpleNamespace(run_name=f"run-of-{job_id}")]

    def list(self, expand_tasks=False):
        return []


class FakePipelines:
    def __init__(self, pipelines=None):
        self.pipelines = pipelines or {}  # id -> SimpleNamespace(name, spec, latest_updates, state)
        self.stopped, self.started = [], []

    def get(self, pid):
        return self.pipelines[pid]

    def stop(self, pid):
        self.stopped.append(pid)
        self.pipelines[pid].latest_updates = []
        self.pipelines[pid].state = None

    def start_update(self, pid):
        self.started.append(pid)


class FakeW:
    def __init__(self, events, jobs, running=None, pipelines=None, denied=()):
        self.jobs = FakeJobs(events, jobs, running, denied)
        self.pipelines = FakePipelines(pipelines)


class MemLedger:
    """paused_writers in memory, logging to the shared event list."""

    def __init__(self, events, rows=None):
        self.events, self.rows = events, list(rows or [])

    def record(self, run_id, obj_id, kind, original):
        self.events.append(("record", int(obj_id) if str(obj_id).isdigit() else obj_id, kind))
        self.rows.append({"apply_run_id": run_id, "job_id": str(obj_id), "setting_kind": kind,
                          "original_pause_status": original, "restored": False})

    def pending(self, run_id=None):
        return [dict(r) for r in self.rows if not r["restored"] and (run_id is None or r["apply_run_id"] == run_id)]

    def mark_restored(self, run_id, obj_id, kind):
        for r in self.rows:
            if (r["apply_run_id"], r["job_id"], r["setting_kind"]) == (run_id, str(obj_id), kind):
                r["restored"] = True


@pytest.fixture
def events():
    return []


# ---------- DuckDB for queue SQL ----------

QUEUE_DDL = """CREATE TABLE gov.sb.metadata_change_requests (
  request_id VARCHAR, batch_id VARCHAR, entity_type VARCHAR, catalog_name VARCHAR,
  schema_name VARCHAR, table_name VARCHAR, column_name VARCHAR, table_type VARCHAR, field VARCHAR,
  previous_value VARCHAR, proposed_value VARCHAR, note VARCHAR, status VARCHAR, requested_by VARCHAR,
  requested_at TIMESTAMP, reviewed_by VARCHAR, reviewed_at TIMESTAMP, review_note VARCHAR,
  force_apply BOOLEAN, apply_attempts INT, deferrals INT, last_attempt_at TIMESTAMP,
  apply_message VARCHAR, applied_at TIMESTAMP, updated_at TIMESTAMP)"""


def to_duck(statement: str) -> str:
    s = statement.replace("`", '"').replace("current_timestamp()", "current_timestamp")
    return re.sub(r"(?<![:\w]):([A-Za-z_]\w*)", r"$\1", s)


class Duck:
    def __init__(self):
        import duckdb
        self.con = duckdb.connect()
        self.con.execute("ATTACH ':memory:' AS gov")
        self.con.execute("CREATE SCHEMA gov.sb")
        self.con.execute(QUEUE_DDL)

    def query(self, statement, params=None, retries=0):
        cur = self.con.execute(to_duck(statement), params or {})
        if statement.strip().split(" ", 1)[0].upper() in ("UPDATE", "INSERT", "DELETE"):
            return [{"num_affected_rows": cur.fetchone()[0]}]
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def rows(self, where="1=1"):
        return self.query(f"SELECT * FROM gov.sb.metadata_change_requests WHERE {where} ORDER BY request_id")


@pytest.fixture
def duck():
    return Duck()
