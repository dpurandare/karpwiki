import uuid

import pytest
import redis.asyncio as redis
from httpx import ASGITransport, AsyncClient

from karpwiki import config, ratelimit


@pytest.fixture
async def redis_client():
    client = redis.from_url(config.CELERY_BROKER_URL)
    yield client
    await client.aclose()


async def test_check_allows_up_to_the_limit_then_blocks(redis_client):
    key = f"ratelimit:test:{uuid.uuid4().hex}"
    try:
        first = await ratelimit.check(redis_client, key=key, limit=2, window_seconds=60)
        second = await ratelimit.check(redis_client, key=key, limit=2, window_seconds=60)
        third = await ratelimit.check(redis_client, key=key, limit=2, window_seconds=60)

        assert first.allowed and first.remaining == 1
        assert second.allowed and second.remaining == 0
        assert not third.allowed and third.remaining == 0
        assert 0 < third.reset_seconds <= 60
    finally:
        await redis_client.delete(key)


def test_principal_key_is_stable_and_never_the_raw_header():
    raw = "Bearer some-real-token"
    key = ratelimit.principal_key({"Authorization": raw})
    assert key != raw
    assert key == ratelimit.principal_key({"authorization": raw})


def test_principal_key_falls_back_to_anon_when_unauthenticated():
    assert ratelimit.principal_key({}) == "anon"


def test_bulk_submit_shares_the_submit_rate_limit_category():
    """phase3-tasklist.md step 74 — a single bulk call can create many sources, so it
    belongs under the same tighter "submit" category as POST /sources, not "general"."""
    from unittest.mock import MagicMock

    from karpwiki.api import _rate_limit_category

    request = MagicMock()
    request.method = "POST"
    request.url.path = "/sources/bulk"
    assert _rate_limit_category(request) == "submit"


async def test_middleware_returns_429_with_headers_once_the_limit_is_exhausted(
    monkeypatch, session
):
    """Overrides `generous_rate_limits` back down so the real enforcement path — the one
    conftest.py's autouse fixture otherwise keeps out of every other test's way — gets
    exercised for real, against real Redis, end to end through the ASGI app.

    Uses a principal unique to this test run, not the suite's shared `deepak` header: the
    real per-principal Redis key is keyed off that raw header value, and dozens of other
    tests hit `/workspaces`-shaped "general" endpoints as `deepak` within the same 60s
    fixed window, which would make this test's own tiny limit trip on the very first
    request. `/workspaces` only needs an authenticated principal, no workspace grant, so no
    `AccessPolicy` row is needed for an unrecognized principal either."""
    monkeypatch.setattr(config, "RATE_LIMIT_GENERAL_PER_PRINCIPAL", 2)
    principal = {"X-Karpwiki-User": f"ratelimit-test-{uuid.uuid4().hex}"}

    import karpwiki.api as api_module
    from karpwiki.api import create_app

    async def _one_session():
        yield session

    app = create_app()
    app.dependency_overrides[api_module._session] = _one_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as http:
        first = await http.get("/workspaces", headers=principal)
        second = await http.get("/workspaces", headers=principal)
        third = await http.get("/workspaces", headers=principal)

        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 429
        assert third.json()["error"]["type"] == "rate_limited"
        assert "Retry-After" in third.headers
        assert third.headers["RateLimit-Remaining"] == "0"
        assert third.headers["RateLimit-Limit"] == "2"


async def test_healthz_is_exempt_from_rate_limiting(monkeypatch, session):
    """`/healthz` (step 49's Docker healthcheck target) must never itself get 429'd — an
    unauthenticated caller's requests all share the "anon" Redis bucket, and many gateway
    replicas each polling their own `/healthz` every few seconds would otherwise contend
    for the same tiny shared counter."""
    monkeypatch.setattr(config, "RATE_LIMIT_GENERAL_PER_PRINCIPAL", 2)

    import karpwiki.api as api_module
    from karpwiki.api import create_app

    async def _one_session():
        yield session

    app = create_app()
    app.dependency_overrides[api_module._session] = _one_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as http:
        for _ in range(5):
            response = await http.get("/healthz")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            assert "RateLimit-Limit" not in response.headers
