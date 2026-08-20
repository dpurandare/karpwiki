"""Optional read-through cache (02 §6, phase3-tasklist.md step 76) — search results only.

**Search results only, confirmed via `AskUserQuestion`**: 02 §6 names two things to cache
("frequently-read published wiki pages" and "recent search result sets"). Page caching has a real
correctness trap: `GET /pages/{id}`'s response includes `_resolve_page_links`'s output, which is
CALLER-specific (a reader without access to a linked draft page gets a shorter `links` list than an
admin would) — caching the full response would leak link visibility across principals. Search is
safe: `search.search()`/`dedicated_index.search()` already receive fully-resolved, caller-specific
parameters (the caller's own accessible `workspace_ids`, `include_drafts`, `page_types`) as plain
arguments, so hashing the FULL resolved parameter set (not just `(workspace_id, query_hash)` as 02
§6's own simplified wording puts it) means two callers with different access or filters can never
collide on the same cache key. Page caching stays a documented, not-yet-built follow-on.

Off by default (`config.CACHE_ENABLED`) — "not required for correctness, purely a latency
optimization" (02 §6's own wording), so a reference deployment doesn't pay a Redis round trip on
every search until an operator opts in. Reuses the same Redis instance the Celery broker already
runs (`config.CELERY_BROKER_URL`), same shape `ratelimit.py` already established for reusing broker
Redis for a second, unrelated purpose.

**Invalidation is TTL-only, by design** — 02 §6's own requirement ("naturally invalidates stale
entries... without explicit cache-busting logic"). A short, bounded staleness window on search
results is consistent with this system's already-accepted eventual-consistency window for the
Full-Text Index itself (02 §8: search already serves the previous version's lexical entries while a
page is `stale` and reindexing).

**Wraps only the raw retrieval call** — never `api.run_search`'s caller-specific step-70 page_type
post-filter, and never the `query_log.record` write, both of which stay unconditional on every
call regardless of a cache hit. Skipping `query_log` on a hit would silently break the search
feedback loop (07 §4, step 68) and usage-analytics search volume (step 73), both of which depend on
a real row existing per real search call — caching only ever short-circuits the expensive lexical
query itself, nothing downstream of it.
"""

import hashlib
import json
import uuid
from datetime import date

import redis.asyncio as redis

from .config import CACHE_TTL_SECONDS, CELERY_BROKER_URL
from .search_result import SearchResult

_HITS_KEY = "cache:search:hits"
_MISSES_KEY = "cache:search:misses"


def client() -> redis.Redis:
    return redis.from_url(CELERY_BROKER_URL)


def search_key(
    *,
    workspace_ids: list[str],
    query: str,
    limit: int,
    include_drafts: bool,
    page_types: list[str] | None,
    tags: list[str] | None,
    date_from: date | None,
    date_to: date | None,
) -> str:
    """A stable hash of the full resolved retrieval-parameter set — see this module's own
    docstring for why every one of these, not just `(workspace_id, query)`, has to be in
    the key for two different callers to never collide."""
    payload = json.dumps(
        {
            "workspace_ids": sorted(workspace_ids),
            "query": query,
            "limit": limit,
            "include_drafts": include_drafts,
            "page_types": sorted(page_types) if page_types else None,
            "tags": sorted(tags) if tags else None,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
        },
        sort_keys=True,
    )
    return "cache:search:" + hashlib.sha256(payload.encode()).hexdigest()


def _serialize(results: list[SearchResult]) -> bytes:
    return json.dumps(
        [
            {
                "page_id": str(r.page_id),
                "workspace_id": r.workspace_id,
                "path": r.path,
                "page_type": r.page_type,
                "title": r.title,
                "score": r.score,
                "excerpt": r.excerpt,
                "citations": list(r.citations),
            }
            for r in results
        ]
    ).encode()


def _deserialize(raw: bytes) -> list[SearchResult]:
    return [
        SearchResult(
            page_id=uuid.UUID(d["page_id"]),
            workspace_id=d["workspace_id"],
            path=d["path"],
            page_type=d["page_type"],
            title=d["title"],
            score=d["score"],
            excerpt=d["excerpt"],
            citations=tuple(d["citations"]),
        )
        for d in json.loads(raw)
    ]


async def get_search_results(redis_client: redis.Redis, key: str) -> list[SearchResult] | None:
    raw = await redis_client.get(key)
    if raw is None:
        await redis_client.incr(_MISSES_KEY)
        return None
    await redis_client.incr(_HITS_KEY)
    return _deserialize(raw)


async def set_search_results(
    redis_client: redis.Redis,
    key: str,
    results: list[SearchResult],
    *,
    ttl_seconds: int = CACHE_TTL_SECONDS,
) -> None:
    await redis_client.set(key, _serialize(results), ex=ttl_seconds)


async def hit_rate() -> float | None:
    """05 §8's Search Performance dashboard's `cache_hit_rate` — the accepted gap this
    step closes. `None` (not `0.0`) when there's no real sample yet (cache disabled, or
    nothing has been looked up), same "don't fake a number" reasoning `search_performance`'s
    own `cache_hit_rate: None` gap already used before this step existed."""
    redis_client = client()
    try:
        hits = int(await redis_client.get(_HITS_KEY) or 0)
        misses = int(await redis_client.get(_MISSES_KEY) or 0)
    finally:
        await redis_client.aclose()
    total = hits + misses
    if total == 0:
        return None
    return hits / total
