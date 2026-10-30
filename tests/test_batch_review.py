import pytest

from server import changes, queue_sql

Q = "`gov`.`sb`.`metadata_change_requests`"


def seed(duck, batch, n, requested_by, status="PENDING", start=0):
    rows = [{"request_id": f"{batch}-{i}", "batch_id": batch, "catalog": "fa", "schema": "sales_core",
             "table": f"t{i % 3}", "column": f"c{i}", "table_type": "MANAGED", "previous_value": None,
             "proposed_value": f"text {i}", "note": None, "requested_by": requested_by}
            for i in range(start, start + n)]
    st, params = queue_sql.insert(Q, rows)
    duck.query(st, params)
    if status != "PENDING":
        ids = ", ".join(f"'{r['request_id']}'" for r in rows)
        duck.query(f"UPDATE gov.sb.metadata_change_requests SET status = '{status}' WHERE request_id IN ({ids})")


@pytest.fixture
def queue(duck, monkeypatch):
    monkeypatch.setattr(changes.sql, "query", duck.query)
    monkeypatch.setattr(changes.cat, "invalidate", lambda *a: None)
    monkeypatch.setattr(changes, "STAGING_TABLE", Q)
    monkeypatch.setattr(changes, "ALLOW_SELF_APPROVAL", False)
    return duck


def test_batch_approval_touches_only_pending_rows_in_the_batch(queue):
    seed(queue, "plan-42", 1200, "dictionary-import")
    seed(queue, "plan-42", 5, "dictionary-import", status="REJECTED", start=1200)
    seed(queue, "other", 7, "someone@corp.com")

    res = changes.review_batch("steward@corp.com", "plan-42", "approve", "  looks good ")
    assert res == {"updated": 1200, "skipped_self": 0}

    rows = queue.rows()
    plan = [r for r in rows if r["batch_id"] == "plan-42"]
    assert sum(r["status"] == "APPROVED" for r in plan) == 1200
    assert sum(r["status"] == "REJECTED" for r in plan) == 5
    assert all(r["reviewed_by"] == "steward@corp.com" and r["review_note"] == "looks good"
               for r in plan if r["status"] == "APPROVED")
    assert all(r["status"] == "PENDING" and r["reviewed_by"] is None for r in rows if r["batch_id"] == "other")


def test_dictionary_import_batch_is_approvable_with_self_approval_off(queue):
    # The requester is "dictionary-import", never the approver, so the self-approval
    # guard (on here) doesn't block any approver.
    seed(queue, "plan-42", 3, "dictionary-import")
    assert changes.review_batch("anyone@corp.com", "plan-42", "approve", None)["updated"] == 3


def test_self_approval_guard_still_applies_to_batches(queue):
    seed(queue, "mine", 2, "me@corp.com")
    res = changes.review_batch("me@corp.com", "mine", "approve", None)
    assert res == {"updated": 0, "skipped_self": 2}


def test_reject_batch(queue):
    seed(queue, "plan-42", 4, "dictionary-import")
    assert changes.review_batch("s@corp.com", "plan-42", "reject", "wrong week")["updated"] == 4
    assert {r["status"] for r in queue.rows()} == {"REJECTED"}


def test_endpoint_is_approver_only(monkeypatch):
    from fastapi.testclient import TestClient

    import app as app_module
    monkeypatch.setattr(app_module, "is_approver", lambda email: False)
    client = TestClient(app_module.app)
    r = client.post("/api/changes/review_batch", json={"batch_id": "plan-42", "decision": "approve"},
                    headers={"x-forwarded-email": "nobody@corp.com"})
    assert r.status_code == 403
