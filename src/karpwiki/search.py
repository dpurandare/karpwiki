"""Full-Text Index (02 §4) — the Platform's only query-time index.

Serves the two workloads 02 §4 names: lexical search and retrieval (04 §1-3), and
near-duplicate similarity for ingest-time duplicate detection (03 §4). Both run against
the same `page_index` rows; neither uses embeddings, and neither calls an LLM.

`workspace_id` is a mandatory filter on every query — 02 §4 partitions logically rather
than physically so a federated search touches one index instead of merging incomparable
scores across shards.

Also owns the indexing lifecycle (02 §7-8): `pending`/`stale` -> `indexing` -> `indexed`/
`error`, via `reindex`/`reindex_pending`/`retry_errored`.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    IndexState,
    IndexStatus,
    IndexType,
    PageIndex,
    PageStatus,
    PageVersion,
    WikiPage,
)

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
    """
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
) -> list[Hit]:
    """Single-stage lexical retrieval with catalog-match boost (04 §1, §3).

    The boost is baked into `index_page`'s weighting (title > description > body), so
    ranking here is a plain `ts_rank_cd` order — no separate boost step. No rerank, no
    synthesis, no LLM.
    """
    if not workspace_ids or not query.strip():
        return []

    statuses = [PageStatus.published.value] + (
        [PageStatus.draft.value] if include_drafts else []
    )
    stmt = (
        text(
            "SELECT i.page_id, i.workspace_id, p.path, "
            "       ts_rank_cd(i.tsv, q, 32) AS score "
            "FROM page_index i "
            "JOIN wiki_page p ON p.page_id = i.page_id, "
            "     websearch_to_tsquery(CAST(:config AS regconfig), :query) q "
            "WHERE i.workspace_id IN :workspace_ids "
            "  AND p.status IN :statuses "
            "  AND i.tsv @@ q "
            "ORDER BY score DESC, i.page_id "
            "LIMIT :limit"
        )
        .bindparams(
            bindparam("workspace_ids", expanding=True),
            bindparam("statuses", expanding=True),
        )
    )
    rows = await session.execute(
        stmt,
        {
            "config": CONFIG,
            "query": query,
            "workspace_ids": workspace_ids,
            "statuses": statuses,
            "limit": limit,
        },
    )
    return [Hit(r.page_id, r.workspace_id, r.path, float(r.score)) for r in rows]


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


async def pending_pages(session: AsyncSession, limit: int = 100) -> list[uuid.UUID]:
    """Pages awaiting (re)index — what the indexing worker pool drains (06 §4)."""
    result = await session.execute(
        select(IndexStatus.page_id)
        .where(IndexStatus.state.in_([IndexState.pending, IndexState.stale]))
        .limit(limit)
    )
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
