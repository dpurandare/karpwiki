"""Maintenance Advisor detectors (05 §2) — phase2-tasklist.md steps 36-40.

Each detector is a pure read-only "find" function plus a thin "run" function that turns
findings into review items. Findings are batched into one review item per workspace per
run rather than one per page (05 §3: "small, single-page reindexes ... do not go through
this review path — only batched/costly reindexes do"), and a run skips entirely if an
equivalent item is already open, since nothing schedules these detectors yet (step 41) and
a naive re-run must not spam duplicates.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config, curate, dedup, llm, pipeline, review, schema, search, versioning
from .frontmatter import split_frontmatter
from .models import (
    FeedbackRating,
    IndexState,
    IndexStatus,
    IngestionLog,
    PageLink,
    PageStatus,
    PageType,
    PageVersion,
    PipelineState,
    QueryFeedback,
    QueryLog,
    RawSource,
    RawSourceStatus,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    VersionTrigger,
    WikiPage,
)


class InvalidResolutionError(ValueError):
    """Mirrors `ingestion.InvalidResolutionError` — kept local rather than imported, since
    `ingestion.py` is what calls into this module for `resolve_review_item`'s dispatch."""


# 09 §8's decided default — confirms 09 §6's illustrative `retention.superseded_source_days`.
DEFAULT_SUPERSEDED_SOURCE_RETENTION_DAYS = 180

# 09 §8's decided default — confirms 09 §6's illustrative
# `thresholds.orphan.query_log_lookback_days`, and sits inside query_log's own 90-day
# retention window (09 §8) by construction, so "zero appearances" never silently means
# "the log was already purged."
DEFAULT_ORPHAN_QUERY_LOG_LOOKBACK_DAYS = 90


# 09 §6's SCHEMA.md template gives popularity-tiered values (`high_traffic_days: 90`,
# `low_traffic_days: 365`), but 05 §2 assigns that tiering to the *scheduler* ("a tuning
# detail of the scheduler, not a hard architectural requirement") — phase2-tasklist.md step
# 41, not this one. This single-tier default is the more responsive of the two, until step
# 41 layers tiering on top via this same `threshold_days` parameter.
DEFAULT_STALENESS_THRESHOLD_DAYS = 90

# Search result feedback loop (07 §4, phase3-tasklist.md step 68) — same lookback window as
# the Orphan/Low-Traffic Detector's own `query_log`-derived signal (`DEFAULT_ORPHAN_QUERY_
# LOG_LOOKBACK_DAYS` above), for the same reason: it must fit inside `query_log`'s own
# 90-day retention (09 §8) or "no recent ratings" would silently mean "the log was purged."
DEFAULT_FEEDBACK_LOOKBACK_DAYS = 90
# A page needs at least this many ratings before its down-ratio means anything — one or two
# down-votes shouldn't flag a page.
DEFAULT_MIN_FEEDBACK_COUNT = 3
# Fraction of ratings that must be "down" within the lookback window to flag a page.
DEFAULT_LOW_RATING_THRESHOLD = 0.6


@dataclass(frozen=True)
class StaleFinding:
    page_id: uuid.UUID
    path: str
    reason: str  # 05 §3's vocabulary: "stale_content" | "source_updated" | "low_feedback"


async def find_stale_pages(
    session: AsyncSession, *, workspace_id: str, threshold_days: int = DEFAULT_STALENESS_THRESHOLD_DAYS
) -> list[StaleFinding]:
    """Signal 1 (05 §2): `index_status = stale` for longer than the threshold. "How long"
    is proxied by the current version's `created_at` — the write that triggered
    `search.mark_stale` (02 §7) — since `index_status` itself carries no "became stale at"
    timestamp of its own."""
    cutoff = datetime.now(UTC) - timedelta(days=threshold_days)
    rows = (
        await session.execute(
            select(WikiPage.page_id, WikiPage.path)
            .join(IndexStatus, IndexStatus.page_id == WikiPage.page_id)
            .join(PageVersion, PageVersion.version_id == WikiPage.current_version_id)
            .where(
                WikiPage.workspace_id == workspace_id,
                IndexStatus.state == IndexState.stale,
                PageVersion.created_at < cutoff,
            )
        )
    ).all()
    return [StaleFinding(page_id=pid, path=path, reason="stale_content") for pid, path in rows]


async def find_stale_pages_tiered(
    session: AsyncSession,
    *,
    workspace_id: str,
    high_traffic_days: int | None = None,
    low_traffic_days: int | None = None,
    traffic_lookback_days: int = DEFAULT_ORPHAN_QUERY_LOG_LOOKBACK_DAYS,
) -> list[StaleFinding]:
    """05 §2's popularity-tiered refresh, layered on top of `find_stale_pages` (Signal 1
    only — Signal 2 has no duration to tier against, it's an immediate flag) without
    changing that function at all: call it once at each tier's day count, then keep a page
    from the more permissive (`high_traffic_days`) result if it's either genuinely
    high-traffic, or stale enough to also appear in the stricter (`low_traffic_days`)
    result on its own merits. "High traffic" reuses the orphan detector's own
    query_log-presence check and lookback window (`find_orphaned_pages`) as the popularity
    signal, rather than inventing a second one.

    `high_traffic_days`/`low_traffic_days` default to `config.py`'s env-overridable
    values — a deliberate exception to this module's usual pattern of local
    `DEFAULT_*` constants, since cadence-adjacent scheduling knobs are a deployment-wide
    concern (like `KARPWIKI_CELERY_BROKER_URL`), not a per-workspace content threshold
    (see `config.py`'s own comment)."""
    high_traffic_days = (
        config.STALENESS_HIGH_TRAFFIC_DAYS if high_traffic_days is None else high_traffic_days
    )
    low_traffic_days = (
        config.STALENESS_LOW_TRAFFIC_DAYS if low_traffic_days is None else low_traffic_days
    )

    permissive = await find_stale_pages(session, workspace_id=workspace_id, threshold_days=high_traffic_days)
    if not permissive:
        return []
    strict_ids = {
        f.page_id
        for f in await find_stale_pages(session, workspace_id=workspace_id, threshold_days=low_traffic_days)
    }

    cutoff = datetime.now(UTC) - timedelta(days=traffic_lookback_days)
    tiered: list[StaleFinding] = []
    for finding in permissive:
        if finding.page_id in strict_ids:
            tiered.append(finding)
            continue
        queried = (
            await session.execute(
                select(QueryLog.query_id)
                .where(
                    QueryLog.created_at >= cutoff,
                    QueryLog.results.contains([{"page_id": str(finding.page_id)}]),
                )
                .limit(1)
            )
        ).first()
        if queried is not None:
            tiered.append(finding)
    return tiered


