# Semantic Builder

A Databricks App that lets business users document their data in Unity Catalog without going through the workspace UI. They browse a node graph of catalogs, schemas, tables and columns and edit descriptions in place. Each edit becomes a change request: an approver reviews it, and a scheduled job applies it in a way that won't disrupt running pipelines.

```
 Business user ──edit──▶ change queue (Delta) ──approve──▶ apply job ──COMMENT / ALTER──▶ Unity Catalog
                         who · when · before · after       defers if the table's writers are running
```

## What's in it

| Path | What it is |
| --- | --- |
| `app/` | FastAPI backend (`app.py`, `server/`) and React frontend (`frontend/`). `frontend/dist` is committed, so deploying doesn't need Node. |
| `jobs/setup.py` | Creates the change-queue and `paused_writers` tables and grants the app's service principal access. |
| `jobs/apply_changes.py` | Applies approved changes on a schedule: per-table *defer* mode (default every 15 min) or a weekly *maintenance window*. |
| `jobs/restore_writers.py` | Puts back any writer jobs the apply job paused. Runs as the apply job's last task, and can be run by hand. |
| `jobs/writers.py` | Shared pause/restore logic and the `paused_writers` ledger. |
| `jobs/import_dictionary.py` | Loads the customer's data-dictionary table into the queue as reviewable batches. |
| `jobs/export_yaml.py` | Optional, off by default. Exports live descriptions to YAML and opens a GitHub pull request. |
| `jobs/seed_demo.py` | Optional. Builds a sample title-insurance estate to try the app on. |
| `app/server/queue_sql.py` | Insert/supersede/batch-review SQL shared by the app (SQL warehouse) and the import job (Spark). |
| `tests/` | pytest suite. Spark and the Databricks SDK are faked; queue SQL runs on DuckDB. |
| `databricks.yml`, `resources/` | The Databricks Asset Bundle: the app, all jobs and every setting as a variable. |

## How changes flow

