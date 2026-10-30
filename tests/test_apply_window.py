from types import SimpleNamespace

import pytest

import apply_changes as ac
from conftest import FakeSpark, FakeW, MemLedger, continuous, pause_state, request, schedule, trigger

ACCOUNTS = ("fa", "sales_core", "accounts")
ORDERS = ("fa", "sales_core", "orders")
TABLES = {
    ACCOUNTS: ("MANAGED", "Accounts", {"name": None, "region": "old"}),
    ORDERS: ("MANAGED", None, {"order_id": None}),
}


def args(**kw):
    base = dict(staging_catalog="gov", staging_schema="sb", check_writers="true", lineage_lookback_days="30",
                self_job_id="999", run_id="run-1", mode="window", max_wait_minutes="30", dry_run="false",
                pause_pipelines="false")
    return SimpleNamespace(**{**base, **kw})


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(events, spark, jobs, running=None, pipelines=None, denied=(), **kw):
    w = FakeW(events, jobs, running, pipelines, denied)
    ledger = MemLedger(events)
    clock = Clock()
    applier = ac.Applier(spark, w, args(**kw), ledger=ledger, sleep=clock.sleep, clock=clock)
    return applier, w, ledger


def test_pauses_only_unpaused_jobs_and_restores_exact_state(events):
    jobs = {1: schedule("UNPAUSED"), 2: schedule("PAUSED"), 3: continuous("UNPAUSED"), 4: trigger("UNPAUSED")}
    spark = FakeSpark([request("a"), request("b", ORDERS, "order_id")], TABLES,
                      {"fa.sales_core.accounts": ["1", "2", "999"], "fa.sales_core.orders": ["3", "4"]})
    applier, w, ledger = make(events, spark, jobs)
    applier.run()

    touched = {job_id for job_id, _ in w.jobs.updates}
    assert 2 not in touched, "an already-paused job must never be updated"
    assert 999 not in touched, "the apply job itself is never a writer"
    assert [(1, {"schedule": "PAUSED"}), (3, {"continuous": "PAUSED"}), (4, {"trigger": "PAUSED"})] == \
        [u for u in w.jobs.updates if "PAUSED" in u[1].values()]
    assert pause_state(w.jobs.jobs[1]) == {"schedule": "UNPAUSED"}
    assert pause_state(w.jobs.jobs[2]) == {"schedule": "PAUSED"}
    assert pause_state(w.jobs.jobs[3]) == {"continuous": "UNPAUSED"}
    assert pause_state(w.jobs.jobs[4]) == {"trigger": "UNPAUSED"}
    assert all(r["restored"] for r in ledger.rows) and len(ledger.rows) == 3
    # the ledger row is written before the job is paused
    for job_id in (1, 3, 4):
        assert events.index(("record", job_id, pause_kind(jobs, job_id))) < \
            next(i for i, e in enumerate(events) if e[0] == "update" and e[1] == job_id)
    # the comment landed and the queue says which jobs were paused for it
    assert any(s.startswith("ALTER TABLE `fa`.`sales_core`.`accounts` ALTER COLUMN `name` COMMENT")
               for s in spark.writes())
    msgs = [a["msg"] for s, a in spark.statements if s.startswith("UPDATE") and a and "APPLIED" in s]
    assert "Applied in maintenance window; paused jobs 1" in msgs
    assert "Applied in maintenance window; paused jobs 3, 4" in msgs


def pause_kind(jobs, job_id):
    return next(k for k in ("schedule", "trigger", "continuous") if getattr(jobs[job_id], k))


def test_restores_even_when_apply_raises(events):
    jobs = {1: schedule("UNPAUSED")}
    # Writing the APPLIED status back to the queue fails: the error escapes apply_table
    # *and* apply_safely's FAILED update, so it propagates out of the window.
    spark = FakeSpark([request("a")], TABLES, {"fa.sales_core.accounts": ["1"]}, raise_on="UPDATE `gov`")
    applier, w, ledger = make(events, spark, jobs)
    with pytest.raises(RuntimeError, match="boom"):
        applier.run()
    assert pause_state(w.jobs.jobs[1]) == {"schedule": "UNPAUSED"}
    assert w.jobs.updates == [(1, {"schedule": "PAUSED"}), (1, {"schedule": "UNPAUSED"})]
    assert all(r["restored"] for r in ledger.rows)


def test_no_approved_requests_pauses_nothing(events):
    jobs = {1: schedule("UNPAUSED")}
    applier, w, ledger = make(events, FakeSpark([], TABLES, {"fa.sales_core.accounts": ["1"]}), jobs)
    applier.run()
    assert w.jobs.updates == [] and ledger.rows == [] and events == []


