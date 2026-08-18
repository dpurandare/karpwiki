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