async def find_pages_citing_superseded_sources(
    session: AsyncSession, *, workspace_id: str
) -> list[StaleFinding]:
    """Signal 2 (05 §2): a cited `raw_source` was superseded without re-ingestion. Scoped
    to the source's own page (`sources/{source_id}.md`, `ingestion._write_source_page`) —
    the only page a `raw_source` has a structured link to today. A concept/entity page's
    provenance back to the source(s) that shaped it isn't tracked anywhere (citations are
    free-text footnotes, not an FK), so this can't reach further without inventing a
    citation graph this step doesn't otherwise need."""
    superseded_ids = (
        await session.execute(
            select(RawSource.source_id).where(
                RawSource.workspace_id == workspace_id,
                RawSource.status == RawSourceStatus.superseded,
            )
        )
    ).scalars().all()
    if not superseded_ids:
        return []

    paths = [f"sources/{sid}.md" for sid in superseded_ids]
    rows = (
        await session.execute(
            select(WikiPage.page_id, WikiPage.path)
            .join(IndexStatus, IndexStatus.page_id == WikiPage.page_id)
            .where(
                WikiPage.workspace_id == workspace_id,
                WikiPage.path.in_(paths),
                IndexStatus.state == IndexState.indexed,
            )
        )
    ).all()
    return [StaleFinding(page_id=pid, path=path, reason="source_updated") for pid, path in rows]


async def find_low_feedback_pages(
    session: AsyncSession,
    *,
    workspace_id: str,
    lookback_days: int = DEFAULT_FEEDBACK_LOOKBACK_DAYS,
    min_feedback_count: int = DEFAULT_MIN_FEEDBACK_COUNT,
    low_rating_threshold: float = DEFAULT_LOW_RATING_THRESHOLD,
) -> list[StaleFinding]:
    """Signal 3 (07 §4, phase3-tasklist.md step 68): a page whose search-result feedback,
    within the lookback window, is at least `min_feedback_count` ratings and at least
    `low_rating_threshold` of them "down" — "this isn't serving readers," a signal neither
    of the first two staleness signals has. Aggregated in Python rather than SQL
    `GROUP BY`/`HAVING`: feedback volume per page is small (this is a per-page rating
    count, not a row-count-in-the-millions table), and the ratio math reads more plainly
    this way than as a single SQL expression."""
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    rows = (
        await session.execute(
            select(QueryFeedback.page_id, QueryFeedback.rating, WikiPage.path)
            .join(WikiPage, WikiPage.page_id == QueryFeedback.page_id)
            .where(WikiPage.workspace_id == workspace_id, QueryFeedback.created_at >= cutoff)
        )
    ).all()

    ratings_by_page: dict[uuid.UUID, tuple[str, list[FeedbackRating]]] = {}
    for page_id, rating, path in rows:
        _, ratings = ratings_by_page.setdefault(page_id, (path, []))
        ratings.append(rating)

    findings = []
    for page_id, (path, ratings) in ratings_by_page.items():
        if len(ratings) < min_feedback_count:
            continue
        down = sum(1 for r in ratings if r is FeedbackRating.down)
        if down / len(ratings) >= low_rating_threshold:
            findings.append(StaleFinding(page_id=page_id, path=path, reason="low_feedback"))
    return findings


async def _open_reindex_item(session: AsyncSession, *, workspace_id: str) -> ReviewItem | None:
    return (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.kind == ReviewKind.reindex,
                ReviewItem.status == ReviewStatus.open,
            )
        )
    ).scalars().first()


async def run_staleness_detector(
    session: AsyncSession,
    *,
    workspace_id: str,
    threshold_days: int = DEFAULT_STALENESS_THRESHOLD_DAYS,
    tiered: bool = False,
    high_traffic_days: int | None = None,
    low_traffic_days: int | None = None,
    traffic_lookback_days: int = DEFAULT_ORPHAN_QUERY_LOG_LOOKBACK_DAYS,
    feedback_lookback_days: int = DEFAULT_FEEDBACK_LOOKBACK_DAYS,
    min_feedback_count: int = DEFAULT_MIN_FEEDBACK_COUNT,
    low_rating_threshold: float = DEFAULT_LOW_RATING_THRESHOLD,
) -> ReviewItem | None:
    """05 §3: one batched `reindex` review item per workspace per run. Signal 2's and
    Signal 3's pages are marked `stale` here (nothing else would ever do it for either —
    a superseded source doesn't itself trigger a page write, and neither does a search
    rating) so approving the item can dispatch `reindex` the same way any other stale
    page's does (02 §7); `search.reindex` itself requires `pending`/`stale`, so a
    low-feedback page batched into this item without this would fail that call.

    `tiered=False` (the default, unchanged from before step 41) uses `threshold_days` as a
    single flat cutoff via `find_stale_pages`, same as every direct/manual call site and
    every existing test. `tiered=True` — what step 41's beat-scheduled dispatch actually
    uses — ignores `threshold_days` and calls `find_stale_pages_tiered` instead (05 §2's
    popularity-tiered refresh); `threshold_days` stays a plain parameter rather than being
    removed so a manual single-value check is still available either way.

    Signal 3 (07 §4, phase3-tasklist.md step 68) — persistently low-rated pages — is added
    unconditionally to both the flat and tiered paths, since it has no duration to tier
    against either (same reasoning `find_stale_pages_tiered`'s own docstring already gives
    for why Signal 2 isn't tiered)."""
    if await _open_reindex_item(session, workspace_id=workspace_id) is not None:
        return None

    if tiered:
        stale = await find_stale_pages_tiered(
            session,
            workspace_id=workspace_id,
            high_traffic_days=high_traffic_days,
            low_traffic_days=low_traffic_days,
            traffic_lookback_days=traffic_lookback_days,
        )
    else:
        stale = await find_stale_pages(session, workspace_id=workspace_id, threshold_days=threshold_days)
    superseded = await find_pages_citing_superseded_sources(session, workspace_id=workspace_id)
    for finding in superseded:
        await search.mark_stale(session, finding.page_id)
    low_feedback = await find_low_feedback_pages(
        session,
        workspace_id=workspace_id,
        lookback_days=feedback_lookback_days,
        min_feedback_count=min_feedback_count,
        low_rating_threshold=low_rating_threshold,
    )
    for finding in low_feedback:
        await search.mark_stale(session, finding.page_id)

    findings = stale + superseded + low_feedback
    if not findings:
        return None

    severity = "high" if len(findings) >= 20 else "medium" if len(findings) >= 5 else "low"
    return await review.create(
        session,
        kind=ReviewKind.reindex,
        subject_ref=workspace_id,
        workspace_id=workspace_id,
        severity=severity,
        proposed_action="reindex now",
        detail={
            "raised_by": "advisor",
            "page_count": len(findings),
            "pages": [
                {"page_id": str(f.page_id), "path": f.path, "reason": f.reason} for f in findings
            ],
        },
    )


