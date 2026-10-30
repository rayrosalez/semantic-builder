import restore_writers
from conftest import FakeW, MemLedger, continuous, pause_state, schedule


def test_restores_leftovers_from_a_crashed_run(events):
    # A run crashed mid-window: job 1's schedule and job 3 were paused, job 2 was
    # already paused before (so it was never recorded), and a pipeline was stopped.
    jobs = {1: schedule("PAUSED"), 2: schedule("PAUSED"), 3: continuous("PAUSED")}
    ledger = MemLedger(events, [
        {"apply_run_id": "crashed", "job_id": "1", "setting_kind": "schedule",
         "original_pause_status": "UNPAUSED", "restored": False},
        {"apply_run_id": "crashed", "job_id": "3", "setting_kind": "continuous",
         "original_pause_status": "UNPAUSED", "restored": False},
        {"apply_run_id": "crashed", "job_id": "p9", "setting_kind": "pipeline",
         "original_pause_status": "RUNNING", "restored": False},
        {"apply_run_id": "older", "job_id": "1", "setting_kind": "schedule",
         "original_pause_status": "UNPAUSED", "restored": True},
    ])
    w = FakeW(events, jobs)
    restored = restore_writers.run(w, ledger)
    assert len(restored) == 3
    assert pause_state(w.jobs.jobs[1]) == {"schedule": "UNPAUSED"}
    assert pause_state(w.jobs.jobs[2]) == {"schedule": "PAUSED"}
    assert pause_state(w.jobs.jobs[3]) == {"continuous": "UNPAUSED"}
    assert w.pipelines.started == ["p9"]
    assert all(r["restored"] for r in ledger.rows)
    # running it again is a no-op
    assert restore_writers.run(w, ledger) == [] and len(w.jobs.updates) == 2


def test_deleted_job_is_marked_restored(events):
    ledger = MemLedger(events, [{"apply_run_id": "r", "job_id": "77", "setting_kind": "schedule",
                                 "original_pause_status": "UNPAUSED", "restored": False}])
    restore_writers.run(FakeW(events, {}), ledger)
    assert ledger.rows[0]["restored"]
