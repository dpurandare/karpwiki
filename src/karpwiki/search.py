"""Full-Text Index (02 §4) — the Platform's only query-time index.

Serves the two workloads 02 §4 names: lexical search and retrieval (04 §1-3, §6-7 —
filters and result provenance/citations, phase2-tasklist.md step 25), and near-duplicate
similarity for ingest-time duplicate detection (03 §4). Both run against the same
`page_index` rows; neither uses embeddings, and neither calls an LLM. Federated resolution
(04 §4 — accessible-workspace fan-out, the taxonomy pre-filter) and `query_log` writes
(04 §8) are gateway concerns, not this module's — `api.py`'s `/search` endpoint does that
around a call to `search()` here.

`workspace_id` is a mandatory filter on every query — 02 §4 partitions logically rather
than physically so a federated search touches one index instead of merging incomparable
scores across shards.

Also owns the indexing lifecycle (02 §7-8): `pending`/`stale` -> `indexing` -> `indexed`/
`error`, via `reindex`/`reindex_pending`/`retry_errored`.
"""

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from sqlalchemy import ARRAY, String, bindparam, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from . import dedicated_index
from .models import (
    IndexState,
    IndexStatus,
    IndexType,
    PageIndex,
    PageStatus,
    PageVersion,
    Workspace,
    WikiPage,
)
from .search_result import SearchResult, extract_citations

# Postgres text-search configuration. A per-workspace analyzer is a multi-language
# roadmap item (08 §3); one configuration is correct for Phase 1.
CONFIG = "english"

# A similarity query is built from the candidate text's own lexemes. Long documents would
# otherwise produce a tsquery with thousands of terms, which is slow and no more accurate.
MAX_SIMILARITY_TERMS = 60

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Hit:
    page_id: uuid.UUID
    workspace_id: str
    path: str
    score: float


async def index_page(session: AsyncSession, *, page: WikiPage, version: PageVersion) -> None:
    """(Re)index one page's current version and mark its index_status `indexed` (02 §7).

    Cheap by design: no LLM call is involved, which is why 02 §7 says reindexing a single
    page is never the costly part of a reindex.

    Always writes the shared Postgres index, dedicated workspace or not: near-duplicate
    detection (`find_similar` below, 03 §4) is an internal workload the dedicated-index
    escape hatch (02 §4, phase2-tasklist.md step 26) was never about — that's a query-
    serving concern for large/isolated workspaces' user-facing traffic. A dedicated
    workspace's pages *additionally* go to OpenSearch, which is what `/search` actually
    queries for that workspace (09 §29) — see that note for the full reasoning.
    """
    workspace = await session.get(Workspace, page.workspace_id)
    if workspace is not None and workspace.dedicated_index:
        await dedicated_index.index_page(page=page, version=version)

    await session.execute(delete(PageIndex).where(PageIndex.page_id == page.page_id))
    await session.execute(
        text(
            "INSERT INTO page_index (page_id, workspace_id, version_id, tsv) "
            "VALUES (:page_id, :workspace_id, :version_id, "
            "        setweight(to_tsvector(CAST(:config AS regconfig), :title), 'A') || "
            "        setweight(to_tsvector(CAST(:config AS regconfig), :description), 'B') || "
            "        setweight(to_tsvector(CAST(:config AS regconfig), :body), 'D'))"
        ).bindparams(
            page_id=page.page_id,
            workspace_id=page.workspace_id,
            version_id=version.version_id,
            config=CONFIG,
            # The title carries more signal than the body. `description` (01 §6's required
            # one-line frontmatter summary — the same content an index.md catalog entry
            # would hold, 01 §4) sits between the two: this is 04 §3's catalog-match boost,
            # realized as a weight tier rather than a separate catalog page + join, since no
            # code yet materializes an actual index.md page to match against (flagged in
            # phase1-tasklist.md's accepted-gaps note).
            title=str(version.frontmatter.get("title", "")),
            description=str(version.frontmatter.get("description", "")),
            body=version.content,
        )
    )

    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    if status is None:
        status = IndexStatus(page_id=page.page_id, index_type=IndexType.fts)
        session.add(status)
    status.state = IndexState.indexed
    status.last_content_version = version.version_id
    status.last_indexed_at = datetime.now(UTC)
    await session.flush()