async def resolve_reindex(
    session: AsyncSession, *, item: ReviewItem, action: str, actor: str
) -> ReviewItem:
    """Admin resolution of a `reindex` review item (05 §3). Bookkeeping only — no dispatch
    here, to avoid a circular import (`tasks.py` already imports `ingestion.py`, which is
    where `resolve_review_item` calls this from); the caller dispatches `reindex` for each
    `item.detail["pages"]` entry itself, the same way it already does for a duplicate
    resolution's `merge` outcome (phase2-tasklist.md step 32, 09 §35)."""
    if item.kind is not ReviewKind.reindex:
        raise InvalidResolutionError(f"review item {item.review_id} is not a reindex item")
    if action not in ("reindex now", "dismiss"):
        # "schedule for off-peak" is 05 §3's third proposed action, but nothing in this
        # codebase schedules deferred work to a specific time window (step 41's Celery beat
        # is recurring-schedule, not one-off-at-a-later-time) — rejected explicitly rather
        # than silently behaving like immediate dispatch.
        raise InvalidResolutionError(
            f"{action!r} is not a supported reindex resolution (reindex now | dismiss)"
        )
    return await review.resolve(session, item=item, action=action, actor=actor)


@dataclass(frozen=True)
class SupersededSourceFinding:
    source_id: uuid.UUID
    filename: str
    superseded_at: datetime


async def find_superseded_sources_past_retention(
    session: AsyncSession,
    *,
    workspace_id: str,
    retention_days: int = DEFAULT_SUPERSEDED_SOURCE_RETENTION_DAYS,
) -> list[SupersededSourceFinding]:
    """05 §4: `raw_source.status = superseded` and past the retention window. Sources
    superseded before `superseded_at` existed (phase2-tasklist.md step 37) have no
    timestamp to check against and are skipped rather than assumed either way — they'll
    be caught once something re-supersedes them or, more likely, never existed in a real
    deployment since this column landed with the detector that reads it."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    rows = (
        await session.execute(
            select(RawSource.source_id, RawSource.filename, RawSource.superseded_at).where(
                RawSource.workspace_id == workspace_id,
                RawSource.status == RawSourceStatus.superseded,
                RawSource.superseded_at.is_not(None),
                RawSource.superseded_at < cutoff,
            )
        )
    ).all()
    return [
        SupersededSourceFinding(source_id=sid, filename=filename, superseded_at=at)
        for sid, filename, at in rows
    ]


async def _open_prune_item(session: AsyncSession, *, workspace_id: str, reason: str) -> ReviewItem | None:
    """Scoped by `detail["reason"]`, not just kind/workspace/status — `ReviewKind.prune`
    now covers more than one detector (`superseded_source_retention` here, `orphaned`
    below), and an open item for one reason must not block a genuinely different one from
    ever being raised (the same problem step 38's per-pair check solves for `duplicate`)."""
    candidates = (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.kind == ReviewKind.prune,
                ReviewItem.status == ReviewStatus.open,
            )
        )
    ).scalars().all()
    for item in candidates:
        if (item.detail or {}).get("reason") == reason:
            return item
    return None


async def run_superseded_source_detector(
    session: AsyncSession,
    *,
    workspace_id: str,
    retention_days: int = DEFAULT_SUPERSEDED_SOURCE_RETENTION_DAYS,
) -> ReviewItem | None:
    """05 §4: one batched `prune` review item per workspace per run, same shape as
    `run_staleness_detector` — skips if an equivalent item is already open."""
    if (
        await _open_prune_item(session, workspace_id=workspace_id, reason="superseded_source_retention")
        is not None
    ):
        return None

    findings = await find_superseded_sources_past_retention(
        session, workspace_id=workspace_id, retention_days=retention_days
    )
    if not findings:
        return None

    severity = "high" if len(findings) >= 20 else "medium" if len(findings) >= 5 else "low"
    return await review.create(
        session,
        kind=ReviewKind.prune,
        subject_ref=workspace_id,
        workspace_id=workspace_id,
        severity=severity,
        proposed_action="delete superseded source",
        detail={
            "raised_by": "advisor",
            "reason": "superseded_source_retention",
            "source_count": len(findings),
            "sources": [
                {
                    "source_id": str(f.source_id),
                    "filename": f.filename,
                    "superseded_at": f.superseded_at.isoformat(),
                }
                for f in findings
            ],
        },
    )


@dataclass(frozen=True)
class OrphanFinding:
    page_id: uuid.UUID
    path: str


# Content page types only — `overview`/`index`/`log` are structural bookkeeping pages that
# legitimately have zero inbound page_link references (nothing links *to* the overview
# page) and aren't prune candidates; `source` pages are cited via free-text footnotes, not
# `page_link` rows (09 §39's note on step 36's Signal 2), so "inbound references" doesn't
# mean the same thing for them, and their own retention already has a dedicated detector
# (step 37).
ORPHAN_CANDIDATE_PAGE_TYPES = (PageType.concept, PageType.entity, PageType.comparison)


