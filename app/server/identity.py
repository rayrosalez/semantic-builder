"""Who is calling, and may they approve?

Databricks Apps puts the signed-in user's identity in X-Forwarded-* headers.
The app's service principal executes the SQL, so these headers are the only
record of the real person — they are written into every change request.
"""
import os
import time

from fastapi import Request

from .config import APPROVERS, IS_DATABRICKS_APP, get_workspace_client

_groups_cache: dict[str, tuple[float, set[str]]] = {}
_GROUPS_TTL = 300


def current_user(request: Request) -> dict:
    email = request.headers.get("x-forwarded-email")
    name = request.headers.get("x-forwarded-preferred-username") or email
    if not email and not IS_DATABRICKS_APP:
        email = os.environ.get("SB_LOCAL_USER", "local.dev@example.com")
        name = email
    return {"email": email or "unknown", "name": name or email or "unknown"}


def _user_groups(email: str) -> set[str]:
    """Group display names for a user, via SCIM (cached for a few minutes)."""
    hit = _groups_cache.get(email)
    if hit and time.time() - hit[0] < _GROUPS_TTL:
        return hit[1]
    groups: set[str] = set()
    try:
        w = get_workspace_client()
        for u in w.users.list(filter=f'userName eq "{email}"', attributes="groups"):
            groups |= {g.display for g in (u.groups or []) if g.display}
    except Exception:
        pass
    _groups_cache[email] = (time.time(), groups)
    return groups


def is_approver(email: str) -> bool:
    email = (email or "").lower()
    if not email or email == "unknown":
        return False
    if email in {a.lower() for a in APPROVERS if "@" in a}:
        return True
    wanted = {a for a in APPROVERS if "@" not in a}
    return bool(wanted and wanted & _user_groups(email))
