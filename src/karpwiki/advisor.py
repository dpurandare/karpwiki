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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import review, search
from .models import (
    IndexState,
    IndexStatus,
    PageVersion,
    RawSource,
    RawSourceStatus,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    WikiPage,
)

class InvalidResolutionError(ValueError):
    """Mirrors `ingestion.InvalidResolutionError` — kept local rather than imported, since
    `ingestion.py` is what calls into this module for `resolve_review_item`'s dispatch."""


# 09 §8's decided default — confirms 09 §6's illustrative `retention.superseded_source_days`.
DEFAULT_SUPERSEDED_SOURCE_RETENTION_DAYS = 180


# 09 §6's SCHEMA.md template gives popularity-tiered values (`high_traffic_days: 90`,
# `low_traffic_days: 365`), but 05 §2 assigns that tiering to the *scheduler* ("a tuning
# detail of the scheduler, not a hard architectural requirement") — phase2-tasklist.md step
# 41, not this one. This single-tier default is the more responsive of the two, until step
# 41 layers tiering on top via this same `threshold_days` parameter.
DEFAULT_STALENESS_THRESHOLD_DAYS = 90


@dataclass(frozen=True)
class StaleFinding:
    page_id: uuid.UUID
    path: str
    reason: str  # 05 §3's vocabulary: "stale_content" | "source_updated"


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
) -> ReviewItem | None:
    """05 §3: one batched `reindex` review item per workspace per run. Signal 2's pages are
    marked `stale` here (nothing else would ever do it — a superseded source doesn't itself
    trigger a page write) so approving the item can dispatch `reindex` the same way any
    other stale page's does (02 §7)."""
    if await _open_reindex_item(session, workspace_id=workspace_id) is not None:
        return None

    stale = await find_stale_pages(session, workspace_id=workspace_id, threshold_days=threshold_days)
    superseded = await find_pages_citing_superseded_sources(session, workspace_id=workspace_id)
    for finding in superseded:
        await search.mark_stale(session, finding.page_id)

    findings = stale + superseded
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


async def _open_prune_item(session: AsyncSession, *, workspace_id: str) -> ReviewItem | None:
    return (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.kind == ReviewKind.prune,
                ReviewItem.status == ReviewStatus.open,
            )
        )
    ).scalars().first()


async def run_superseded_source_detector(
    session: AsyncSession,
    *,
    workspace_id: str,
    retention_days: int = DEFAULT_SUPERSEDED_SOURCE_RETENTION_DAYS,
) -> ReviewItem | None:
    """05 §4: one batched `prune` review item per workspace per run, same shape as
    `run_staleness_detector` — skips if an equivalent item is already open."""
    if await _open_prune_item(session, workspace_id=workspace_id) is not None:
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


async def resolve_prune(session: AsyncSession, *, item: ReviewItem, action: str, actor: str) -> ReviewItem:
    """Admin resolution of a `prune` review item (05 §4). Only `superseded_source_retention`
    is built (step 37) — `orphaned`/`low_traffic` (step 39) and `contradicted_by` (step 40)
    extend this once their detectors exist, the same way `resolve_duplicate` grew one
    action at a time rather than all four arriving at once.

    "delete superseded source" only ever flips `RawSource.status` to `archived` — 05 §4's
    "follows the object-store lifecycle tiering" already assigns physical erasure to an
    external object-store lifecycle policy reacting to that status tag (02 §2), not to
    application code (`objectstore.delete` is explicitly staging-only, never for a
    final-key object)."""
    if item.kind is not ReviewKind.prune:
        raise InvalidResolutionError(f"review item {item.review_id} is not a prune item")
    reason = (item.detail or {}).get("reason")
    if reason != "superseded_source_retention":
        raise InvalidResolutionError(
            f"prune resolution for reason {reason!r} is not implemented"
        )
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
    return await review.resolve(session, item=item, action=action, actor=actor)
