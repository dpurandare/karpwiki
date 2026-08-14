"""Per-agent-role model resolution (09 §16)."""

import pytest

from karpwiki import llm


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
