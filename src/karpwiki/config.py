"""Runtime configuration, read from the environment.

Phase 1a needs three bindings only: the Metadata DB, the Object Store root, and the
Celery broker (02 §1-3, 08 §2). The LLM settings below are the platform defaults a
workspace's SCHEMA.md may override (09 §16); nothing reads them until 1b.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Local development reads a gitignored .env; a deployment has none and passes real
# environment variables instead. load_dotenv does not override variables already set,
# so the deployment's values always win and this is a no-op there.
load_dotenv()

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

# Platform-default model per agent role, as a Pydantic AI "provider:model" string.
# A workspace's SCHEMA.md `llm.<role>.model` takes precedence (09 §16); the API key is
# never configured here — it resolves from the secrets manager at call time (09 §13).
LLM_CLASSIFIER_MODEL = os.environ.get("KARPWIKI_LLM_CLASSIFIER_MODEL", "")
LLM_CURATOR_MODEL = os.environ.get("KARPWIKI_LLM_CURATOR_MODEL", "")
