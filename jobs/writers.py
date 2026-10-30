"""Pause and restore the jobs/pipelines that write to tables, for maintenance
windows. Used by apply_changes.py (window mode) and restore_writers.py.

Every change is recorded in <staging>.paused_writers BEFORE it is made, so a
crash at any point leaves enough behind to put things back:

  apply_run_id, job_id, setting_kind, original_pause_status, paused_at, restored_at

setting_kind is schedule | trigger | continuous for a job (job_id is the job
id), or pipeline for a continuous pipeline we stopped (job_id holds the
pipeline id, original_pause_status is RUNNING).
"""
import dataclasses

from databricks.sdk.errors import NotFound
from databricks.sdk.service.jobs import JobSettings, PauseStatus

JOB_SETTING_KINDS = ("schedule", "trigger", "continuous")
PIPELINE_KIND = "pipeline"

ACTIVE_PIPELINE_UPDATE_STATES = {"QUEUED", "CREATED", "WAITING_FOR_RESOURCES", "INITIALIZING",
                                 "RESETTING", "SETTING_UP_TABLES", "RUNNING", "STOPPING"}


class SparkLedger:
    """The paused_writers state table."""

    def __init__(self, spark, table: str):
        self.spark, self.table = spark, table

    def record(self, run_id: str, obj_id: str, kind: str, original: str) -> None:
        self.spark.sql(
            f"INSERT INTO {self.table} (apply_run_id, job_id, setting_kind, original_pause_status, "
            "paused_at, restored_at) VALUES (:run, :id, :kind, :orig, current_timestamp(), NULL)",
            args={"run": run_id, "id": str(obj_id), "kind": kind, "orig": original})

    def pending(self, run_id: str | None = None) -> list[dict]:
        where = "restored_at IS NULL" + (" AND apply_run_id = :run" if run_id else "")
        rows = self.spark.sql(
            f"SELECT apply_run_id, job_id, setting_kind, original_pause_status FROM {self.table} "
            f"WHERE {where} ORDER BY paused_at", args={"run": run_id} if run_id else None).collect()
        return [r.asDict() for r in rows]

    def mark_restored(self, run_id: str, obj_id: str, kind: str) -> None:
        self.spark.sql(
            f"UPDATE {self.table} SET restored_at = current_timestamp() "
            "WHERE apply_run_id = :run AND job_id = :id AND setting_kind = :kind AND restored_at IS NULL",
            args={"run": run_id, "id": str(obj_id), "kind": kind})


def _status(obj) -> str:
    return obj.pause_status.value if obj.pause_status else "UNPAUSED"


def job_pause_state(w, job_id: str) -> dict[str, str]:
    """{setting_kind: current pause status} for the kinds this job has."""
    settings = w.jobs.get(int(job_id)).settings
    return {k: _status(getattr(settings, k)) for k in JOB_SETTING_KINDS if getattr(settings, k, None)}


def pause_job(w, ledger, run_id: str, job_id: str) -> list[str]:
    """Pause every unpaused schedule/trigger/continuous setting of a job. Only
    pause_status changes; already-paused settings are left alone (and not
    recorded, so restore never touches them). Returns the kinds paused."""
    settings = w.jobs.get(int(job_id)).settings
    changed = {}
    for kind in JOB_SETTING_KINDS:
        obj = getattr(settings, kind, None)
        if obj is None or _status(obj) == "PAUSED":
            continue
        ledger.record(run_id, job_id, kind, _status(obj))
        changed[kind] = dataclasses.replace(obj, pause_status=PauseStatus.PAUSED)
    if changed:
        w.jobs.update(job_id=int(job_id), new_settings=JobSettings(**changed))
    return list(changed)


def pipeline_running(pl) -> bool:
    latest = (pl.latest_updates or [None])[0]
    state = latest.state.value if latest and latest.state else None
    return state in ACTIVE_PIPELINE_UPDATE_STATES or getattr(getattr(pl, "state", None), "value", None) == "RUNNING"


def stop_pipeline(w, ledger, run_id: str, pipeline_id: str) -> bool:
    """Stop a running continuous pipeline, recording it first. False if it wasn't running."""
    if not pipeline_running(w.pipelines.get(pipeline_id)):
        return False
    ledger.record(run_id, pipeline_id, PIPELINE_KIND, "RUNNING")
    w.pipelines.stop(pipeline_id)
    return True


def restore(w, ledger, run_id: str | None = None) -> tuple[list[str], list[str]]:
    """Put back everything recorded and not yet restored (for one run, or all).
    Returns (restored, errors). A row is only marked restored once it is."""
    restored, errors = [], []
    for row in ledger.pending(run_id):
        rid, obj_id, kind, original = (row["apply_run_id"], row["job_id"], row["setting_kind"],
                                       row["original_pause_status"])
        label = f"{'pipeline' if kind == PIPELINE_KIND else 'job'} {obj_id} {kind}"
        try:
            if kind == PIPELINE_KIND:
                if original == "RUNNING":
                    w.pipelines.start_update(obj_id)
            else:
                obj = getattr(w.jobs.get(int(obj_id)).settings, kind, None)
                if obj is not None:
                    w.jobs.update(job_id=int(obj_id), new_settings=JobSettings(
                        **{kind: dataclasses.replace(obj, pause_status=PauseStatus(original))}))
            ledger.mark_restored(rid, obj_id, kind)
            restored.append(f"{label} -> {original}")
        except NotFound:
            ledger.mark_restored(rid, obj_id, kind)
            restored.append(f"{label}: no longer exists, nothing to restore")
        except Exception as e:
            errors.append(f"{label}: {str(e).splitlines()[0][:200]}")
    return restored, errors