def test_dry_run_changes_nothing(events, capsys):
    jobs = {1: schedule("UNPAUSED"), 2: schedule("PAUSED")}
    spark = FakeSpark([request("a"), request("b", ORDERS, None)], TABLES,
                      {"fa.sales_core.accounts": ["1", "2"]})
    applier, w, ledger = make(events, spark, jobs, running={1: -1}, dry_run="true")
    applier.run()
    assert spark.writes() == []
    assert w.jobs.updates == [] and ledger.rows == []
    out = capsys.readouterr().out
    assert "job 1: {'schedule': 'UNPAUSED'} -> would pause ['schedule']" in out
    assert "would pause nothing" in out  # job 2, already paused
    assert "DDL: ALTER TABLE `fa`.`sales_core`.`accounts` ALTER COLUMN `name` COMMENT 'New text'" in out
    assert "DDL: COMMENT ON TABLE `fa`.`sales_core`.`orders` IS 'New text'" in out


def test_dry_run_in_defer_mode_changes_nothing(events):
    spark = FakeSpark([request("a")], TABLES, {"fa.sales_core.accounts": ["1"]})
    applier, w, _ = make(events, spark, {1: schedule()}, mode="defer", dry_run="true")
    applier.run()
    assert spark.writes() == [] and w.jobs.updates == []


def test_waits_for_running_writer_then_applies(events):
    jobs = {1: schedule("UNPAUSED")}
    spark = FakeSpark([request("a")], TABLES, {"fa.sales_core.accounts": ["1"]})
    applier, w, _ = make(events, spark, jobs, running={1: 3})  # running for 3 polls, then done
    applier.run()
    assert applier.summary == {"applied": 1}
    assert applier.clock() == 3 * ac.POLL_SECONDS


def test_timeout_defers_only_tables_with_running_writers(events):
    jobs = {1: schedule("UNPAUSED"), 2: schedule("UNPAUSED")}
    spark = FakeSpark([request("a"), request("b", ORDERS, "order_id")], TABLES,
                      {"fa.sales_core.accounts": ["1"], "fa.sales_core.orders": ["2"]})
    applier, w, ledger = make(events, spark, jobs, running={1: -1}, max_wait_minutes="5")
    applier.run()
    assert applier.summary == {"deferred": 1, "applied": 1}
    deferred = [a["msg"] for s, a in spark.statements if "deferrals" in s]
    assert deferred and deferred[0].startswith("Waiting: job 1 (run-of-1) is running after 5 min")
    assert not any("`accounts` ALTER" in s for s in spark.writes())
    assert any("`orders` ALTER" in s for s in spark.writes())
    assert pause_state(w.jobs.jobs[1]) == {"schedule": "UNPAUSED"}  # never cancelled, restored


def test_continuous_pipeline_defers_unless_pause_pipelines(events):
    pl = SimpleNamespace(name="sf_ingest", spec=SimpleNamespace(continuous=True),
                         latest_updates=[SimpleNamespace(state=SimpleNamespace(value="RUNNING"))], state=None)
    spark = FakeSpark([request("a")], TABLES)
    applier, w, _ = make(events, spark, {}, pipelines={"p1": pl})
    applier.find_writers = lambda tables: {"fa.sales_core.accounts": {("PIPELINE", "p1")}}
    applier.run()
    msgs = [a["msg"] for s, a in spark.statements if "deferrals" in s]
    assert msgs == ["Waiting: continuous pipeline sf_ingest must be stopped manually or enable pause_pipelines"]
    assert w.pipelines.stopped == []

    events.clear()
    spark = FakeSpark([request("a")], TABLES)
    applier, w, ledger = make(events, spark, {}, pipelines={"p1": pl}, pause_pipelines="true")
    applier.find_writers = lambda tables: {"fa.sales_core.accounts": {("PIPELINE", "p1")}}
    applier.run()
    assert w.pipelines.stopped == ["p1"] and w.pipelines.started == ["p1"]
    assert applier.summary == {"applied": 1} and all(r["restored"] for r in ledger.rows)


def test_forced_request_applies_while_writer_still_running(events):
    spark = FakeSpark([request("a", force=True)], TABLES, {"fa.sales_core.accounts": ["1"]})
    applier, w, _ = make(events, spark, {1: schedule()}, running={1: -1}, max_wait_minutes="1")
    applier.run()
    assert applier.summary == {"applied": 1}


def test_unpausable_job_defers_its_tables(events):
    spark = FakeSpark([request("a")], TABLES, {"fa.sales_core.accounts": ["7"]})
    applier, w, _ = make(events, spark, {7: schedule()}, denied={7})  # no CAN_MANAGE on job 7
    applier.run()
    assert applier.summary == {"deferred": 1}
    assert not any("ALTER" in s for s in spark.writes())


def test_deleted_job_in_history_does_not_block(events):
    spark = FakeSpark([request("a")], TABLES, {"fa.sales_core.accounts": ["404"]})
    applier, w, _ = make(events, spark, {})  # job 404 was deleted but is still in Delta history
    applier.run()
    assert applier.summary == {"applied": 1} and w.jobs.updates == []
