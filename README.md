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
| `jobs/setup.py` | Creates the change-queue table and grants the app's service principal access. |
| `jobs/apply_changes.py` | Applies approved changes on a schedule (default every 15 min). |
| `jobs/seed_demo.py` | Optional. Builds a sample title-insurance estate to try the app on. |
| `databricks.yml`, `resources/` | The Databricks Asset Bundle: the app, all three jobs and every setting as a variable. |

## How changes flow

1. **Edit.** A user changes a table or column description and submits it, optionally with a note. The app writes a `PENDING` row to `<staging_catalog>.<staging_schema>.metadata_change_requests` recording the requester (from their app sign-in), the time, the previous value and the proposed value. The app never runs DDL itself. A newer request for the same field supersedes the older one.
2. **Review.** Approvers, listed by email or workspace group, approve or reject requests in bulk with an optional note. Approving your own requests is disabled unless `allow_self_approval` is true.
3. **Apply.** The `semantic-builder-apply` job takes the approved changes and, for each table:
   - finds the table's writers two ways: jobs and pipelines in `system.access.table_lineage` (last 30 days), and the job behind each recent commit in the table's Delta history (`DESCRIBE HISTORY`). Lineage can lag by minutes and skips writes that have no source table; Delta history is immediate. The apply job ignores its own commits;
   - **defers** the change if any of them is running right now and records why ("Waiting: job 123 is running"). An approver can override this with *Apply even if busy*;
   - applies all of the table's column edits in **one commit** (`ALTER TABLE … ALTER COLUMN a COMMENT …, b COMMENT …`) and the table description in one more, retrying on commit conflicts;
   - writes the outcome back to the queue: `APPLIED`, `FAILED` (with the error, and a *Retry* button), or a deferral note.
4. **History.** Every table description and column has a GitHub-style timeline in the app showing who proposed what, the word-level diff, who reviewed it and when it landed. If someone later changes a description outside the app, it's flagged as *Changed outside*.

### Why the queue exists

Every description change on a Delta table is a metadata commit to the table's transaction log. If a job writes to that table at the same moment, the write can fail with `MetadataChangedException`. A pipeline that runs every 15 minutes, around the clock, is almost always mid-write at some point, so the apply job waits for a gap. Batching all of a table's edits into one commit keeps the window small.

### Materialized views and streaming tables

Comments on these apply and survive a `REFRESH`, but they're reset when the pipeline redefines the object (`CREATE OR REPLACE` or a redeploy that sets its own comment). The apply job still applies the change but marks it with a warning so the pipeline owner can copy the text into the pipeline source. If a description later reverts, the app shows it as drift.

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

## Not yet built

- Domains, tags and other metadata beyond descriptions (the queue's `field` column is there for it)
- AI-suggested descriptions (`ai_query`) for users to edit
- Approval routing per catalog or schema (approvers are global today)
