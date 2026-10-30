"""Restore jobs/pipelines the apply job paused but never put back.

Runs as the apply job's last task (run_if: ALL_DONE), so even if
apply_changes crashed mid-window the writers come back. Safe to run by hand
when no apply run is in progress: it only touches rows in paused_writers with
restored_at IS NULL, and restores each to the pause status recorded before it
was changed. (During a live window it would unpause writers early.)

  --run_id   only this apply run's rows (default: every unrestored row, so an
             earlier run that crashed is cleaned up too)
"""
import argparse
import inspect
import os
import sys

# Serverless runs the file via exec(compile(...)) with no __file__; the code
# object still carries the real path.
sys.path.insert(0, os.path.dirname(os.path.abspath(inspect.currentframe().f_code.co_filename)))
import writers as wr  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--staging_catalog", required=True)
    p.add_argument("--staging_schema", required=True)
    p.add_argument("--run_id", default="")
    # parse_known_args: "run now with different parameters" passes the apply task's
    # parameters to every python task in the job.
    return p.parse_known_args(argv)[0]


def run(w, ledger, run_id: str | None = None) -> list[str]:
    restored, errors = wr.restore(w, ledger, run_id or None)
    for line in restored:
        print("restored", line)
    if errors:
        raise RuntimeError("Could not restore: " + "; ".join(errors))
    print(f"{len(restored)} setting(s) restored" if restored else "Nothing to restore")
    return restored


def main(argv=None) -> None:
    from databricks.sdk import WorkspaceClient
    from pyspark.sql import SparkSession

    args = parse_args(argv)
    table = f"`{args.staging_catalog}`.`{args.staging_schema}`.`paused_writers`"
    run(WorkspaceClient(), wr.SparkLedger(SparkSession.builder.getOrCreate(), table), args.run_id)


if __name__ == "__main__":
    main()
