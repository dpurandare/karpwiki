"""Read-through search-result caching wired into GET /search (02 §6, phase3-tasklist.md
step 76) — cache.py's own unit tests cover the module directly; these prove the wiring
into api.run_search behaves correctly end to end."""

import uuid
from datetime import date

import pytest
import redis.asyncio as redis
from sqlalchemy import select

from karpwiki import cache, config, search, versioning
from karpwiki.models import PageStatus, PageType, PageVersion, QueryLog

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}


@pytest.fixture(autouse=True)
async def _reset_cache_counters():
    """The hit/miss counters are global Redis keys, not reset per test the way the DB
    session fixture is — every test here that enables the cache must not leak counts into
    an unrelated test (e.g. test_metrics_api.py's `cache_hit_rate is None` assertion)."""
    client = redis.from_url(config.CELERY_BROKER_URL)
    await client.delete(cache._HITS_KEY, cache._MISSES_KEY)
    yield
    await client.delete(cache._HITS_KEY, cache._MISSES_KEY)
    await client.aclose()


async def _page(session, workspace, *, title, body, status=PageStatus.published):
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=PageType.concept,
        title=title,
        description=f"About {title}.",
        date=date(2026, 8, 20),
        tags=["a", "b"],
        body=body,
        author="system:curator",
        status=status,
    )
    version = await session.get(PageVersion, page.current_version_id)
    await search.index_page(session, page=page, version=version)
    return page


async def test_cache_disabled_by_default_reflects_new_pages_immediately(client, session, workspace):
    term = f"marker{uuid.uuid4().hex}"
    await _page(session, workspace, title="First", body=f"contains {term}")

    first = await client.get("/search", headers=CONTRIBUTOR, params={"q": term})
    assert len(first.json()["items"]) == 1

    await _page(session, workspace, title="Second", body=f"also contains {term}")
    second = await client.get("/search", headers=CONTRIBUTOR, params={"q": term})
    assert len(second.json()["items"]) == 2


async def test_cache_enabled_serves_a_stale_result_within_ttl(client, session, workspace, monkeypatch):
    monkeypatch.setattr(config, "CACHE_ENABLED", True)
    term = f"marker{uuid.uuid4().hex}"
    await _page(session, workspace, title="First", body=f"contains {term}")

    first = await client.get("/search", headers=CONTRIBUTOR, params={"q": term})
    assert len(first.json()["items"]) == 1

    await _page(session, workspace, title="Second", body=f"also contains {term}")
    second = await client.get("/search", headers=CONTRIBUTOR, params={"q": term})
    assert len(second.json()["items"]) == 1  # cache hit — the new page isn't reflected yet


async def test_cache_enabled_still_records_query_log_on_a_hit(client, session, workspace, monkeypatch):
    """The real correctness constraint cache.py's own docstring names: query_log must be
    written on every call, cache hit or not, or the feedback loop/analytics silently break."""
    monkeypatch.setattr(config, "CACHE_ENABLED", True)
    term = f"marker{uuid.uuid4().hex}"
    await _page(session, workspace, title="First", body=f"contains {term}")

    await client.get("/search", headers=CONTRIBUTOR, params={"q": term})
    await client.get("/search", headers=CONTRIBUTOR, params={"q": term})  # cache hit

    count = (
        await session.execute(select(QueryLog).where(QueryLog.query_text == term))
    ).scalars().all()
    assert len(count) == 2


async def test_cache_enabled_never_leaks_across_callers_with_different_access(
    client, session, workspace, other_workspace, monkeypatch
):
    """The core security guarantee: two callers with different accessible workspaces must
    never collide on the same cache key, even for the identical query text."""
    from karpwiki.models import AccessPolicy, Role

    monkeypatch.setattr(config, "CACHE_ENABLED", True)
    term = f"marker{uuid.uuid4().hex}"
    await _page(session, workspace, title="Eng Page", body=f"shared {term}")
    await _page(session, other_workspace, title="Policy Page", body=f"shared {term}")
    # "morgan" is a fresh principal with access to `other_workspace` ONLY — `casey` (the
    # fixture's other default principal) already has reader on `workspace` too, which
    # would defeat this test's whole point.
    session.add(AccessPolicy(workspace_id=other_workspace.workspace_id, principal="morgan", role=Role.reader))
    await session.commit()

    deepak_results = await client.get("/search", headers=CONTRIBUTOR, params={"q": term})
    assert {i["title"] for i in deepak_results.json()["items"]} == {"Eng Page"}

    morgan_results = await client.get(
        "/search", headers={"X-Karpwiki-User": "morgan"}, params={"q": term}
    )
    assert {i["title"] for i in morgan_results.json()["items"]} == {"Policy Page"}
