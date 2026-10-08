"""Semantic Builder — a business-friendly editor for Unity Catalog descriptions.

React SPA (frontend/dist) + this FastAPI backend. Reads come from
information_schema; edits are queued as change requests in a Delta table and
applied later by a scheduled job, after approval.
"""
import os

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server import catalog, changes
from server.config import (
    ALLOW_SELF_APPROVAL,
    APP_TITLE,
    APPLY_JOB_ID,
    COVERAGE_TARGET,
    SCOPE,
    STAGING_TABLE,
    get_workspace_client,
    workspace_host,
)
from server.identity import current_user, is_approver

app = FastAPI(title=APP_TITLE)


@app.exception_handler(PermissionError)
def _forbidden(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=403)


@app.exception_handler(LookupError)
def _missing(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=404)


@app.exception_handler(ValueError)
def _bad(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


def _require_approver(request: Request) -> dict:
    user = current_user(request)
    if not is_approver(user["email"]):
        raise PermissionError("Only approvers can do that")
    return user


# ---------- identity + config ----------

@app.get("/api/me")
def me(request: Request):
    user = current_user(request)
    return {**user, "is_approver": is_approver(user["email"])}


@app.get("/api/config")
def config():
    return {
        "title": APP_TITLE,
        "scope": SCOPE,
        "staging_table": STAGING_TABLE.replace("`", ""),
        "coverage_target": COVERAGE_TARGET,
        "allow_self_approval": ALLOW_SELF_APPROVAL,
        "apply_job_id": APPLY_JOB_ID or None,
        "workspace_host": workspace_host(),
    }


# ---------- graph ----------

@app.get("/api/graph/catalogs")
def graph_catalogs():
    return catalog.list_catalogs()


@app.get("/api/graph/catalogs/{c}/schemas")
def graph_schemas(c: str):
    return catalog.list_schemas(c)


@app.get("/api/graph/catalogs/{c}/schemas/{s}/tables")
def graph_tables(c: str, s: str):
    return catalog.list_tables(c, s)


@app.get("/api/tables/{c}/{s}/{t}")
def table(c: str, s: str, t: str):
    return catalog.table_detail(c, s, t)


@app.get("/api/search")
def search(q: str = Query("")):
    return catalog.search(q)


@app.post("/api/refresh")
def refresh():
    catalog.invalidate()
    return {"ok": True}


# ---------- change requests ----------

@app.post("/api/changes")
def submit(request: Request, body: dict = Body(...)):
    user = current_user(request)
    return changes.submit(user["email"], body.get("changes", []), body.get("note"))


@app.get("/api/changes")
def list_changes(request: Request, status: str = Query(""), mine: bool = False, limit: int = 200):
    statuses = [s.strip().upper() for s in status.split(",") if s.strip()]
    requested_by = current_user(request)["email"] if mine else None
    return changes.list_requests(statuses or None, requested_by, min(limit, 1000))


@app.get("/api/changes/summary")
def summary():
    return changes.summary()


@app.get("/api/history")
def history(catalog: str, schema: str, table: str, column: str | None = None):
    return changes.history(catalog, schema, table, column)


@app.post("/api/changes/review")
def review(request: Request, body: dict = Body(...)):
    user = _require_approver(request)
    return changes.review(user["email"], body.get("ids", []), body.get("decision", ""), body.get("note"))


@app.post("/api/changes/{request_id}/withdraw")
def withdraw(request: Request, request_id: str):
    return {"updated": changes.withdraw(current_user(request)["email"], request_id)}


@app.post("/api/changes/{request_id}/force")
def force(request: Request, request_id: str):
    _require_approver(request)
    return {"updated": changes.force(request_id)}


@app.post("/api/changes/{request_id}/retry")
def retry(request: Request, request_id: str):
    _require_approver(request)
    return {"updated": changes.retry(request_id)}


# ---------- apply job ----------

@app.get("/api/apply/runs")
def apply_runs():
    if not APPLY_JOB_ID:
        return []
    w = get_workspace_client()
    out = []
    for r in w.jobs.list_runs(job_id=int(APPLY_JOB_ID), limit=5):
        state = r.state
        out.append({
            "run_id": r.run_id,
            "start_time": r.start_time,
            "end_time": r.end_time,
            "life_cycle_state": state.life_cycle_state.value if state and state.life_cycle_state else None,
            "result_state": state.result_state.value if state and state.result_state else None,
            "url": r.run_page_url,
        })
        if len(out) >= 5:
            break
    return out


@app.post("/api/apply/run")
def apply_now(request: Request):
    _require_approver(request)
    if not APPLY_JOB_ID:
        raise HTTPException(400, "Apply job is not configured (SB_APPLY_JOB_ID)")
    run = get_workspace_client().jobs.run_now(job_id=int(APPLY_JOB_ID))
    return {"run_id": run.run_id}


# ---------- SPA ----------

_DIST = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.isdir(_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        candidate = os.path.realpath(os.path.join(_DIST, full_path))
        if full_path and candidate.startswith(os.path.realpath(_DIST) + os.sep) and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(_DIST, "index.html"))
