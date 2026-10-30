import yaml

from export_yaml import render

LONG = ("Unique identifier of the Salesforce opportunity as assigned by the source CRM; joins to "
        "opportunity_line_items.opportunity_id and is stable across stage changes.")


def test_render_round_trips_through_yaml():
    tables = {
        "opportunities": {"description": LONG, "columns": {
            "opportunity_id": "Customer's opportunity id: never reused",
            "Amount USD": None,
            "notes": "Line one\nLine two",
            "flag": "true",
        }},
        "empty_table": {"description": None, "columns": {}},
    }
    targets = {"dev": {"catalog": "st2_lakehouse_dev", "schema": "ewb_salesforce_uat"},
               "prod": {"catalog": "st2_lakehouse_prod", "schema": "ewb_salesforce_prod"}}
    text = render("st2_lakehouse_prod", "ewb_salesforce_prod", "Salesforce, curated", tables, targets,
                  "plan-42", "2026-10-26", "semantic-builder")
    assert text.startswith("# plan_id: plan-42\n# week_of: 2026-10-26\n# catalog: st2_lakehouse_prod\n")
    assert max(len(line) for line in text.splitlines()) <= 100  # long descriptions are wrapped
    doc = yaml.safe_load(text)
    assert doc["catalog"] == "${var.catalog}" and doc["schema"] == "${var.schema}"
    assert doc["targets"]["prod"]["variables"] == {"catalog": "st2_lakehouse_prod", "schema": "ewb_salesforce_prod"}
    assert doc["schema_description"] == "Salesforce, curated" and doc["schema_tags"] == {}
    opp = doc["tables"]["opportunities"]
    assert opp["description"] == LONG and opp["tags"] == {}
    assert opp["columns"]["opportunity_id"] == {"description": "Customer's opportunity id: never reused", "tags": {}}
    assert opp["columns"]["Amount USD"]["description"] == ""
    assert opp["columns"]["notes"]["description"] == "Line one\nLine two"
    assert opp["columns"]["flag"]["description"] == "true"
    assert doc["tables"]["empty_table"] == {"description": "", "columns": {}, "tags": {}}