async def search(
    session: AsyncSession,
    *,
    query: str,
    workspace_ids: list[str],
    limit: int = 20,
    include_drafts: bool = False,
    page_types: list[str] | None = None,
    tags: list[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[SearchResult]:
    """Single-stage lexical retrieval with catalog-match boost and result provenance
    (04 §1, §3, §6, §7).

    The boost is baked into `index_page`'s weighting (title > description > body), so
    ranking here is a plain `ts_rank_cd` order — no separate boost step. No rerank, no
    synthesis, no LLM. Filters (04 §6) apply before ranking; `page_type`/`tags`/`date`
    read the frontmatter already stored on the indexed version, so no extra join or
    denormalized column is needed for them.
    """
    if not workspace_ids or not query.strip():
        return []

    statuses = [PageStatus.published.value] + (
        [PageStatus.draft.value] if include_drafts else []
    )

    filters = ["i.workspace_id IN :workspace_ids", "p.status IN :statuses", "i.tsv @@ q"]
    params: dict = {
        "config": CONFIG,
        "query": query,
        "workspace_ids": workspace_ids,
        "statuses": statuses,
        "limit": limit,
    }
    binds = [bindparam("workspace_ids", expanding=True), bindparam("statuses", expanding=True)]

    if page_types:
        filters.append("p.page_type IN :page_types")
        params["page_types"] = page_types
        binds.append(bindparam("page_types", expanding=True))
    if tags:
        # JSONB `?|` takes one array operand, not an expanded IN-style list of scalars —
        # `expanding=True` (used above for workspace_ids/page_types) is the wrong shape
        # here; an explicit ARRAY(String) type tells asyncpg to send a real array.
        filters.append("pv.frontmatter -> 'tags' ?| :tags")
        params["tags"] = tags
        binds.append(bindparam("tags", type_=ARRAY(String)))
    if date_from is not None:
        filters.append("(pv.frontmatter ->> 'date')::date >= :date_from")
        params["date_from"] = date_from
    if date_to is not None:
        filters.append("(pv.frontmatter ->> 'date')::date <= :date_to")
        params["date_to"] = date_to

    stmt = text(
        "SELECT i.page_id, i.workspace_id, p.path, p.page_type, "
        "       COALESCE(pv.frontmatter ->> 'title', '') AS title, "
        "       ts_rank_cd(i.tsv, q, 32) AS score, "
        "       ts_headline(CAST(:config AS regconfig), pv.content, q, "
        "                   'MaxFragments=1, MinWords=15, MaxWords=35') AS excerpt, "
        "       pv.content AS content "
        "FROM page_index i "
        "JOIN wiki_page p ON p.page_id = i.page_id "
        "JOIN page_version pv ON pv.version_id = i.version_id, "
        "     websearch_to_tsquery(CAST(:config AS regconfig), :query) q "
        f"WHERE {' AND '.join(filters)} "
        "ORDER BY score DESC, i.page_id "
        "LIMIT :limit"
    ).bindparams(*binds)

    rows = await session.execute(stmt, params)
    return [
        SearchResult(
            page_id=r.page_id,
            workspace_id=r.workspace_id,
            path=r.path,
            page_type=r.page_type,
            title=r.title,
            score=float(r.score),
            excerpt=r.excerpt,
            citations=extract_citations(r.content),
        )
        for r in rows
    ]


def merge_federated(
    shared: list[SearchResult], dedicated: list[SearchResult]
) -> list[SearchResult]:
    """04 §4: merge the shared index's results with a dedicated backend's — normalizing
    only the dedicated scores (min-max to `[0,1]`, within this result set) before merging;
    the shared index's raw `ts_rank_cd` scores are left as-is. Sorted by that score
    descending, tie-broken by `workspace_id` then `page_id` for deterministic ordering.

    This is the spec's own "approximation, called out explicitly so it's a deliberate
    tradeoff rather than a surprise" — not a claim the two scales are truly comparable.
    """
    merged = list(shared) + _min_max_normalize(dedicated)
    return sorted(merged, key=lambda r: (-r.score, r.workspace_id, str(r.page_id)))


def _min_max_normalize(results: list[SearchResult]) -> list[SearchResult]:
    if not results:
        return []
    scores = [r.score for r in results]
    lo, hi = min(scores), max(scores)
    if hi == lo:
        # Every hit scored identically. 04 §4 doesn't say what to do here; mapping all to
        # 1.0 (rather than dividing by zero) is the least-surprising reading of "these all
        # matched equally well" — none should be pushed toward the bottom of the merge.
        return [replace(r, score=1.0) for r in results]
    return [replace(r, score=(r.score - lo) / (hi - lo)) for r in results]


async def find_similar(
    session: AsyncSession, *, text_body: str, workspace_id: str, limit: int = 10
) -> list[Hit]:
    """"More like this" against a workspace's own pages (02 §4, 03 §4).

    The score is **lexeme containment**: what fraction of the candidate's distinct terms
    appear in the page. 1.0 means the page covers everything the candidate talks about.

    Ranking functions are the wrong tool here. `ts_rank` is unbounded and length-dependent,
    and normalising it against the candidate's own self-rank does not bound it either — a
    longer page routinely out-ranks a short candidate on the candidate's own query, so
    every result clamps to 1.0 and identical text becomes indistinguishable from
    same-topic text. Containment is bounded by construction and degrades smoothly, which is
    what a fixed `SCHEMA.md` threshold needs.

    Containment rather than Jaccard because 03 §4 compares a *summary* against full page
    text: the summary is legitimately much shorter, and Jaccard would punish that
    asymmetry rather than the dissimilarity we actually care about.

    The tsquery prefilter is what keeps the GIN index in play; containment is then computed
    only over pages that share at least one term.
    """
    if not text_body.strip():
        return []

    rows = await session.execute(
        text(
            "WITH candidate AS ( "
            "  SELECT to_tsvector(CAST(:config AS regconfig), :body) AS tsv "
            "), terms AS ( "
            "  SELECT lexeme FROM candidate, unnest(candidate.tsv) "
            "  ORDER BY array_length(positions, 1) DESC NULLS LAST LIMIT :max_terms "
            "), q AS ( "
            "  SELECT to_tsquery(CAST(:config AS regconfig), string_agg(quote_literal(lexeme), ' | ')) AS query, "
            "         array_agg(lexeme) AS lexemes "
            "  FROM terms "
            ") "
            "SELECT i.page_id, i.workspace_id, p.path, "
            "       cardinality(ARRAY(SELECT unnest(q.lexemes) "
            "                         INTERSECT SELECT unnest(tsvector_to_array(i.tsv))))::float "
            "       / NULLIF(cardinality(q.lexemes), 0) AS score "
            "FROM page_index i "
            "JOIN wiki_page p ON p.page_id = i.page_id, q "
            "WHERE i.workspace_id = :workspace_id AND i.tsv @@ q.query "
            "ORDER BY score DESC, i.page_id "
            "LIMIT :limit"
        ),
        {
            "config": CONFIG,
            "body": text_body,
            "workspace_id": workspace_id,
            "max_terms": MAX_SIMILARITY_TERMS,
            "limit": limit,
        },
    )
    return [Hit(r.page_id, r.workspace_id, r.path, float(r.score)) for r in rows]


async def mark_stale(session: AsyncSession, page_id: uuid.UUID) -> None:
    """A new version makes an indexed page stale (02 §7); pending stays pending."""
    status = await session.get(IndexStatus, (page_id, IndexType.fts))
    if status is not None and status.state is IndexState.indexed:
        status.state = IndexState.stale


async def pending_pages(
    session: AsyncSession, limit: int = 100, *, workspace_id: str | None = None
) -> list[uuid.UUID]:
    """Pages awaiting (re)index — what the indexing worker pool drains (06 §4).

    `workspace_id` scopes the sweep to one workspace — phase2-tasklist.md step 32's "a page
    write enqueues reindex" dispatch uses this right after a curate/bulk-move/rollback write,
    rather than tracking the exact set of pages a call touched.
    """
    query = select(IndexStatus.page_id).where(
        IndexStatus.state.in_([IndexState.pending, IndexState.stale])
    )
    if workspace_id is not None:
        query = query.join(WikiPage, WikiPage.page_id == IndexStatus.page_id).where(
            WikiPage.workspace_id == workspace_id
        )
    result = await session.execute(query.limit(limit))
    return list(result.scalars())


async def reindex(session: AsyncSession, page_id: uuid.UUID) -> IndexState:
    """Run one page through the indexing lifecycle's transient `indexing` state (02 §7):
    `pending`/`stale` -> `indexing` -> `indexed` on success, or -> `error` on failure.

    02 §7 marks the `stale -> indexing` transition always-automatic — nothing dispatches
    it yet (09 §21), so this is Phase 1's stand-in "reindex job": a caller (a test, an
    admin action, or `reindex_pending`'s sweep below) invokes it explicitly.
    """
    status = await session.get(IndexStatus, (page_id, IndexType.fts))
    if status is None or status.state not in (IndexState.pending, IndexState.stale):
        raise ValueError(
            f"page {page_id} is not pending/stale "
            f"(state={status.state if status else None})"
        )

    status.state = IndexState.indexing
    await session.flush()

    page = await session.get(WikiPage, page_id)
    version = await session.get(PageVersion, page.current_version_id)
    try:
        await index_page(session, page=page, version=version)
    except Exception:
        logger.exception("reindex failed for page %s", page_id)
        status.state = IndexState.error
        await session.flush()
        return IndexState.error
    return IndexState.indexed


async def reindex_pending(session: AsyncSession, limit: int = 100) -> list[uuid.UUID]:
    """Drain `pending_pages()` through `reindex()` — the sweep a scheduled or manually
    triggered "reindex job" (02 §7) runs. Returns the page_ids that ended `indexed`."""
    done = []
    for page_id in await pending_pages(session, limit=limit):
        if await reindex(session, page_id) is IndexState.indexed:
            done.append(page_id)
    return done


async def retry_errored(session: AsyncSession, limit: int = 100) -> list[uuid.UUID]:
    """02 §7: `error -> pending`, so a retried page re-enters `reindex_pending`'s sweep."""
    result = await session.execute(
        select(IndexStatus.page_id).where(IndexStatus.state == IndexState.error).limit(limit)
    )
    page_ids = list(result.scalars())
    for page_id in page_ids:
        status = await session.get(IndexStatus, (page_id, IndexType.fts))
        status.state = IndexState.pending
    await session.flush()
    return page_ids
