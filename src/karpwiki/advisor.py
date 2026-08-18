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

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import curate, dedup, llm, review, search, versioning
from .frontmatter import split_frontmatter
from .models import (
    IndexState,
    IndexStatus,
    PageLink,
    PageStatus,
    PageType,
    PageVersion,
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
    (step 37) and `orphaned` (step 39) are built — `contradicted_by` (step 40) extends this
    once its detector exists, the same way `resolve_duplicate` grew one action at a time
    rather than all four arriving at once.

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
        merged = await (call or call_page_merge_model)(
            model=llm.resolve_model("curator"),
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