async def find_orphaned_pages(
    session: AsyncSession,
    *,
    workspace_id: str,
    lookback_days: int = DEFAULT_ORPHAN_QUERY_LOG_LOOKBACK_DAYS,
) -> list[OrphanFinding]:
    """05 §2: zero inbound `page_link` references **and** zero `query_log` appearances over
    the lookback window — both conditions, not either alone (a page with no incoming links
    but real query traffic is still being used; a rarely-linked page someone keeps
    searching for isn't truly orphaned). Stage 1 (inbound links) is cheap and workspace-wide
    in one query; stage 2 (query_log) only runs against that already-small candidate set,
    one query per candidate — a periodic batch job, not a hot path, same cost shape as the
    other detectors' per-page checks."""
    candidates = (
        await session.execute(
            select(WikiPage.page_id, WikiPage.path).where(
                WikiPage.workspace_id == workspace_id,
                WikiPage.status == PageStatus.published,
                WikiPage.page_type.in_(ORPHAN_CANDIDATE_PAGE_TYPES),
                ~exists().where(PageLink.to_page_id == WikiPage.page_id),
            )
        )
    ).all()
    if not candidates:
        return []

    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    findings: list[OrphanFinding] = []
    for page_id, path in candidates:
        queried = (
            await session.execute(
                select(QueryLog.query_id)
                .where(
                    QueryLog.created_at >= cutoff,
                    QueryLog.results.contains([{"page_id": str(page_id)}]),
                )
                .limit(1)
            )
        ).first()
        if queried is None:
            findings.append(OrphanFinding(page_id=page_id, path=path))
    return findings


async def run_orphan_detector(
    session: AsyncSession,
    *,
    workspace_id: str,
    lookback_days: int = DEFAULT_ORPHAN_QUERY_LOG_LOOKBACK_DAYS,
) -> ReviewItem | None:
    """05 §2/§4: one batched `prune` review item per workspace per run, same shape as
    `run_superseded_source_detector` — skips if an `orphaned`-reason item is already open."""
    if await _open_prune_item(session, workspace_id=workspace_id, reason="orphaned") is not None:
        return None

    findings = await find_orphaned_pages(session, workspace_id=workspace_id, lookback_days=lookback_days)
    if not findings:
        return None

    severity = "high" if len(findings) >= 20 else "medium" if len(findings) >= 5 else "low"
    return await review.create(
        session,
        kind=ReviewKind.prune,
        subject_ref=workspace_id,
        workspace_id=workspace_id,
        severity=severity,
        proposed_action="archive page",
        detail={
            "raised_by": "advisor",
            "reason": "orphaned",
            "page_count": len(findings),
            "pages": [{"page_id": str(f.page_id), "path": f.path} for f in findings],
        },
    )


async def resolve_prune(session: AsyncSession, *, item: ReviewItem, action: str, actor: str) -> ReviewItem:
    """Admin resolution of a `prune` review item (05 §4). `superseded_source_retention`
    (step 37), `orphaned` (step 39), and `contradicted_by` (step 40) are built — each added
    its own reason branch rather than all four arriving at once, the same way
    `resolve_duplicate` grew one action at a time.

    "delete superseded source" only ever flips `RawSource.status` to `archived` — 05 §4's
    "follows the object-store lifecycle tiering" already assigns physical erasure to an
    external object-store lifecycle policy reacting to that status tag (02 §2), not to
    application code (`objectstore.delete` is explicitly staging-only, never for a
    final-key object). "archive page" is the direct `WikiPage.status = archived` analog for
    a page subject rather than a source one (05 §4: "archive... reversible... default")."""
    if item.kind is not ReviewKind.prune:
        raise InvalidResolutionError(f"review item {item.review_id} is not a prune item")
    reason = (item.detail or {}).get("reason")

    if reason == "superseded_source_retention":
        if action not in ("delete superseded source", "dismiss"):
            raise InvalidResolutionError(
                f"{action!r} is not a supported prune resolution for a superseded source "
                "(delete superseded source | dismiss)"
            )
        if action == "delete superseded source":
            for entry in (item.detail or {}).get("sources", []):
                source = await session.get(RawSource, uuid.UUID(entry["source_id"]))
                if source is not None and source.status is RawSourceStatus.superseded:
                    source.status = RawSourceStatus.archived
    elif reason == "orphaned":
        if action not in ("archive page", "dismiss"):
            raise InvalidResolutionError(
                f"{action!r} is not a supported prune resolution for orphaned pages "
                "(archive page | dismiss)"
            )
        if action == "archive page":
            for entry in (item.detail or {}).get("pages", []):
                page = await session.get(WikiPage, uuid.UUID(entry["page_id"]))
                if page is not None and page.status is PageStatus.published:
                    page.status = PageStatus.archived
    elif reason == "contradicted_by":
        if action not in ("archive page", "dismiss"):
            raise InvalidResolutionError(
                f"{action!r} is not a supported prune resolution for a contradiction "
                "(archive page | dismiss)"
            )
        if action == "archive page":
            page = await session.get(WikiPage, uuid.UUID((item.detail or {})["page_id"]))
            if page is not None and page.status is PageStatus.published:
                page.status = PageStatus.archived
    else:
        raise InvalidResolutionError(f"prune resolution for reason {reason!r} is not implemented")

    return await review.resolve(session, item=item, action=action, actor=actor)


# --- Existing-Content Duplicate Detector (05 §5) — step 38 -------------------------------
#
# Unlike the two detectors above, findings here are NOT batched one-item-per-workspace:
# `merge`/`supersede`/`keep_both`/`reject` are inherently pair-specific decisions (one pair
# might get merged, another dismissed), so one review item per similar pair matches how
# ingest-time `duplicate` items already work — always singular, never batched (03 §4).


@dataclass(frozen=True)
class DuplicatePagePairFinding:
    primary_page_id: uuid.UUID
    primary_path: str
    duplicate_page_id: uuid.UUID
    duplicate_path: str
    score: float


