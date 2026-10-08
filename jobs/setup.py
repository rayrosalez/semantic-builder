"""One-time (idempotent) setup: the change-request table + grants for the app.

Parameters
  --staging_catalog / --staging_schema  where the queue lives
  --app_sp      application id of the app's service principal
  --scope       the app's SB_SCOPE (catalogs the app should be able to read)
"""
import argparse

from pyspark.sql import SparkSession

p = argparse.ArgumentParser()
p.add_argument("--staging_catalog", required=True)
p.add_argument("--staging_schema", required=True)
p.add_argument("--app_sp", required=True)
p.add_argument("--scope", default="")
args = p.parse_args()

spark = SparkSession.builder.getOrCreate()
cat, sch, sp = f"`{args.staging_catalog}`", f"`{args.staging_schema}`", f"`{args.app_sp}`"
table = f"{cat}.{sch}.`metadata_change_requests`"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {cat}.{sch} "
          "COMMENT 'Semantic Builder: queued metadata change requests'")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {table} (
  request_id      STRING    NOT NULL COMMENT 'Unique id of this change request',
  batch_id        STRING    COMMENT 'Requests submitted together from one save share a batch id',
  entity_type     STRING    COMMENT 'TABLE (table description) or COLUMN (column description)',
  catalog_name    STRING    COMMENT 'Target catalog',
  schema_name     STRING    COMMENT 'Target schema',
  table_name      STRING    COMMENT 'Target table, view, materialized view or streaming table',
  column_name     STRING    COMMENT 'Target column; NULL for a table description',
  table_type      STRING    COMMENT 'information_schema table_type at request time',
  field           STRING    COMMENT 'Which metadata field is changing (COMMENT today)',
  previous_value  STRING    COMMENT 'Live value when the request was submitted',
  proposed_value  STRING    COMMENT 'Requested new value',
  note            STRING    COMMENT 'Why the requester made the change',
  status          STRING    COMMENT 'PENDING, APPROVED, REJECTED, APPLIED, FAILED, WITHDRAWN or SUPERSEDED',
  requested_by    STRING    COMMENT 'Email of the requester (from the app sign-in)',
  requested_at    TIMESTAMP COMMENT 'When it was requested',
  reviewed_by     STRING    COMMENT 'Email of the approver or rejecter',
  reviewed_at     TIMESTAMP COMMENT 'When it was reviewed',
  review_note     STRING    COMMENT 'Reviewer comment',
  force_apply     BOOLEAN   COMMENT 'Approver override: apply even if a writer to the table is running',
  apply_attempts  INT       COMMENT 'Times the apply job tried and failed to run the DDL',
  deferrals       INT       COMMENT 'Times the apply job postponed this change because the table was busy',
  last_attempt_at TIMESTAMP COMMENT 'Last time the apply job looked at this request',
  apply_message   STRING    COMMENT 'Latest note from the apply job (deferral reason, error, warning)',
  applied_at      TIMESTAMP COMMENT 'When the change landed in Unity Catalog',
  updated_at      TIMESTAMP COMMENT 'Last time this row changed'
)
CLUSTER BY (catalog_name, schema_name, table_name)
COMMENT 'Semantic Builder change queue: who changed which description, from what, to what, and its status'
TBLPROPERTIES ('delta.enableDeletionVectors' = 'true', 'delta.enableRowTracking' = 'true')
""")

grants = [
    f"GRANT USE CATALOG ON CATALOG {cat} TO {sp}",
    f"GRANT USE SCHEMA, SELECT, MODIFY ON SCHEMA {cat}.{sch} TO {sp}",
]
for c in sorted({s.strip().split(".")[0] for s in args.scope.split(",") if s.strip()}):
    grants += [f"GRANT USE CATALOG ON CATALOG `{c}` TO {sp}",
               f"GRANT BROWSE ON CATALOG `{c}` TO {sp}"]

for g in grants:
    try:
        spark.sql(g)
        print("ok   ", g)
    except Exception as e:  # e.g. the job identity doesn't own a scope catalog
        print("FAIL ", g, "->", str(e).splitlines()[0][:200])

print(f"Setup complete: {table}")
