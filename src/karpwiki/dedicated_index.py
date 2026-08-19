"""Dedicated Full-Text Index backend (02 §4, 08 §2) — OpenSearch, for a workspace flagged
`Workspace.dedicated_index=True` — phase2-tasklist.md step 26.

One shared OpenSearch index (`INDEX_NAME`) holds every dedicated workspace's pages,
`workspace_id`-filtered exactly like the Postgres shared index (02 §4's partitioning
principle applies to either backend) — not one physical OpenSearch index per workspace.
`08` §2's "dedicated index *instance*" is read here as "a dedicated backend/technology,"
not "one index per workspace"; see `09` §29 for the reasoning.

Query-serving only. Near-duplicate detection (`search.find_similar`, 03 §4) always runs
against the shared Postgres index regardless of a workspace's backend choice — `search.
index_page` writes here in *addition to* Postgres, never instead of it, for exactly that
reason (see its docstring).

A fresh `AsyncOpenSearch` client is opened and closed per call (`_client()` below) rather
than reused as a module-level singleton — its underlying `aiohttp` session binds to
whichever asyncio event loop is running when first used, and this codebase's tests give
each async test function its own loop (`asyncio_mode = "auto"`'s default). A long-lived
client would work for one loop and then break on the next with a cross-loop "used inside a
task" error. Postgres's `create_async_engine` in `db.py` never hits this because the test
`session` fixture already creates a fresh engine per test; this is the OpenSearch
equivalent of that same pattern, just paid per call instead of per test since there is no
per-request session-scoped fixture equivalent here yet.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import date

from opensearchpy import AsyncOpenSearch

from .config import OPENSEARCH_INDEX_NAME, OPENSEARCH_URL
from .models import PageStatus, PageVersion, WikiPage
from .search_result import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT, SearchResult, extract_citations

INDEX_NAME = OPENSEARCH_INDEX_NAME

_MAPPING = {
    "mappings": {
        "properties": {
            "workspace_id": {"type": "keyword"},
            "page_type": {"type": "keyword"},
            "path": {"type": "keyword"},
            "status": {"type": "keyword"},
            "title": {"type": "text"},
            "description": {"type": "text"},
            "content": {"type": "text"},
            "tags": {"type": "keyword"},
            "date": {"type": "date", "format": "yyyy-MM-dd||strict_date_optional_time"},
        }
    }
}


@asynccontextmanager
async def _client():
    client = AsyncOpenSearch(hosts=[OPENSEARCH_URL], use_ssl=False, verify_certs=False)
    try:
        yield client
    finally:
        await client.close()


async def ensure_index(client: AsyncOpenSearch) -> None:
    """Idempotent — safe to call before every write/query, matching how the shared
    Postgres index's table already exists via migration rather than a run-time check."""
    if not await client.indices.exists(index=INDEX_NAME):
        await client.indices.create(index=INDEX_NAME, body=_MAPPING)


async def index_page(*, page: WikiPage, version: PageVersion) -> None:
    """Upsert one page's current version. Indexing by a fixed `_id` (the page_id) makes
    this a full-document replace on every call — no separate delete-then-insert needed,
    unlike the shared index's explicit tsvector row replacement (`search.index_page`)."""
    frontmatter = version.frontmatter
    async with _client() as client:
        await ensure_index(client)
        await client.index(
            index=INDEX_NAME,
            id=str(page.page_id),
            body={
                "workspace_id": page.workspace_id,
                "page_type": page.page_type.value,
                "path": page.path,
                "status": page.status.value,
                "title": str(frontmatter.get("title", "")),
                "description": str(frontmatter.get("description", "")),
                "content": version.content,
                "tags": list(frontmatter.get("tags") or []),
                "date": frontmatter.get("date"),
            },
            refresh="wait_for",
        )


async def delete_page(page_id: uuid.UUID) -> None:
    """Symmetric with `search.index_page`'s Postgres delete-then-insert — removes a page's
    document entirely, e.g. if it's ever migrated off a dedicated workspace."""
    async with _client() as client:
        await ensure_index(client)
        await client.delete(index=INDEX_NAME, id=str(page_id), ignore=[404], refresh="wait_for")


async def search(
    *,
    query: str,
    workspace_ids: list[str],
    limit: int = DEFAULT_SEARCH_LIMIT,
    include_drafts: bool = False,
    page_types: list[str] | None = None,
    tags: list[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[SearchResult]:
    """The OpenSearch-backed twin of `search.search` — same filter contract (04 §6), same
    `SearchResult` shape (04 §7), scored on OpenSearch's own BM25 relevance rather than
    `ts_rank_cd`. `search.merge_federated` is what reconciles the two scales."""
    limit = min(limit, MAX_SEARCH_LIMIT)
    if not workspace_ids or not query.strip():
        return []

    statuses = [PageStatus.published.value] + (
        [PageStatus.draft.value] if include_drafts else []
    )
    filters: list[dict] = [
        {"terms": {"workspace_id": workspace_ids}},
        {"terms": {"status": statuses}},
    ]
    if page_types:
        filters.append({"terms": {"page_type": page_types}})
    if tags:
        filters.append({"terms": {"tags": tags}})
    if date_from is not None or date_to is not None:
        date_range: dict = {}
        if date_from is not None:
            date_range["gte"] = date_from.isoformat()
        if date_to is not None:
            date_range["lte"] = date_to.isoformat()
        filters.append({"range": {"date": date_range}})

    body = {
        "size": limit,
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            # Mirrors the shared index's title(A) > description(B) >
                            # content(D) weight tiers (search.index_page) as query-time
                            # field boosts — OpenSearch has no index-time setweight
                            # equivalent used here.
                            "fields": ["title^3", "description^2", "content"],
                        }
                    }
                ],
                "filter": filters,
            }
        },
        "highlight": {
            # Matches ts_headline's default StartSel/StopSel (search.search) — a merged
            # federated result set would otherwise mark matches with <em> from one backend
            # and <b> from the other for no reason a caller could anticipate.
            "pre_tags": ["<b>"],
            "post_tags": ["</b>"],
            "fields": {"content": {"fragment_size": 200, "number_of_fragments": 1}},
            "no_match_size": 200,
        },
    }

    async with _client() as client:
        await ensure_index(client)
        response = await client.search(index=INDEX_NAME, body=body)

    results = []
    for hit in response["hits"]["hits"]:
        source = hit["_source"]
        fragments = hit.get("highlight", {}).get("content") or [source["content"][:200]]
        results.append(
            SearchResult(
                page_id=uuid.UUID(hit["_id"]),
                workspace_id=source["workspace_id"],
                path=source["path"],
                page_type=source["page_type"],
                title=source["title"],
                score=float(hit["_score"]),
                excerpt=fragments[0],
                citations=extract_citations(source["content"]),
            )
        )
    return results