# Reuses 03 §4/09 §17's already-calibrated near-duplicate threshold rather than inventing a
# second one — same metric (`search.find_similar`'s lexeme containment), same meaning.
DEFAULT_DUPLICATE_SIMILARITY_THRESHOLD = dedup.DEFAULT_NEAR_DUPLICATE_SCORE

# Caps how many pairs one detector run raises — a large workspace's first-ever run could
# otherwise surface dozens of pairs at once; bounded here rather than left unbounded since
# nothing schedules incremental runs yet (step 41) to naturally spread this out over time.
DEFAULT_MAX_DUPLICATE_ITEMS_PER_RUN = 10


async def find_similar_page_pairs(
    session: AsyncSession,
    *,
    workspace_id: str,
    threshold: float = DEFAULT_DUPLICATE_SIMILARITY_THRESHOLD,
) -> list[DuplicatePagePairFinding]:
    """05 §5: a "more like this" scan of each workspace's own published pages against the
    Full-Text Index, for high-similarity pairs never caught at ingest time. Scans every
    published page's own body against `search.find_similar` (excluding the page's own
    match against itself), then keeps one finding per unordered pair — a pair would
    otherwise turn up twice, once from each page's own scan.

    "Primary" is the older page (created first — `versioning.create_page`'s original,
    since a page's `page_id` carries no timestamp of its own) and "duplicate" the newer;
    an arbitrary but defensible convention (the pair is symmetric — nothing about the
    content says which "should" be canonical) an admin can always resolve differently."""
    pages = (
        await session.execute(
            select(WikiPage.page_id, WikiPage.path, WikiPage.current_version_id)
            .where(WikiPage.workspace_id == workspace_id, WikiPage.status == PageStatus.published)
        )
    ).all()
    if len(pages) < 2:
        return []

    version_ids = [v for _, _, v in pages if v is not None]
    versions = {
        row.version_id: row
        for row in (
            await session.execute(select(PageVersion).where(PageVersion.version_id.in_(version_ids)))
        ).scalars()
    }

    seen_pairs: set[frozenset[uuid.UUID]] = set()
    findings: list[DuplicatePagePairFinding] = []
    for page_id, path, version_id in pages:
        version = versions.get(version_id)
        if version is None:
            continue
        _, body = split_frontmatter(version.content)
        hits = await search.find_similar(session, text_body=body, workspace_id=workspace_id)
        for hit in hits:
            if hit.page_id == page_id or hit.score < threshold:
                continue
            pair_key = frozenset({page_id, hit.page_id})
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            other_version_id = next((v for pid, _, v in pages if pid == hit.page_id), None)
            other = versions.get(other_version_id)
            # Older `created_at` is primary; ties (shouldn't happen — clock_timestamp() per
            # version, 01 §5) keep this page as primary arbitrarily.
            if other is not None and other.created_at < version.created_at:
                findings.append(
                    DuplicatePagePairFinding(
                        primary_page_id=hit.page_id,
                        primary_path=hit.path,
                        duplicate_page_id=page_id,
                        duplicate_path=path,
                        score=hit.score,
                    )
                )
            else:
                findings.append(
                    DuplicatePagePairFinding(
                        primary_page_id=page_id,
                        primary_path=path,
                        duplicate_page_id=hit.page_id,
                        duplicate_path=hit.path,
                        score=hit.score,
                    )
                )
    return findings


async def _open_duplicate_item_for_pair(
    session: AsyncSession, *, workspace_id: str, page_a_id: uuid.UUID, page_b_id: uuid.UUID
) -> ReviewItem | None:
    candidates = (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.kind == ReviewKind.duplicate,
                ReviewItem.status == ReviewStatus.open,
            )
        )
    ).scalars().all()
    pair_key = frozenset({str(page_a_id), str(page_b_id)})
    for item in candidates:
        detail = item.detail or {}
        if detail.get("raised_by") != "advisor":
            continue
        if frozenset({detail.get("primary_page_id"), detail.get("duplicate_page_id")}) == pair_key:
            return item
    return None


async def run_existing_content_duplicate_detector(
    session: AsyncSession,
    *,
    workspace_id: str,
    threshold: float = DEFAULT_DUPLICATE_SIMILARITY_THRESHOLD,
    max_items: int = DEFAULT_MAX_DUPLICATE_ITEMS_PER_RUN,
) -> list[ReviewItem]:
    """05 §5: one `duplicate` review item per similar pair, skipping any pair that already
    has an open advisor-raised item (a naive re-run must not re-flag a pair an admin hasn't
    resolved yet) and capping how many new items one run raises."""
    findings = await find_similar_page_pairs(session, workspace_id=workspace_id, threshold=threshold)
    items: list[ReviewItem] = []
    for finding in findings:
        if len(items) >= max_items:
            break
        if (
            await _open_duplicate_item_for_pair(
                session,
                workspace_id=workspace_id,
                page_a_id=finding.primary_page_id,
                page_b_id=finding.duplicate_page_id,
            )
            is not None
        ):
            continue
        severity = "high" if finding.score >= 0.85 else "medium"
        item = await review.create(
            session,
            kind=ReviewKind.duplicate,
            subject_ref=str(finding.primary_page_id),
            workspace_id=workspace_id,
            severity=severity,
            proposed_action="merge",
            detail={
                "raised_by": "advisor",
                "primary_page_id": str(finding.primary_page_id),
                "primary_path": finding.primary_path,
                "duplicate_page_id": str(finding.duplicate_page_id),
                "duplicate_path": finding.duplicate_path,
                "score": round(finding.score, 4),
            },
        )
        items.append(item)
    return items


class PageMergeCall:
    """The LLM call `resolve_existing_duplicate`'s `merge` action makes — isolated the same
    way `ingestion.MergeCall` is, for tests to inject a fake and skip the network."""

    async def __call__(
        self, *, model: str, primary_body: str, duplicate_body: str, primary_path: str
    ) -> curate.MergedPage: ...