1. **Edit.** A user changes a table or column description and submits it, optionally with a note. The app writes a `PENDING` row to `<staging_catalog>.<staging_schema>.metadata_change_requests` recording the requester (from their app sign-in), the time, the previous value and the proposed value. The app never runs DDL itself. A newer request for the same field supersedes the older one.
2. **Review.** Approvers, listed by email or workspace group, approve or reject requests with an optional note: one at a time, a selection, or a whole **batch** at once (see [Batch approval](#batch-approval)). Approving your own requests is disabled unless `allow_self_approval` is true.
3. **Apply.** The `semantic-builder-apply` job takes the approved changes. In the default *defer* mode, for each table it:
   - finds the table's writers two ways: jobs and pipelines in `system.access.table_lineage` (last 30 days), and the job behind each recent commit in the table's Delta history (`DESCRIBE HISTORY`). Lineage can lag by minutes and skips writes that have no source table; Delta history is immediate. The apply job ignores its own commits;
   - **defers** the change if any of them is running right now and records why ("Waiting: job 123 is running"). An approver can override this with *Apply even if busy*;
   - applies all of the table's column edits in **one commit** (`ALTER TABLE … ALTER COLUMN a COMMENT …, b COMMENT …`) and the table description in one more, retrying on commit conflicts;
   - writes the outcome back to the queue: `APPLIED`, `FAILED` (with the error, and a *Retry* button), or a deferral note.

   For writers that never pause on their own, use [maintenance window mode](#maintenance-window-mode) instead.
4. **History.** Every table description and column has a GitHub-style timeline in the app showing who proposed what, the word-level diff, who reviewed it and when it landed. If someone later changes a description outside the app, it's flagged as *Changed outside*.

### Why the queue exists

Every description change on a Delta table is a metadata commit to the table's transaction log. If a job writes to that table at the same moment, the write can fail with `MetadataChangedException`. A pipeline that runs every 15 minutes, around the clock, is almost always mid-write at some point, so the apply job waits for a gap. Batching all of a table's edits into one commit keeps the window small.

### Materialized views and streaming tables

Comments on these apply and survive a `REFRESH`, but they're reset when the pipeline redefines the object (`CREATE OR REPLACE` or a redeploy that sets its own comment). The apply job still applies the change but marks it with a warning so the pipeline owner can copy the text into the pipeline source. If a description later reverts, the app shows it as drift.

## Maintenance window mode

Defer mode needs gaps between writer runs, and there's still a small race between "nobody is running" and the `ALTER`. When the tables are written around the clock (for example every 15 minutes, 24/7), set `apply_mode: window` and schedule the job weekly. Each run then:

1. Collects the approved requests. **If there are none, it exits before touching any job.**
2. Finds each table's writers, the same way defer mode does (lineage plus Delta history, excluding the apply job itself). Jobs that have been deleted but still appear in the last 30 days of history are ignored.
3. For each writer job, records the current pause state of its `schedule`, `trigger` or `continuous` setting in `<staging_catalog>.<staging_schema>.paused_writers`, **then** pauses it. Only `pause_status` changes; nothing else in the job is rewritten, and jobs that were already paused are never touched.
4. Pipelines: a triggered pipeline is covered by pausing the jobs that run it. A **continuous** pipeline is left running unless `pause_pipelines: "true"`; its tables are deferred with *"Waiting: continuous pipeline <name> must be stopped manually or enable pause_pipelines"*. With `pause_pipelines` on, it is stopped (and recorded), then restarted afterwards.
5. Waits for runs already in flight, polling every 30 s for up to `max_wait_minutes` (default 30). **Running jobs are never cancelled.** At the deadline, only the tables whose writers are still running are deferred to the next window; the rest go ahead. Just before each table's commit it checks that table's writers once more.
6. Applies each table's edits with the same batching and retries as defer mode, and records which jobs were paused (*"Applied in maintenance window; paused jobs 123, 456"*).
7. Restores every job and pipeline it changed to its recorded state, in a `finally` block, so this happens even if the apply fails. The job's second task, `restore_writers` (`run_if: ALL_DONE`), then restores anything still marked unrestored in `paused_writers`, in case the first task died outright. You can run `jobs/restore_writers.py` by hand too, but not while a window is in progress.

A job you can see but can't manage (no `CAN_MANAGE`) can't be paused. Its tables are deferred with the reason, never applied unprotected. *Apply even if busy* still works in window mode, for a writer that is still running at the deadline.

`apply_dry_run: "true"` (or `--dry_run=true`) prints the planned DDL, which jobs and pipelines would be paused, which writers are running now, and which tables would be deferred. It makes no DDL, job or queue changes. Run it once before the first real window.

**Schedule and DST.** The FA targets use `apply_cron: "0 0 1 ? * SUN"` with `apply_timezone: America/Chicago`. Because the timezone observes daylight saving, the window is always 1:00 AM local. On the fall-back Sunday, 1:00 AM happens twice: `max_concurrent_runs: 1` keeps the runs from overlapping, and the second one finds nothing approved and exits without pausing anything. In the app, deferred requests show the next window, calculated from the apply job's own schedule (*"Next window: Sun, Nov 1, 1:00 AM CST"*). *Run now* runs the same pause, apply and restore flow, after a confirmation.

## Dictionary import

`semantic-builder-import` (`jobs/import_dictionary.py`) reads the customer's dictionary Delta table (`dictionary_table`) and turns it into PENDING requests, as if each row had been typed into the app. It runs daily by default (`import_cron: "0 0 6 * * ?"`, `America/Chicago`; pause it with `import_pause_status: PAUSED`), so approvers have the week to review before the Sunday window. With `dictionary_table` empty it does nothing.

`dictionary_column_map` maps our fields to the table's columns, so a renamed column is a config change rather than a code change:

```json
{"catalog": "catalog_name", "schema": "schema_name", "table": "table_name", "column": "column_name",
 "description": "description", "plan_id": "plan_id", "week_of": "week_of"}
```

A NULL `column` means the row is the table description. `plan_id` and `week_of` are optional (set them to `""` to drop them).

- Only the mapped columns are read. The dictionary's source / source-tag field is deliberately ignored.
- Rows outside `scope`, blank descriptions, and tables or columns that no longer exist are skipped and counted in the run output.
- A row whose trimmed text matches what's live in Unity Catalog is skipped.
- Each real difference becomes a request with `requested_by = 'dictionary-import'`, `batch_id = plan_id` (or one generated id per run), `note = 'Dictionary import, week_of <week_of>'`, and the live value as `previous_value`. As in the app, it supersedes any open request for the same field.
- **Idempotent:** a field is skipped if a request with the same text is already open, or was rejected or failed. Re-running on unchanged input creates nothing, and a value an approver rejected doesn't come back every day. If several rows target one field, the latest `week_of` wins.
- Rows are written in multi-row statements of 500. `import_dry_run: "true"` prints per-table counts and writes nothing.

## Batch approval

The *To review* tab lists each batch with more than one pending request: one dictionary import (*"Dictionary import · week_of 2026-10-26"*) or one save in the app. Each card shows the number of requests, tables, columns and table descriptions, with **Approve batch** / **Reject batch** buttons (after a confirmation) and a filter to show just that batch. The counts come from the whole queue, so a 4,000-request import shows its true size. Reviewing request by request still works below. The endpoint is `POST /api/changes/review_batch` (`{batch_id, decision, note}`, approvers only): one `UPDATE` of every PENDING row in the batch. The self-approval guard still applies, and since imported requests come from `dictionary-import`, any approver can approve them.

## Optional: YAML export and GitHub pull request

Off by default (`github_export_enabled: "false"`). When it's `"true"`, a step after `apply_changes` writes the descriptions now live in each in-scope schema to YAML in the DictionarySpec layout. That layout is a comment header (`plan_id`, `week_of`, `catalog`, `schema`), then `variables`, `targets` (from `export_targets`), `catalog: ${var.catalog}`, `schema: ${var.schema}`, `schema_description`, `schema_tags`, `source` and `tables` (each with `description`, `columns: {name: {description, tags}}` and `tags`). Long descriptions are wrapped.

It then opens a pull request against `github_base_branch` and never pushes to it directly. Files go to `github_path_template` (default `metadata/{schema}.yaml`). The token is read from `github_secret_scope` / `github_secret_key` and never logged. For GitHub Enterprise Server, set `github_api_url` to `https://<host>/api/v3`. If nothing changed, no PR is opened.

> **Apply once.** If the customer's own GitHub Action also applies metadata when this PR merges, every change is applied twice. Pick one side to apply; the other should only keep the record.

## First American targets

`fa_dev`, `fa_nonprod` and `fa_prod` are in `databricks.yml`:

| Target | Scope |
| --- | --- |
| `fa_dev` | `st2_lakehouse_dev.ewb_salesforce_uat` |
| `fa_nonprod` | `st2_lakehouse_nonprod.ewb_salesforce_uat` |
| `fa_prod` | `st2_lakehouse_prod.ewb_salesforce_prod` |

All three run in window mode on Sunday at 1:00 AM `America/Chicago`. Anything FA must supply is a `<PLACEHOLDER>`: workspace host, SQL warehouse ID, staging catalog, approvers group, dictionary table, and the `run_as` service principal (commented out). Before the first real window, deploy with `apply_dry_run: "true"`, run the apply job, and read its output.

## Deploy to a new workspace

You need a SQL warehouse, a catalog you can create a schema in (for the queue), and the catalogs you want the business to edit.

1. In `databricks.yml`, copy the `customer` target and fill in:

   | Variable | Example |
   | --- | --- |
   | `warehouse_id` | `abc123def456` |
   | `scope` | `title_prod, escrow, analytics.reporting_*`: catalogs, `catalog.schema`, or `catalog.schema_glob*` |
   | `staging_catalog` | `governance` |
   | `approvers` | `data-stewards` (a group) or `jane@corp.com,raj@corp.com` |

   Optional: `apply_cron`, `apply_pause_status`, `coverage_target` (default `0.8`), `allow_self_approval`, `check_writers`, `app_name`.

2. Deploy, run setup, and start the app:

   ```bash
   databricks bundle deploy -t customer -p <profile>
   databricks bundle run semantic_builder_setup -t customer -p <profile>
   databricks bundle run semantic_builder -t customer -p <profile>
   ```

3. **Permissions**
   - *App service principal* (granted by the setup job): `USE CATALOG` + `BROWSE` on each in-scope catalog, and read/write on the queue schema. The app only reads metadata and writes to the queue.
   - *Apply job identity* (the deployer by default): needs `MODIFY` on the in-scope tables (or ownership), plus `SELECT` on `system.access.table_lineage` for the writer check. In production, set `run_as` on the target to a dedicated service principal. Without lineage access the job still works, using Delta history alone plus retries.
   - **Window mode** also needs `CAN_MANAGE` on every writer job, to pause and unpause it, and on the continuous pipelines if `pause_pipelines` is on. Without it, that job's tables are deferred rather than applied unprotected.
   - *Import job identity*: `SELECT` on the dictionary table, `BROWSE` on the in-scope catalogs, and write on the queue schema.
   - *GitHub export* (if enabled): `READ` on the secret scope, and a token that can create branches and pull requests in `github_repo`.
   - If the setup job can't grant on a catalog it doesn't own, it prints the failed `GRANT` statements so a catalog owner can run them.

## Try it with sample data

```bash
databricks bundle run semantic_builder_seed_demo -t dev -p <profile>
```

This creates `fa_title_production`, `fa_escrow`, `fa_claims`, `fa_salesforce`, `fa_servicenow`, `fa_workday` and `fa_reporting` (including a view and a materialized view) in `demo_catalog`, documented unevenly on purpose. Re-running it resets those schemas.

## Local development

```bash
# backend (uses your CLI profile; SB_LOCAL_USER stands in for the SSO header)
cd app
DATABRICKS_PROFILE=<profile> DATABRICKS_WAREHOUSE_ID=<id> SB_SCOPE="<catalog>" \
SB_STAGING_CATALOG=<catalog> SB_APPROVERS=you@corp.com SB_ALLOW_SELF_APPROVAL=true \
SB_LOCAL_USER=you@corp.com uvicorn app:app --port 8000

# frontend with hot reload (proxies /api to :8000)
cd app/frontend && npm install && npm run dev

# rebuild the committed bundle after frontend changes
npm run build
```

Tests (no workspace needed):

```bash
pip install pytest duckdb httpx fastapi databricks-sdk pyyaml
pytest tests
```

## Not yet built

- Domains, tags and other metadata beyond descriptions (the queue's `field` column is there for it)
- AI-suggested descriptions (`ai_query`) for users to edit
- An export layout confirmed against FA's DictionarySpec example file (the current one follows the written spec)
- Approval routing per catalog or schema (approvers are global today)
