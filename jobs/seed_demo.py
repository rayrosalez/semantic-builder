"""Optional: build a sample title-insurance data estate to explore in the app.

Creates <prefix>* schemas in --catalog with deliberately uneven documentation
(some areas fully described, some half, some bare) so the graph has something
to point at. Re-running resets those schemas to this starting state (it never
touches anything outside them).
Not needed for a real deployment — point SB_SCOPE at your own catalogs instead.
"""
import argparse
import time

from pyspark.sql import SparkSession

p = argparse.ArgumentParser()
p.add_argument("--catalog", required=True)
p.add_argument("--prefix", default="fa_")
p.add_argument("--warehouse_id", default="")
args = p.parse_args()
spark = SparkSession.builder.getOrCreate()


def lit(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


# schema -> (schema comment, {table: (table comment, [(column, type, comment)])})
# A None comment is left undocumented on purpose.
ESTATE = {
    "title_production": ("Core title & settlement production system (order to policy).", {
        "title_orders": ("One row per title order opened by a branch or agent.", [
            ("order_id", "STRING", "Unique title order number assigned at open."),
            ("branch_id", "STRING", "Branch that owns the order."),
            ("agent_id", "STRING", "Issuing agent, when the order came through the agency channel."),
            ("property_id", "STRING", "Subject property; joins to properties.property_id."),
            ("transaction_type", "STRING", "PURCHASE, REFINANCE, EQUITY or COMMERCIAL."),
            ("opened_at", "TIMESTAMP", "When the order was opened."),
            ("closed_at", "TIMESTAMP", "Settlement date; NULL while the order is open."),
            ("liability_amount", "DECIMAL(18,2)", "Proposed policy liability in USD."),
            ("status", "STRING", "OPEN, CLOSED, CANCELLED or ON_HOLD."),
        ]),
        "policies": ("Issued owner's and lender's title policies.", [
            ("policy_number", "STRING", "Policy number as printed on the jacket."),
            ("order_id", "STRING", "Order the policy was issued from."),
            ("policy_type", "STRING", "OWNER or LENDER."),
            ("form_code", "STRING", "ALTA form the policy was written on."),
            ("effective_date", "DATE", "Policy effective date."),
            ("coverage_amount", "DECIMAL(18,2)", "Insured amount in USD."),
            ("premium_amount", "DECIMAL(18,2)", "Gross premium charged."),
            ("underwriter_code", "STRING", None),
        ]),
        "commitments": ("Title commitments issued ahead of closing.", [
            ("commitment_id", "STRING", "Commitment number."),
            ("order_id", "STRING", "Owning title order."),
            ("issued_at", "TIMESTAMP", "When the commitment went out."),
            ("revision", "INT", "Revision number; 0 is the original."),
            ("requirements_count", "INT", None),
        ]),
        "exceptions": (None, [
            ("exception_id", "STRING", None),
            ("commitment_id", "STRING", "Commitment the exception appears on."),
            ("schedule", "STRING", None),
            ("exception_text", "STRING", None),
            ("cleared", "BOOLEAN", None),
        ]),
        "properties": ("Subject properties referenced by orders.", [
            ("property_id", "STRING", "Internal property key."),
            ("apn", "STRING", "Assessor parcel number."),
            ("street_address", "STRING", "Street address line."),
            ("city", "STRING", "City."),
            ("state", "STRING", "Two-letter state code."),
            ("county_fips", "STRING", "County FIPS code."),
            ("property_type", "STRING", "SFR, CONDO, MULTI_FAMILY, LAND or COMMERCIAL."),
        ]),
    }),
    "escrow": ("Escrow and settlement funds.", {
        "escrow_files": ("Escrow file per closing.", [
            ("escrow_file_id", "STRING", "Escrow file number."),
            ("order_id", "STRING", "Related title order."),
            ("escrow_officer", "STRING", None),
            ("opened_at", "TIMESTAMP", "When escrow opened."),
            ("scheduled_close", "DATE", None),
            ("status", "STRING", None),
        ]),
        "disbursements": (None, [
            ("disbursement_id", "STRING", None),
            ("escrow_file_id", "STRING", None),
            ("payee_name", "STRING", None),
            ("amount", "DECIMAL(18,2)", None),
            ("method", "STRING", None),
            ("disbursed_at", "TIMESTAMP", None),
        ]),
        "wire_instructions": ("Verified outbound wire instructions. Sensitive.", [
            ("instruction_id", "STRING", "Instruction id."),
            ("escrow_file_id", "STRING", "Escrow file."),
            ("bank_name", "STRING", None),
            ("routing_number", "STRING", None),
            ("account_last4", "STRING", "Last four digits only; full number is never stored here."),
            ("verified_by", "STRING", None),
            ("verified_at", "TIMESTAMP", None),
        ]),
        "earnest_money": (None, [
            ("deposit_id", "STRING", None),
            ("escrow_file_id", "STRING", None),
            ("amount", "DECIMAL(18,2)", None),
            ("received_at", "TIMESTAMP", None),
        ]),
    }),
    "claims": ("Title claims and loss reserves.", {
        "claims": ("Claims filed against issued policies.", [
            ("claim_id", "STRING", "Claim number."),
            ("policy_number", "STRING", "Policy the claim is filed under."),
            ("reported_at", "TIMESTAMP", "Date the claim was reported."),
            ("cause_code", "STRING", None),
            ("status", "STRING", "OPEN, CLOSED_PAID, CLOSED_DENIED or LITIGATION."),
            ("adjuster_id", "STRING", None),
        ]),
        "claim_payments": (None, [
            ("payment_id", "STRING", None),
            ("claim_id", "STRING", None),
            ("amount", "DECIMAL(18,2)", None),
            ("paid_at", "TIMESTAMP", None),
            ("payment_type", "STRING", None),
        ]),
        "loss_reserves": ("Monthly case and IBNR reserve snapshots.", [
            ("claim_id", "STRING", "Claim; NULL for IBNR rows."),
            ("as_of_month", "DATE", "Reserve snapshot month."),
            ("case_reserve", "DECIMAL(18,2)", None),
            ("ibnr_reserve", "DECIMAL(18,2)", None),
        ]),
    }),
    "salesforce": ("Salesforce CRM, ingested with Lakeflow Connect.", {
        "account": ("Salesforce Account object.", [
            ("id", "STRING", "Salesforce record id."),
            ("name", "STRING", "Account name."),
            ("type", "STRING", None),
            ("industry", "STRING", None),
            ("owner_id", "STRING", None),
            ("billing_state", "STRING", None),
            ("last_modified_date", "TIMESTAMP", None),
        ]),
        "opportunity": (None, [
            ("id", "STRING", None),
            ("account_id", "STRING", None),
            ("stage_name", "STRING", None),
            ("amount", "DECIMAL(18,2)", None),
            ("close_date", "DATE", None),
            ("probability", "DOUBLE", None),
        ]),
        "contact": (None, [
            ("id", "STRING", None),
            ("account_id", "STRING", None),
            ("email", "STRING", None),
            ("title", "STRING", None),
        ]),
    }),
    "servicenow": (None, {
        "incident": (None, [
            ("sys_id", "STRING", None), ("number", "STRING", None), ("priority", "STRING", None),
            ("state", "STRING", None), ("assignment_group", "STRING", None),
            ("opened_at", "TIMESTAMP", None), ("resolved_at", "TIMESTAMP", None),
        ]),
        "change_request": (None, [
            ("sys_id", "STRING", None), ("number", "STRING", None), ("risk", "STRING", None),
            ("start_date", "TIMESTAMP", None), ("end_date", "TIMESTAMP", None),
        ]),
        "cmdb_ci": (None, [
            ("sys_id", "STRING", None), ("name", "STRING", None), ("class_name", "STRING", None),
            ("owned_by", "STRING", None),
        ]),
    }),
    "workday": ("Workday HCM, ingested with Lakeflow Connect.", {
        "worker": ("Current and former workers.", [
            ("worker_id", "STRING", "Workday worker id."),
            ("hire_date", "DATE", "Original hire date."),
            ("termination_date", "DATE", None),
            ("cost_center_id", "STRING", None),
            ("job_family", "STRING", None),
        ]),
        "cost_center": (None, [
            ("cost_center_id", "STRING", None),
            ("name", "STRING", None),
            ("business_unit", "STRING", None),
        ]),
    }),
}

for schema, (schema_comment, tables) in ESTATE.items():
    s = f"`{args.catalog}`.`{args.prefix}{schema}`"
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {s}")
    if schema_comment:
        spark.sql(f"COMMENT ON SCHEMA {s} IS {lit(schema_comment)}")
    for table, (comment, cols) in tables.items():
        col_sql = ", ".join(
            f"`{c}` {t}" + (f" COMMENT {lit(d)}" if d else "")
            for c, t, d in cols)
        tbl_comment = f" COMMENT {lit(comment)}" if comment else ""
        spark.sql(f"CREATE OR REPLACE TABLE {s}.`{table}` ({col_sql}){tbl_comment}")
        print("created", f"{s}.{table}")

# A reporting layer with a view (and, via seed_mv.sql, a materialized view) so the
# pipeline-managed path (apply + "add to source" warning) can be seen in the app.
r = f"`{args.catalog}`.`{args.prefix}reporting`"
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {r}")
spark.sql(f"COMMENT ON SCHEMA {r} IS {lit('Curated reporting layer.')}")
tp = f"`{args.catalog}`.`{args.prefix}title_production`"
spark.sql(f"""CREATE OR REPLACE VIEW {r}.`open_orders_v` AS
  SELECT order_id, branch_id, transaction_type, opened_at, liability_amount
  FROM {tp}.title_orders WHERE status = 'OPEN'""")
print("created", f"{r}.open_orders_v")

# Serverless job compute can't create materialized views, so this one statement
# goes to the SQL warehouse.
if args.warehouse_id:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    res = w.statement_execution.execute_statement(
        warehouse_id=args.warehouse_id, wait_timeout="0s",
        statement=f"""CREATE OR REPLACE MATERIALIZED VIEW {r}.`order_volume_daily`
          COMMENT 'Daily title order volume and liability by branch.'
          AS SELECT date(opened_at) AS order_date, branch_id, transaction_type,
                    count(*) AS orders, sum(liability_amount) AS liability
             FROM {tp}.title_orders GROUP BY ALL""")
    while res.status.state.value in ("PENDING", "RUNNING"):
        time.sleep(5)
        res = w.statement_execution.get_statement(res.statement_id)
    if res.status.state.value == "SUCCEEDED":
        print("created", f"{r}.order_volume_daily")
    else:
        print("skipped materialized view:", res.status.error.message if res.status.error else res.status.state)
