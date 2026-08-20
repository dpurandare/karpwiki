"""Optional read-through search-result cache (02 §6, phase3-tasklist.md step 76)."""

import uuid

import pytest
import redis.asyncio as redis

from karpwiki import cache, config
from karpwiki.search_result import SearchResult

RESULT = SearchResult(
    page_id=uuid.uuid4(),
    workspace_id="eng-docs",
    path="concepts/foo.md",
    page_type="concept",
    title="Foo",
    score=0.9,
    excerpt="an excerpt",
    citations=("[^1]: raw.md",),
)


@pytest.fixture
async def redis_client():
    client = redis.from_url(config.CELERY_BROKER_URL)
    yield client
    await client.aclose()


def test_search_key_is_stable_for_identical_params():
    kwargs = dict(
        workspace_ids=["ws-a", "ws-b"],
        query="retry",
        limit=20,
        include_drafts=False,
        page_types=None,
        tags=None,
        date_from=None,
        date_to=None,
    )
    assert cache.search_key(**kwargs) == cache.search_key(**kwargs)


def test_search_key_ignores_workspace_id_ordering():
    a = cache.search_key(
        workspace_ids=["ws-a", "ws-b"],
        query="retry",
        limit=20,
        include_drafts=False,
        page_types=None,
        tags=None,
        date_from=None,
        date_to=None,
    )
    b = cache.search_key(
        workspace_ids=["ws-b", "ws-a"],
        query="retry",
        limit=20,
        include_drafts=False,
        page_types=None,
        tags=None,
        date_from=None,
        date_to=None,
    )
    assert a == b


@pytest.mark.parametrize(
    "override",
    [
        {"workspace_ids": ["ws-a"]},
        {"query": "backoff"},
        {"limit": 5},
        {"include_drafts": True},
        {"page_types": ["concept"]},
        {"tags": ["reliability"]},
    ],
)
def test_search_key_differs_when_any_resolved_param_differs(override):
    """The real correctness guarantee this module's docstring makes: two callers with
    different accessible workspaces or filters can never collide on the same key."""
    base = dict(
        workspace_ids=["ws-a", "ws-b"],
        query="retry",
        limit=20,
        include_drafts=False,
        page_types=None,
        tags=None,
        date_from=None,
        date_to=None,
    )
    assert cache.search_key(**base) != cache.search_key(**{**base, **override})


async def test_get_returns_none_on_a_miss_and_records_it(redis_client):
    key = f"cache:search:{uuid.uuid4().hex}"
    await redis_client.delete(cache._MISSES_KEY, cache._HITS_KEY)
    try:
        result = await cache.get_search_results(redis_client, key)
        assert result is None
        assert int(await redis_client.get(cache._MISSES_KEY)) == 1
    finally:
        await redis_client.delete(cache._MISSES_KEY, cache._HITS_KEY)


async def test_set_then_get_round_trips_and_records_a_hit(redis_client):
    key = f"cache:search:{uuid.uuid4().hex}"
    await redis_client.delete(cache._MISSES_KEY, cache._HITS_KEY)
    try:
        await cache.set_search_results(redis_client, key, [RESULT], ttl_seconds=30)
        got = await cache.get_search_results(redis_client, key)
        assert got == [RESULT]
        assert int(await redis_client.get(cache._HITS_KEY)) == 1
    finally:
        await redis_client.delete(key, cache._MISSES_KEY, cache._HITS_KEY)


async def test_hit_rate_is_none_with_no_activity(redis_client):
    await redis_client.delete(cache._HITS_KEY, cache._MISSES_KEY)
    assert await cache.hit_rate() is None


async def test_hit_rate_reflects_real_hits_and_misses(redis_client):
    await redis_client.delete(cache._HITS_KEY, cache._MISSES_KEY)
    try:
        await redis_client.set(cache._HITS_KEY, 3)
        await redis_client.set(cache._MISSES_KEY, 1)
        assert await cache.hit_rate() == 0.75
    finally:
        await redis_client.delete(cache._HITS_KEY, cache._MISSES_KEY)
