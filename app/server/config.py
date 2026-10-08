"""Runtime config for Semantic Builder.

Every environment-specific value arrives as an env var. In a deployed app the
bundle sets them (resources/app.yml); locally, export them or rely on the
defaults below, which only make sense for a developer's own workspace.
"""
import fnmatch
import os
import re
from functools import lru_cache

from databricks.sdk import WorkspaceClient

IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))

WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")

# Where the change-request queue lives: <catalog>.<schema>.metadata_change_requests
STAGING_CATALOG = os.environ.get("SB_STAGING_CATALOG", "main")
STAGING_SCHEMA = os.environ.get("SB_STAGING_SCHEMA", "semantic_builder")
STAGING_TABLE = f"`{STAGING_CATALOG}`.`{STAGING_SCHEMA}`.`metadata_change_requests`"

# Which objects the business can see and edit. Comma-separated entries of
#   catalog | catalog.schema | catalog.schema_glob*   (e.g. "finance, ops.claims_*")
SCOPE = [s.strip() for s in os.environ.get("SB_SCOPE", "").split(",") if s.strip()]

# Approvers: comma-separated user emails and/or workspace group names.
APPROVERS = [s.strip() for s in os.environ.get("SB_APPROVERS", "").split(",") if s.strip()]
ALLOW_SELF_APPROVAL = os.environ.get("SB_ALLOW_SELF_APPROVAL", "false").lower() == "true"

# Coverage at or above this ratio counts as "documented" (0-1).
COVERAGE_TARGET = float(os.environ.get("SB_COVERAGE_TARGET", "0.8"))

# Job id of the apply job (the bundle wires it in) — enables "Apply now".
APPLY_JOB_ID = os.environ.get("SB_APPLY_JOB_ID", "")

APP_TITLE = os.environ.get("SB_APP_TITLE", "Semantic Builder")

# Pipeline-internal tables that should never show up for business users.
HIDDEN_TABLE_PATTERNS = ("__materialization_*", "event_log_*", "__*")

_IDENT = re.compile(r"^[A-Za-z0-9_\-]+$")


def scope_catalogs() -> list[str]:
    return sorted({s.split(".")[0] for s in SCOPE})


def schema_in_scope(catalog: str, schema: str) -> bool:
    if schema == "information_schema":
        return False
    for entry in SCOPE:
        parts = entry.split(".", 1)
        if parts[0] != catalog:
            continue
        if len(parts) == 1 or fnmatch.fnmatch(schema, parts[1]):
            return True
    return False


def table_visible(name: str) -> bool:
    return not any(fnmatch.fnmatch(name, p) for p in HIDDEN_TABLE_PATTERNS)


def ident(name: str) -> str:
    """Backtick-quote a UC identifier after validating it (identifiers can't be
    bound as SQL parameters, so they are checked here instead)."""
    if not name or not _IDENT.match(name):
        raise ValueError(f"Invalid identifier: {name!r}")
    return f"`{name}`"


@lru_cache(maxsize=1)
def get_workspace_client() -> WorkspaceClient:
    if IS_DATABRICKS_APP:
        return WorkspaceClient()
    return WorkspaceClient(profile=os.environ.get("DATABRICKS_PROFILE", "DEFAULT"))


def workspace_host() -> str:
    host = get_workspace_client().config.host or ""
    return host if host.startswith("http") else f"https://{host}"
