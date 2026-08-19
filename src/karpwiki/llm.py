"""Per-agent-role model resolution (09 §16), and the retry-with-backoff wrapper every real
LLM call in this codebase uses (03 §1, phase2-tasklist.md step 33).

The effective model for an agent is the workspace's `SCHEMA.md` override if it sets one,
otherwise the platform default. The value is a single Pydantic AI `provider:model` string,
so switching provider and switching model are the same operation and no caller branches on
provider. Credentials are not here — they resolve from the secrets manager (09 §13).

`retry_transient` started in `ingestion.py` (step 33) and moved here (step 38) once
`advisor.py` needed the identical behavior for its own merge call — `advisor.py` cannot
import `ingestion.py` (it would cycle: `ingestion -> advisor -> ingestion`), but both can
safely import this module, which has no karpwiki-internal dependencies beyond `config`.
"""

import asyncio
import logging
from typing import Literal

from . import config

logger = logging.getLogger(__name__)

AgentRole = Literal["classifier", "curator"]

class TransientCallFailed(Exception):
    """Every attempt failed. `attempts` and the chained `__cause__` (the last underlying
    exception) are what a caller's except block reads to fill in a review-item/log's
    "attempt count and failure context" (03 §1)."""

    def __init__(self, attempts: int):
        super().__init__(f"exhausted {attempts} attempts")
        self.attempts = attempts


async def retry_transient(fn):
    """Retry an external call with exponential backoff — wraps only the real LLM calls
    (`ingestion.call_model`/`call_curator_model`/`call_merge_model`,
    `advisor.call_page_merge_model`), never the generic `call` parameters callers inject,
    so the test suite's fakes stay a single, fast, deterministic call. Retries on any
    exception rather than a specific provider's error types: Pydantic AI abstracts the
    provider (08 §2), so pinning to e.g. OpenAI's `RateLimitError` would be brittle across
    providers and isn't asked for anywhere in spec/ — a permanent failure just costs a few
    wasted attempts before giving up, same as a transient one exhausting its budget."""
    attempts = config.LLM_RETRY_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except Exception as exc:
            if attempt == attempts:
                raise TransientCallFailed(attempt) from exc
            delay = config.LLM_RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1)
            logger.warning(
                "transient call failure (attempt %d/%d): %s — retrying in %.1fs",
                attempt,
                attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)


def failure_detail(step: str, exc: Exception) -> dict:
    """Shared shape for an `error`-style transition's detail (03 §1) — `attempts` only
    appears when a `TransientCallFailed` really did retry; a test's directly-injected
    failure (no retry wrapper involved) keeps the plain `{"step", "error"}` shape."""
    detail = {"step": step, "error": type(exc.__cause__ or exc).__name__}
    if isinstance(exc, TransientCallFailed):
        detail["attempts"] = exc.attempts
    return detail

_PLATFORM_DEFAULTS: dict[str, str] = {
    "classifier": config.LLM_CLASSIFIER_MODEL,
    "curator": config.LLM_CURATOR_MODEL,
}


class ModelNotConfiguredError(RuntimeError):
    """Neither the workspace nor the platform defines a model for this role."""


def resolve_model(role: AgentRole, schema: dict | None = None) -> str:
    """Return the `provider:model` string for `role` in a workspace with this SCHEMA.md."""
    override = ((schema or {}).get("llm") or {}).get(role, {}).get("model")
    model = override or _PLATFORM_DEFAULTS.get(role) or ""
    if not model:
        raise ModelNotConfiguredError(
            f"no model configured for the {role} agent: set llm.{role}.model in the "
            f"workspace's SCHEMA.md, or KARPWIKI_LLM_{role.upper()}_MODEL"
        )
    return model
