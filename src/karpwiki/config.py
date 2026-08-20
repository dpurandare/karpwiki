"""Runtime configuration, read from the environment.

Phase 1a needs three bindings only: the Metadata DB, the Object Store root, and the
Celery broker (02 §1-3, 08 §2). The LLM settings below are the platform defaults a
workspace's SCHEMA.md may override (09 §16); nothing reads them until 1b.
"""

import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# Local development reads a gitignored .env; a deployment has none and passes real
# environment variables instead. load_dotenv does not override variables already set,
# so the deployment's values always win and this is a no-op there.
#
# usecwd=True searches upward from the working directory rather than from the caller's
# stack frame. The default walks frames and raises outright when there is none — in a
# REPL, in `python -` from stdin, or under a frozen interpreter.
load_dotenv(find_dotenv(usecwd=True))

DATABASE_URL = os.environ.get(
    "KARPWIKI_DATABASE_URL",
    "postgresql+asyncpg://karpwiki:karpwiki@localhost:5432/karpwiki",
)

def _pin_object_store(url: str) -> str:
    """Resolve a relative file:// object store to an absolute path at startup.

    fsspec resolves a relative file:// URL against the process's working directory, so a
    Wiki Service started from one directory and a worker started from another would use
    different object stores and fail to read each other's diffs (06 §5 runs them as
    separate process groups). Resolving here pins the value and makes it inspectable, but
    it cannot make two processes agree: a deployment must set an absolute path or an
    s3://-style URL. The relative default is for a single-process dev checkout only.
    """
    prefix = "file://"
    if not url.startswith(prefix):
        return url
    path = url[len(prefix) :]
    if path.startswith("/"):
        return url
    return f"{prefix}{Path(path).resolve()}"


# fsspec URL — file:// for local development, s3:// in a deployment (02 §2, 08 §3).
OBJECT_STORE_URL = _pin_object_store(
    os.environ.get("KARPWIKI_OBJECT_STORE_URL", "file://./var/objectstore")
)

CELERY_BROKER_URL = os.environ.get("KARPWIKI_CELERY_BROKER_URL", "redis://localhost:6379/0")

# Celery's Redis transport doesn't notice a dropped consumer — it only redelivers an
# unacked message after this elapses (default 3600s is far too slow, found live via a real
# kill-and-restart check, 09 §36). 600s comfortably covers the slowest real path
# (curate_source's several sequential LLM-touched page writes) while bounding genuine-crash
# recovery to minutes.
CELERY_VISIBILITY_TIMEOUT_SECONDS = int(
    os.environ.get("KARPWIKI_CELERY_VISIBILITY_TIMEOUT_SECONDS", "600")
)

# Dedicated Full-Text Index backend for large/isolated workspaces (02 §4, 08 §2's pick;
# phase2-tasklist.md step 26). Only read for a workspace with `dedicated_index=True`.
OPENSEARCH_URL = os.environ.get("KARPWIKI_OPENSEARCH_URL", "http://localhost:9200")
# One shared index holds every dedicated workspace's pages (dedicated_index.py) — a
# deployment sharing one OpenSearch cluster across multiple karpwiki environments needs a
# distinct name per environment to avoid collision.
OPENSEARCH_INDEX_NAME = os.environ.get("KARPWIKI_OPENSEARCH_INDEX_NAME", "karpwiki-pages")

# Platform-default model per agent role, as a Pydantic AI "provider:model" string.
# A workspace's SCHEMA.md `llm.<role>.model` takes precedence (09 §16); the API key is
# never configured here — it resolves from the secrets manager at call time (09 §13).
LLM_CLASSIFIER_MODEL = os.environ.get("KARPWIKI_LLM_CLASSIFIER_MODEL", "")
LLM_CURATOR_MODEL = os.environ.get("KARPWIKI_LLM_CURATOR_MODEL", "")

