"""Runtime configuration, read from the environment.

Phase 1a needs three bindings only: the Metadata DB, the Object Store root, and the
Celery broker (02 §1-3, 08 §2). The LLM settings below are the platform defaults a
workspace's SCHEMA.md may override (09 §16); nothing reads them until 1b.
"""

import os

DATABASE_URL = os.environ.get(
    "KARPWIKI_DATABASE_URL",
    "postgresql+asyncpg://karpwiki:karpwiki@localhost:5432/karpwiki",
)

# fsspec URL — file:// for local development, s3:// in a deployment (02 §2, 08 §3).
OBJECT_STORE_URL = os.environ.get("KARPWIKI_OBJECT_STORE_URL", "file://./var/objectstore")

CELERY_BROKER_URL = os.environ.get("KARPWIKI_CELERY_BROKER_URL", "redis://localhost:6379/0")

# Platform-default model per agent role, as a Pydantic AI "provider:model" string.
# A workspace's SCHEMA.md `llm.<role>.model` takes precedence (09 §16); the API key is
# never configured here — it resolves from the secrets manager at call time (09 §13).
LLM_CLASSIFIER_MODEL = os.environ.get("KARPWIKI_LLM_CLASSIFIER_MODEL", "")
LLM_CURATOR_MODEL = os.environ.get("KARPWIKI_LLM_CURATOR_MODEL", "")