async def call_page_merge_model(
    *, model: str, primary_body: str, duplicate_body: str, primary_path: str
) -> curate.MergedPage:
    """Real merge call via Pydantic AI — 05 §5's existing-content `merge` resolution.
    Deliberately a separate function from `ingestion.call_merge_model` rather than a shared
    import: `advisor.py` cannot import `ingestion.py` (`ingestion -> advisor` already exists
    for `resolve_review_item`'s dispatch, so the reverse would cycle). Transient failures
    retried with backoff (`llm.retry_transient`), same as every other real LLM call."""
    from pydantic_ai import Agent

    agent = Agent(
        model,
        output_type=curate.MergedPage,
        system_prompt=(
            "You maintain a page in an enterprise wiki. A periodic similarity scan found "
            "another page in the same workspace covering the same subject, and an admin "
            "has decided to fold it into this one rather than keep both. Rewrite this "
            "page's full body to incorporate anything new, corrected, or updated from the "
            "duplicate page, preserving what still holds from this page. Then write a "
            "one-sentence change summary noting that this update came from a duplicate "
            "merge."
        ),
    )
    result = await llm.retry_transient(
        lambda: agent.run(
            f"This page's body:\n\n{primary_body}\n\n---\n\n"
            f"Duplicate page ({primary_path}'s near-duplicate):\n\n{duplicate_body}"
        )
    )
    return result.output


async def resolve_existing_duplicate(
    session: AsyncSession,
    *,
    item: ReviewItem,
    action: str,
    actor: str,
    call: PageMergeCall | None = None,
) -> None:
    """Admin resolution of an advisor-raised `duplicate` review item (05 §5). Reuses the
    same four action names `ingestion.resolve_duplicate` (ingest-time) uses, reinterpreted
    for two already-published pages rather than a `RawSource` + an existing page — there is
    no "new" item here to actually reject, so `reject`/`keep_both` both leave every page
    untouched and differ only in their recorded audit label (the finding was wrong vs. the
    finding was right but not worth acting on)."""
    if item.kind is not ReviewKind.duplicate or (item.detail or {}).get("raised_by") != "advisor":
        raise InvalidResolutionError(
            f"review item {item.review_id} is not an advisor-raised duplicate item"
        )
    if action not in ("reject", "keep_both", "supersede", "merge"):
        raise InvalidResolutionError(
            f"{action!r} is not a supported duplicate resolution "
            "(reject | keep_both | supersede | merge)"
        )

    detail = item.detail
    primary_id = uuid.UUID(detail["primary_page_id"])
    duplicate_id = uuid.UUID(detail["duplicate_page_id"])

    if action == "supersede":
        duplicate_page = await session.get(WikiPage, duplicate_id)
        if duplicate_page is not None:
            duplicate_page.status = PageStatus.archived
    elif action == "merge":
        primary_page = await session.get(WikiPage, primary_id)
        duplicate_page = await session.get(WikiPage, duplicate_id)
        if primary_page is None or duplicate_page is None:
            raise InvalidResolutionError(
                f"review item {item.review_id}'s pages no longer both exist"
            )
        primary_version = await session.get(PageVersion, primary_page.current_version_id)
        duplicate_version = await session.get(PageVersion, duplicate_page.current_version_id)
        _, primary_body = split_frontmatter(primary_version.content)
        _, duplicate_body = split_frontmatter(duplicate_version.content)
        workspace_schema = await schema.load(session, workspace_id=primary_page.workspace_id)
        merged = await (call or call_page_merge_model)(
            model=llm.resolve_model("curator", schema.as_dict(workspace_schema)),
            primary_body=primary_body,
            duplicate_body=duplicate_body,
            primary_path=primary_page.path,
        )
        await versioning.write_version(
            session,
            page=primary_page,
            body=merged.body,
            author="system:curator",
            trigger=VersionTrigger.ingest,
            change_summary=merged.change_summary,
        )
        duplicate_page.status = PageStatus.archived
    # reject/keep_both: no page changes — see docstring.

    await review.resolve(session, item=item, action=action, actor=actor)


# --- Contradiction Detector (05 §2, "Curator's periodic lint pass") — step 40 ------------
#
# Unlike every earlier detector, lexical similarity alone can't answer the actual
# question here — "do these two pages agree or conflict" is a semantic judgment, not a
# containment score — so this is the first detector that spends an LLM call during
# *detection* itself, not just at resolution. `find_contradiction_candidates` stays a
# cheap DB-only prefilter (reusing `search.find_similar`, same mechanism step 38 uses);
# the LLM call only runs against pairs that prefilter surfaces, capped per run.
#
# Findings raise a `prune` item (reason=`contradicted_by`, resolved by `resolve_prune`
# above) for the page the Curator judges outdated — pair-specific, not batched, same
# reasoning as step 38's duplicate items.


@dataclass(frozen=True)
class ContradictionCandidate:
    page_a_id: uuid.UUID
    page_a_path: str
    page_b_id: uuid.UUID
    page_b_path: str
    score: float


# Lower bound: pairs below this share too little vocabulary to plausibly be making a
# claim about the same subject — not worth an LLM call. Upper bound:
# `dedup.DEFAULT_NEAR_DUPLICATE_SCORE` — pairs at or above that are step 38's
# near-duplicate territory already (a near-duplicate is a merge candidate, not a
# contradiction candidate), so the two detectors' candidate pools never overlap.
DEFAULT_CONTRADICTION_MIN_SIMILARITY = 0.35
DEFAULT_CONTRADICTION_MAX_SIMILARITY = dedup.DEFAULT_NEAR_DUPLICATE_SCORE

# Caps how many candidate pairs one run spends an LLM call checking. Unlike step 38's
# per-run item cap, this bounds LLM calls made during *detection*, not just items raised
# — every candidate costs a real call regardless of whether the Curator confirms a
# contradiction, so this must be tighter than a cap on findings alone.
DEFAULT_MAX_CONTRADICTION_CHECKS_PER_RUN = 5