# Retry/backoff for the three real LLM calls (llm.py) — 03 §1 requires retry-with-backoff
# but names no specific count/delay, so these are this implementation's defaults (doubling
# from the base delay, capping a stuck call rather than blocking a worker slot forever).
# Deployment-wide operational tuning, not a per-workspace content threshold.
LLM_RETRY_ATTEMPTS = int(os.environ.get("KARPWIKI_LLM_RETRY_ATTEMPTS", "3"))
LLM_RETRY_BASE_DELAY_SECONDS = float(os.environ.get("KARPWIKI_LLM_RETRY_BASE_DELAY_SECONDS", "1.0"))

# Maintenance Advisor Celery beat cadence (phase2-tasklist.md step 41, 05 §2's
# "scheduling philosophy"). Deployment-wide operational tuning — how often *this
# deployment's* beat process sweeps every workspace — unlike `advisor.py`'s own
# DEFAULT_* thresholds, which are content tuning `09` §6's SCHEMA.md template scopes
# per workspace (real SCHEMA.md parsing stays out of scope, `09` §26, so those stay
# Python defaults with a function-parameter override instead of env vars). Contradiction
# Detection spends a real LLM call per candidate (step 40), so it gets its own, less
# frequent interval by default rather than sharing the other four detectors' cadence.
MAINTENANCE_INTERVAL_HOURS = float(os.environ.get("KARPWIKI_MAINTENANCE_INTERVAL_HOURS", "24"))
MAINTENANCE_CONTRADICTION_INTERVAL_HOURS = float(
    os.environ.get("KARPWIKI_MAINTENANCE_CONTRADICTION_INTERVAL_HOURS", "168")
)

# Connector-poll dispatch tick (09 §4, phase2-tasklist.md step 52) — same category as the
# maintenance cadence above, a deployment-wide operational knob, not a per-connector
# setting (that's each `Connector.schedule`'s own `interval_minutes`, step 51). This is how
# often the dispatcher itself wakes up to check which *enabled* connectors are due; it must
# be at least as frequent as the shortest configured per-connector interval to matter, so
# it defaults short (unlike the maintenance advisor's daily/weekly cadence).
CONNECTOR_DISPATCH_INTERVAL_MINUTES = float(
    os.environ.get("KARPWIKI_CONNECTOR_DISPATCH_INTERVAL_MINUTES", "5")
)

# Stuck-Pipeline Sweep Detector (phase3-tasklist.md step 64) — deployment-wide operational
# tuning, same category as the maintenance cadence above, not per-workspace content tuning.
# Threshold sits well above `CELERY_VISIBILITY_TIMEOUT_SECONDS` (600s/10min default): that's
# how long a genuine worker crash takes to self-heal via redelivery (09 §36), so this
# detector should only fire for a source that's been resting well past what automatic
# recovery would explain — a lost dispatch, not an in-progress crash recovery. The sweep
# interval defaults to match the threshold rather than the daily/weekly detector cadence,
# since an operational issue like this is worth surfacing within about an hour, not a day.
STUCK_PIPELINE_THRESHOLD_HOURS = float(
    os.environ.get("KARPWIKI_STUCK_PIPELINE_THRESHOLD_HOURS", "1")
)
MAINTENANCE_STUCK_PIPELINE_INTERVAL_HOURS = float(
    os.environ.get("KARPWIKI_MAINTENANCE_STUCK_PIPELINE_INTERVAL_HOURS", "1")
)
# Git clone timeout (connectors_git.py, step 54) — scales with the repos a deployment's
# connectors actually poll, which varies per deployment.
GIT_CLONE_TIMEOUT_SECONDS = int(os.environ.get("KARPWIKI_GIT_CLONE_TIMEOUT_SECONDS", "60"))

# Staleness popularity tiering (05 §2, `09` §6's SCHEMA.md `high_traffic_days`/
# `low_traffic_days` illustrative defaults) — env-overridable like the cadence settings
# above, even though every other detector threshold in `advisor.py` stays a plain Python
# constant; a workspace's actual traffic pattern is closer to a deployment's own tuning
# call than to the workspace-authored content thresholds SCHEMA.md would otherwise own.
STALENESS_HIGH_TRAFFIC_DAYS = int(os.environ.get("KARPWIKI_STALENESS_HIGH_TRAFFIC_DAYS", "90"))
STALENESS_LOW_TRAFFIC_DAYS = int(os.environ.get("KARPWIKI_STALENESS_LOW_TRAFFIC_DAYS", "365"))

