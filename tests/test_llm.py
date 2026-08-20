"""Per-agent-role model resolution (09 §16), and retry-with-backoff (03 §1,
phase2-tasklist.md step 33) — the latter moved here from test_ingestion.py at step 38, when
advisor.py needed the same helper `ingestion.py` had been using alone, forcing it into this
shared, dependency-free module.
"""

import pytest

from karpwiki import config, llm


def test_workspace_schema_overrides_the_platform_default(monkeypatch):
    monkeypatch.setitem(llm._PLATFORM_DEFAULTS, "curator", "openai:platform-default")
    schema = {"llm": {"curator": {"model": "openai:workspace-choice"}}}
    assert llm.resolve_model("curator", schema) == "openai:workspace-choice"


def test_platform_default_applies_when_the_workspace_is_silent(monkeypatch):
    monkeypatch.setitem(llm._PLATFORM_DEFAULTS, "classifier", "openai:platform-default")
    assert llm.resolve_model("classifier", {}) == "openai:platform-default"
    assert llm.resolve_model("classifier", None) == "openai:platform-default"
    assert llm.resolve_model("classifier", {"llm": {"curator": {"model": "x"}}}) == (
        "openai:platform-default"
    )


def test_roles_resolve_independently(monkeypatch):
    monkeypatch.setitem(llm._PLATFORM_DEFAULTS, "classifier", "openai:cheap")
    monkeypatch.setitem(llm._PLATFORM_DEFAULTS, "curator", "openai:flagship")
    assert llm.resolve_model("classifier") == "openai:cheap"
    assert llm.resolve_model("curator") == "openai:flagship"


def test_unconfigured_role_raises_rather_than_guessing(monkeypatch):
    monkeypatch.setitem(llm._PLATFORM_DEFAULTS, "curator", "")
    with pytest.raises(llm.ModelNotConfiguredError, match="curator"):
        llm.resolve_model("curator", {})


def test_a_role_key_explicitly_present_but_none_falls_back_to_the_default(monkeypatch):
    """A real parsed SCHEMA.md dict (schema.as_dict) sets every optional field explicitly,
    including `llm.<role>: None` when unset — the key is present, not absent, unlike this
    test file's other hand-built dicts above. Regression for a real bug this shape
    triggered live (phase3-tasklist.md step 59): `.get(role, {})`'s default never applies
    when the key already exists with value `None`, and `.get("model")` on `None` raised."""
    monkeypatch.setitem(llm._PLATFORM_DEFAULTS, "curator", "openai:platform-default")
    schema = {"llm": {"classifier": None, "curator": None}}
    assert llm.resolve_model("curator", schema) == "openai:platform-default"


async def test_retry_transient_succeeds_after_failures(monkeypatch):
    monkeypatch.setattr(config, "LLM_RETRY_BASE_DELAY_SECONDS", 0.0)
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) < config.LLM_RETRY_ATTEMPTS:
            raise TimeoutError("upstream")
        return "ok"

    assert await llm.retry_transient(flaky) == "ok"
    assert len(attempts) == config.LLM_RETRY_ATTEMPTS


async def test_retry_transient_raises_once_exhausted(monkeypatch):
    monkeypatch.setattr(config, "LLM_RETRY_BASE_DELAY_SECONDS", 0.0)

    async def always_fails():
        raise TimeoutError("upstream")

    with pytest.raises(llm.TransientCallFailed) as exc_info:
        await llm.retry_transient(always_fails)
    assert exc_info.value.attempts == config.LLM_RETRY_ATTEMPTS
    assert isinstance(exc_info.value.__cause__, TimeoutError)


def test_failure_detail_only_adds_attempts_for_transient_call_failed():
    assert llm.failure_detail("classify", TimeoutError("boom")) == {
        "step": "classify",
        "error": "TimeoutError",
    }
    try:
        raise llm.TransientCallFailed(3) from TimeoutError("boom")
    except llm.TransientCallFailed as exc:
        detail = llm.failure_detail("classify", exc)
    assert detail == {"step": "classify", "error": "TimeoutError", "attempts": 3}