async def find_contradiction_candidates(
    session: AsyncSession,
    *,
    workspace_id: str,
    min_similarity: float = DEFAULT_CONTRADICTION_MIN_SIMILARITY,
    max_similarity: float = DEFAULT_CONTRADICTION_MAX_SIMILARITY,
) -> list[ContradictionCandidate]:
    """DB-only prefilter: every published page's body scanned against `search.find_similar`
    for other pages in the [`min_similarity`, `max_similarity`) band, deduped so a pair
    surfaces once regardless of which page's own scan found it."""
    pages = (
        await session.execute(
            select(WikiPage.page_id, WikiPage.path, WikiPage.current_version_id)
            .where(WikiPage.workspace_id == workspace_id, WikiPage.status == PageStatus.published)
        )
    ).all()
    if len(pages) < 2:
        return []

    version_ids = [v for _, _, v in pages if v is not None]
    versions = {
        row.version_id: row
        for row in (
            await session.execute(select(PageVersion).where(PageVersion.version_id.in_(version_ids)))
        ).scalars()
    }

    seen_pairs: set[frozenset[uuid.UUID]] = set()
    candidates: list[ContradictionCandidate] = []
    for page_id, path, version_id in pages:
        version = versions.get(version_id)
        if version is None:
            continue
        _, body = split_frontmatter(version.content)
        hits = await search.find_similar(session, text_body=body, workspace_id=workspace_id)
        for hit in hits:
            if hit.page_id == page_id or not (min_similarity <= hit.score < max_similarity):
                continue
            pair_key = frozenset({page_id, hit.page_id})
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            candidates.append(
                ContradictionCandidate(
                    page_a_id=page_id,
                    page_a_path=path,
                    page_b_id=hit.page_id,
                    page_b_path=hit.path,
                    score=hit.score,
                )
            )
    return candidates


class ContradictionJudgment(BaseModel):
    """The Curator's lint-pass verdict for one candidate pair."""

    contradicts: bool
    outdated_page: Literal["a", "b"] = Field(
        description=(
            "Which page makes the claim that should be retired, when contradicts is "
            "true. Ignored when contradicts is false, but still required — pick either "
            "value in that case."
        )
    )
    explanation: str = Field(min_length=1, description="One or two sentences on the conflicting claims.")


class ContradictionCheckCall:
    """Isolated the same way `PageMergeCall` is, for tests to inject a fake and skip the
    network."""

    async def __call__(
        self, *, model: str, page_a_body: str, page_a_path: str, page_b_body: str, page_b_path: str
    ) -> ContradictionJudgment: ...


async def call_contradiction_check(
    *, model: str, page_a_body: str, page_a_path: str, page_b_body: str, page_b_path: str
) -> ContradictionJudgment:
    """Real lint-pass call via Pydantic AI (05 §2). A lint judgment, not a curation output
    — lives here rather than in `curate.py`, same reasoning as `call_page_merge_model`.
    Transient failures retried with backoff (`llm.retry_transient`), same as every other
    real LLM call."""
    from pydantic_ai import Agent

    agent = Agent(
        model,
        output_type=ContradictionJudgment,
        system_prompt=(
            "You are auditing an enterprise wiki for contradictions, as part of a "
            "periodic lint pass. You will be shown two pages that a similarity scan "
            "flagged as covering related subject matter. Decide whether they make a "
            "genuinely conflicting factual claim about the same thing — not just "
            "overlapping topics, and not one page being a more detailed or more recent "
            "version of the other's same claim, which is an update rather than a "
            "contradiction. If they do conflict, name which page (a or b) makes the "
            "claim that is more likely outdated or wrong and should be retired."
        ),
    )
    result = await llm.retry_transient(
        lambda: agent.run(
            f"Page a ({page_a_path}):\n\n{page_a_body}\n\n---\n\n"
            f"Page b ({page_b_path}):\n\n{page_b_body}"
        )
    )
    return result.output


async def _open_prune_item_for_pair(
    session: AsyncSession, *, workspace_id: str, reason: str, page_a_id: uuid.UUID, page_b_id: uuid.UUID
) -> ReviewItem | None:
    """Pair-scoped, unlike `_open_prune_item` above — `contradicted_by` items are
    per-pair like step 38's duplicates, not one-per-workspace like the batched reasons, so
    an open item for one pair must not block a genuinely different pair from ever being
    raised."""
    candidates = (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.kind == ReviewKind.prune,
                ReviewItem.status == ReviewStatus.open,
            )
        )
    ).scalars().all()
    pair_key = frozenset({str(page_a_id), str(page_b_id)})
    for item in candidates:
        detail = item.detail or {}
        if detail.get("reason") != reason:
            continue
        if frozenset({detail.get("page_id"), detail.get("contradicting_page_id")}) == pair_key:
            return item
    return None


async def run_contradiction_detector(
    session: AsyncSession,
    *,
    workspace_id: str,
    min_similarity: float = DEFAULT_CONTRADICTION_MIN_SIMILARITY,
    max_similarity: float = DEFAULT_CONTRADICTION_MAX_SIMILARITY,
    max_checks: int = DEFAULT_MAX_CONTRADICTION_CHECKS_PER_RUN,
    call: ContradictionCheckCall | None = None,
) -> list[ReviewItem]:
    """05 §2: one `prune` review item (reason=`contradicted_by`) per confirmed
    contradicting pair. Spends at most `max_checks` LLM calls per run regardless of how
    many candidates the similarity band surfaces."""
    candidates = await find_contradiction_candidates(
        session, workspace_id=workspace_id, min_similarity=min_similarity, max_similarity=max_similarity
    )
    workspace_schema_dict = schema.as_dict(await schema.load(session, workspace_id=workspace_id))
    items: list[ReviewItem] = []
    checked = 0
    for candidate in candidates:
        if checked >= max_checks:
            break
        if (
            await _open_prune_item_for_pair(
                session,
                workspace_id=workspace_id,
                reason="contradicted_by",
                page_a_id=candidate.page_a_id,
                page_b_id=candidate.page_b_id,
            )
            is not None
        ):
            continue

        page_a = await session.get(WikiPage, candidate.page_a_id)
        page_b = await session.get(WikiPage, candidate.page_b_id)
        if page_a is None or page_b is None:
            continue
        version_a = await session.get(PageVersion, page_a.current_version_id)
        version_b = await session.get(PageVersion, page_b.current_version_id)
        _, body_a = split_frontmatter(version_a.content)
        _, body_b = split_frontmatter(version_b.content)

        checked += 1
        judgment = await (call or call_contradiction_check)(
            model=llm.resolve_model("curator", workspace_schema_dict),
            page_a_body=body_a,
            page_a_path=candidate.page_a_path,
            page_b_body=body_b,
            page_b_path=candidate.page_b_path,
        )
        if not judgment.contradicts:
            continue

        if judgment.outdated_page == "a":
            flagged_id, flagged_path = candidate.page_a_id, candidate.page_a_path
            other_id, other_path = candidate.page_b_id, candidate.page_b_path
        else:
            flagged_id, flagged_path = candidate.page_b_id, candidate.page_b_path
            other_id, other_path = candidate.page_a_id, candidate.page_a_path

        item = await review.create(
            session,
            kind=ReviewKind.prune,
            subject_ref=str(flagged_id),
            workspace_id=workspace_id,
            severity="medium",
            proposed_action="archive page",
            detail={
                "raised_by": "advisor",
                "reason": "contradicted_by",
                "page_id": str(flagged_id),
                "path": flagged_path,
                "contradicting_page_id": str(other_id),
                "contradicting_path": other_path,
                "explanation": judgment.explanation,
                "score": round(candidate.score, 4),
            },
        )
        items.append(item)
    return items