# Real OIDC `Authenticator` (06 §3, 08 §2's Authlib pick; phase2-tasklist.md step 47) —
# the second `Authenticator` implementation, alongside `TrustedHeaderAuthenticator`
# (09 §15's Phase 1 stand-in, still the default when these are unset). Empty by default;
# `auth.default_authenticator()` only builds an `OidcAuthenticator` once `OIDC_ISSUER` and
# `OIDC_AUDIENCE` are both set, so an unconfigured deployment keeps today's behavior.
OIDC_ISSUER = os.environ.get("KARPWIKI_OIDC_ISSUER", "")
OIDC_AUDIENCE = os.environ.get("KARPWIKI_OIDC_AUDIENCE", "")
# Direct JWKS URI — skips OIDC discovery (`{issuer}/.well-known/openid-configuration`)
# when the IdP's JWKS endpoint doesn't live at the discovery-document's default location,
# or to avoid the extra discovery round trip entirely. Optional; discovery is the default.
OIDC_JWKS_URI = os.environ.get("KARPWIKI_OIDC_JWKS_URI", "")
# Which token claims carry the principal id and its groups — real IdPs vary here (`sub`
# vs `email`/`preferred_username`; `groups` is common but not standardized).
OIDC_PRINCIPAL_CLAIM = os.environ.get("KARPWIKI_OIDC_PRINCIPAL_CLAIM", "sub")
OIDC_GROUPS_CLAIM = os.environ.get("KARPWIKI_OIDC_GROUPS_CLAIM", "groups")
# JWKS/discovery HTTP client timeout — network-dependent, so worth a deployment override
# rather than a bare constant on `OidcAuthenticator`.
OIDC_JWKS_TIMEOUT_SECONDS = float(os.environ.get("KARPWIKI_OIDC_JWKS_TIMEOUT_SECONDS", "5.0"))

# Rate limiting (01 §1-2, 07 §3, 09 §14; phase2-tasklist.md step 48) — deployment-wide
# operational tuning, the same category cadence/staleness-tiering env vars above already
# occupy, not a per-workspace content threshold. One shared window; three categories
# (07 §3's own "submissions, search calls, and API requests") each get a per-principal
# limit (always checked) and a per-workspace limit (checked only when workspace_id is
# already a plain request parameter — see ratelimit.py's module docstring).
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("KARPWIKI_RATE_LIMIT_WINDOW_SECONDS", "60"))
RATE_LIMIT_SUBMIT_PER_PRINCIPAL = int(os.environ.get("KARPWIKI_RATE_LIMIT_SUBMIT_PER_PRINCIPAL", "20"))
RATE_LIMIT_SUBMIT_PER_WORKSPACE = int(os.environ.get("KARPWIKI_RATE_LIMIT_SUBMIT_PER_WORKSPACE", "200"))
RATE_LIMIT_SEARCH_PER_PRINCIPAL = int(os.environ.get("KARPWIKI_RATE_LIMIT_SEARCH_PER_PRINCIPAL", "60"))
RATE_LIMIT_SEARCH_PER_WORKSPACE = int(os.environ.get("KARPWIKI_RATE_LIMIT_SEARCH_PER_WORKSPACE", "600"))
RATE_LIMIT_GENERAL_PER_PRINCIPAL = int(os.environ.get("KARPWIKI_RATE_LIMIT_GENERAL_PER_PRINCIPAL", "300"))
RATE_LIMIT_GENERAL_PER_WORKSPACE = int(os.environ.get("KARPWIKI_RATE_LIMIT_GENERAL_PER_WORKSPACE", "3000"))

# Taxonomy bulk-move batch size (bulk_move.py, 09 §11/§30) — larger batches finish an
# admin's move faster but hold a longer-running transaction per batch; deployment-tunable.
BULK_MOVE_BATCH_SIZE = int(os.environ.get("KARPWIKI_BULK_MOVE_BATCH_SIZE", "100"))
