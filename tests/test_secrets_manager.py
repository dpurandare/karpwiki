"""Connector credential resolution (09 §13, phase2-tasklist.md step 53)."""

import pytest

from karpwiki import secrets_manager


async def test_env_resolver_reads_the_named_variable(monkeypatch):
    monkeypatch.setenv("SOME_CONNECTOR_SECRET", "the-real-token")
    resolver = secrets_manager.EnvSecretResolver()

    assert await resolver.resolve("SOME_CONNECTOR_SECRET") == "the-real-token"


async def test_env_resolver_raises_when_unset(monkeypatch):
    monkeypatch.delenv("NO_SUCH_VAR_HERE", raising=False)
    resolver = secrets_manager.EnvSecretResolver()

    with pytest.raises(secrets_manager.SecretNotFoundError):
        await resolver.resolve("NO_SUCH_VAR_HERE")


def test_default_secret_resolver_is_env_based():
    assert isinstance(secrets_manager.default_secret_resolver(), secrets_manager.EnvSecretResolver)