# Only these three can ever be observed *persisted* in a stuck state (pipeline.py's
# `ABORTABLE_IF_STUCK` — same set, imported from there rather than redefined here so the
# detector and the abort transition can never drift apart).
STUCK_PIPELINE_STATES = pipeline.ABORTABLE_IF_STUCK


@dataclass(frozen=True)
class StuckSourceFinding:
    source_id: uuid.UUID
    workspace_id: str | None
    filename: str
    pipeline_state: PipelineState
    entered_state_at: datetime


async def find_stuck_sources(
    session: AsyncSession, *, threshold_hours: float | None = None
) -> list[StuckSourceFinding]:
    """A source sitting in `submitted`/`classified`/`ingesting` — each independently
    committed, each waiting on a *separate* Celery dispatch that might have been lost
    (broker message dropped, dispatch code never reached) — past `threshold_hours`
    (deliberately well above `CELERY_VISIBILITY_TIMEOUT_SECONDS`, `config.py`, so a normal
    crash-and-redeliver has time to self-heal first). "How long stuck" is measured off the
    latest `ingestion_log` entry per source (`raw_source` itself has no per-state
    timestamp), not `raw_source.created_at`, which only ever records submission time."""
    threshold = (
        config.STUCK_PIPELINE_THRESHOLD_HOURS if threshold_hours is None else threshold_hours
    )
    cutoff = datetime.now(UTC) - timedelta(hours=threshold)
    entered_at = (
        select(
            IngestionLog.source_id, func.max(IngestionLog.created_at).label("entered_at")
        )
        .group_by(IngestionLog.source_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(RawSource, entered_at.c.entered_at)
            .join(entered_at, entered_at.c.source_id == RawSource.source_id)
            .where(
                RawSource.pipeline_state.in_(STUCK_PIPELINE_STATES),
                entered_at.c.entered_at < cutoff,
            )
            .order_by(entered_at.c.entered_at)
        )
    ).all()
    return [
        StuckSourceFinding(
            source_id=source.source_id,
            workspace_id=source.workspace_id,
            filename=source.filename,
            pipeline_state=source.pipeline_state,
            entered_state_at=entered_state_at,
        )
        for source, entered_state_at in rows
    ]


async def _open_stuck_item(session: AsyncSession) -> ReviewItem | None:
    return (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.kind == ReviewKind.stuck,
                ReviewItem.status == ReviewStatus.open,
            )
        )
    ).scalars().first()


async def run_stuck_pipeline_detector(
    session: AsyncSession, *, threshold_hours: float | None = None
) -> ReviewItem | None:
    """One batched review item per run, not per workspace like the other five detectors
    (05 §2) — a `submitted`-stuck source has no `workspace_id` yet (03 §1), so this
    detector can't fan out per workspace the way they do. `workspace_id=None` here is the
    same shape `submission`/`classification` items already use for exactly this reason
    (09 §22), not a gap."""
    if await _open_stuck_item(session) is not None:
        return None
    findings = await find_stuck_sources(session, threshold_hours=threshold_hours)
    if not findings:
        return None

    severity = "high" if len(findings) >= 10 else "medium" if len(findings) >= 3 else "low"
    return await review.create(
        session,
        kind=ReviewKind.stuck,
        subject_ref="stuck-pipeline-sweep",
        workspace_id=None,
        severity=severity,
        proposed_action="retry",
        detail={
            "raised_by": "advisor",
            "source_count": len(findings),
            "sources": [
                {
                    "source_id": str(f.source_id),
                    "workspace_id": f.workspace_id,
                    "filename": f.filename,
                    "pipeline_state": f.pipeline_state.value,
                    "entered_state_at": f.entered_state_at.isoformat(),
                }
                for f in findings
            ],
        },
    )


async def resolve_stuck(
    session: AsyncSession, *, item: ReviewItem, action: str, actor: str
) -> ReviewItem:
    """Admin resolution of a `stuck` review item (phase3-tasklist.md step 64). Bookkeeping
    only — same "no dispatch/side effect here" shape as `resolve_reindex`/`resolve_prune`
    above, for the same reason (`tasks.py` already imports `ingestion.py`): `retry`'s
    re-dispatch and `abort`'s per-source `rejected` transition both need code only
    `ingestion.py`/`api.py` can reach without a circular import, so `resolve_review_item`
    (`ingestion.py`) does that work itself, right after calling this."""
    if item.kind is not ReviewKind.stuck:
        raise InvalidResolutionError(f"review item {item.review_id} is not a stuck item")
    if action not in ("retry", "abort", "dismiss"):
        raise InvalidResolutionError(
            f"{action!r} is not a supported stuck resolution (retry | abort | dismiss)"
        )
    return await review.resolve(session, item=item, action=action, actor=actor)
