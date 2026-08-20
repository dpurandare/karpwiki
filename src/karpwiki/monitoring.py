"""Performance Monitoring (05 §8, phase2-tasklist.md step 44) — read-only metrics for
the Admin Console. `00` §1 scopes this repo to "admin console scope, not pixel-level UI
design," so this module (and its `GET /metrics/*` endpoints in `api.py`) is the backend
data a real dashboard would render, not a UI.

One accepted gap, documented here rather than silently missing:

- **Cache hit rate** (05 §8's Search Performance row): `02` §6's optional cache layer was
  never built in this implementation — flagged roadmap-only since phase2-tasklist.md step
  34's writeup. `search_performance()` reports `cache_hit_rate: None` rather than a fake
  number.

**Storage trend** (05 §8's Storage Utilization row) was the same shape of gap — "no
metrics-history/time-series mechanism exists anywhere in this codebase" — until
phase3-tasklist.md step 72 built one: `StorageSnapshot` (a new table),
`record_storage_snapshot`/`purge_storage_snapshots_older_than` below, and a new
beat-scheduled `tasks.record_storage_snapshots` task. `storage_utilization()`'s `trend`
field is real now — an ascending-by-date list of past snapshots, empty (not `None`) until
the first scheduled run has actually recorded one.

`index_health`/`ingestion_pipeline`/`search_performance`/`review_queue_health`/
`storage_utilization` all accept an optional `workspace_id` — scoped to one workspace when
given, aggregated across every workspace otherwise, the same optional-scope shape
`document_types.py`'s list functions already use. `queue_depths()` is the one exception: a
Celery queue mixes every workspace's work, so per-workspace depth isn't a coherent concept
— it's reported globally.
"""

from datetime import UTC, datetime, timedelta

import redis.asyncio as redis
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from . import objectstore
from .config import CELERY_BROKER_URL
from .models import (
    IndexState,
    IndexStatus,
    PageVersion,
    PipelineState,
    RawSource,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    StorageSnapshot,
    WikiPage,
)

# 06 §1's already-decided SLA: submission/classification/duplicate items gate a
# placeholder page going live; reindex/prune have none (processed in maintenance batches).
SLA_KINDS = (ReviewKind.submission, ReviewKind.classification, ReviewKind.duplicate)
DEFAULT_REVIEW_SLA_HOURS = 4
DEFAULT_SEARCH_LATENCY_SLA_MS = 1000  # 06 §1: p95 < 1s
DEFAULT_STUCK_THRESHOLD_HOURS = 24
DEFAULT_WINDOW_DAYS = 7
# Storage changes slowly day-to-day — a week (DEFAULT_WINDOW_DAYS, tuned for the faster-
# moving ingestion/search dashboards) is too short a window to show a meaningful trend.
DEFAULT_STORAGE_TREND_DAYS = 30
# Same retention-hygiene shape as query_log.RETENTION_DAYS (02 §5, 09 §8) — no privacy
# motive here (just aggregate byte counts), but unbounded growth of a row-per-workspace-
# per-run table isn't free either, and 90 days comfortably covers this module's own trend
# window above.
DEFAULT_STORAGE_SNAPSHOT_RETENTION_DAYS = 90


async def queue_depths() -> dict[str, int]:
    """Real Celery/Redis queue lengths (05 §8's "Queue depth"). Celery's Redis transport
    keys each queue by its plain name, so a bare `LLEN` per name in `tasks.QUEUES` is the
    whole implementation — no Celery inspection API needed."""
    from .tasks import QUEUES

    client = redis.from_url(CELERY_BROKER_URL)
    try:
        return {q: await client.llen(q) for q in QUEUES}
    finally:
        await client.aclose()


async def index_health(
    session: AsyncSession,
    *,
    workspace_id: str | None = None,
    stuck_threshold_hours: int = DEFAULT_STUCK_THRESHOLD_HOURS,
) -> dict:
    """05 §8's Index Health dashboard: `index_status` distribution per workspace/index
    type, plus a count "stuck" beyond a threshold. `IndexStatus` has no "entered this
    state at" timestamp (only `last_indexed_at`, the last *successful* index) — the same
    gap `advisor.find_stale_pages` already works around, reusing its exact proxy: the
    current version's `created_at` when a page has never been successfully indexed."""
    filters = [WikiPage.workspace_id == workspace_id] if workspace_id else []
    dist_rows = (
        await session.execute(
            select(WikiPage.workspace_id, IndexStatus.index_type, IndexStatus.state, func.count())
            .join(IndexStatus, IndexStatus.page_id == WikiPage.page_id)
            .where(*filters)
            .group_by(WikiPage.workspace_id, IndexStatus.index_type, IndexStatus.state)
        )
    ).all()

    cutoff = datetime.now(UTC) - timedelta(hours=stuck_threshold_hours)
    stuck_count = (
        await session.execute(
            select(func.count())
            .select_from(WikiPage)
            .join(IndexStatus, IndexStatus.page_id == WikiPage.page_id)
            .join(PageVersion, PageVersion.version_id == WikiPage.current_version_id)
            .where(
                *filters,
                IndexStatus.state != IndexState.indexed,
                func.coalesce(IndexStatus.last_indexed_at, PageVersion.created_at) < cutoff,
            )
        )
    ).scalar_one()

    return {
        "distribution": [
            {
                "workspace_id": ws,
                "index_type": index_type.value,
                "state": state.value,
                "count": count,
            }
            for ws, index_type, state, count in dist_rows
        ],
        "stuck_count": stuck_count,
        "stuck_threshold_hours": stuck_threshold_hours,
    }


async def ingestion_pipeline(
    session: AsyncSession,
    *,
    workspace_id: str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    sla_hours: int = DEFAULT_REVIEW_SLA_HOURS,
) -> dict:
    """05 §8's Ingestion Pipeline dashboard, minus queue depth (see `queue_depths()` above
    — call both, this module never merges them since one needs a DB session and the other
    doesn't). Error rate/throughput are windowed counts, not instantaneous rates — the
    closest meaningful reading given `raw_source` has no "errored_at" timestamp of its
    own, only `created_at` (step 43) and `ingested_at`."""
    now = datetime.now(UTC)
    sla_cutoff = timedelta(hours=sla_hours)
    ri_filters = [
        ReviewItem.status == ReviewStatus.open,
        ReviewItem.kind.in_(SLA_KINDS),
    ]
    if workspace_id:
        ri_filters.append(ReviewItem.workspace_id == workspace_id)
    open_sla_items = (
        await session.execute(
            select(ReviewItem.workspace_id, ReviewItem.kind, ReviewItem.created_at).where(*ri_filters)
        )
    ).all()
    past_sla = [
        {
            "workspace_id": ws,
            "kind": kind.value,
            "age_hours": round((now - created).total_seconds() / 3600, 2),
        }
        for ws, kind, created in open_sla_items
        if (now - created) > sla_cutoff
    ]

    window_cutoff = now - timedelta(days=window_days)
    rs_filters = [RawSource.workspace_id == workspace_id] if workspace_id else []
    submitted = (
        await session.execute(
            select(func.count())
            .select_from(RawSource)
            .where(RawSource.created_at >= window_cutoff, *rs_filters)
        )
    ).scalar_one()
    ingested = (
        await session.execute(
            select(func.count())
            .select_from(RawSource)
            .where(
                RawSource.ingested_at.is_not(None),
                RawSource.ingested_at >= window_cutoff,
                *rs_filters,
            )
        )
    ).scalar_one()
    errored = (
        await session.execute(
            select(func.count())
            .select_from(RawSource)
            .where(
                RawSource.pipeline_state == PipelineState.error,
                RawSource.created_at >= window_cutoff,
                *rs_filters,
            )
        )
    ).scalar_one()

    return {
        "window_days": window_days,
        "sla_hours": sla_hours,
        "open_items_past_sla": past_sla,
        "submitted_in_window": submitted,
        "ingested_in_window": ingested,
        "throughput_per_day": round(ingested / window_days, 2) if window_days else 0.0,
        "errored_in_window": errored,
        "error_rate": round(errored / submitted, 4) if submitted else 0.0,
    }


async def search_performance(
    session: AsyncSession, *, workspace_id: str | None = None, window_days: int = DEFAULT_WINDOW_DAYS
) -> dict:
    """05 §8's Search Performance dashboard: p50/p95 over `query_log.duration_ms`
    (step 44, populated by `api.py`'s `/search` endpoint going forward — rows from before
    this column existed have `duration_ms IS NULL` and are excluded, not treated as 0).
    `cache_hit_rate` is the accepted gap this module's docstring names."""
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    filters = ["created_at >= :cutoff", "duration_ms IS NOT NULL"]
    params: dict = {"cutoff": cutoff}
    if workspace_id:
        filters.append(":workspace_id = ANY(resolved_workspaces)")
        params["workspace_id"] = workspace_id

    row = (
        await session.execute(
            text(
                "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50, "
                "       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95, "
                "       count(*) AS sample_count "
                "FROM query_log "
                f"WHERE {' AND '.join(filters)}"
            ),
            params,
        )
    ).one()

    p95 = float(row.p95) if row.p95 is not None else None
    return {
        "window_days": window_days,
        "sample_count": row.sample_count,
        "p50_ms": float(row.p50) if row.p50 is not None else None,
        "p95_ms": p95,
        "p95_sla_ms": DEFAULT_SEARCH_LATENCY_SLA_MS,
        "p95_breaches_sla": p95 is not None and p95 > DEFAULT_SEARCH_LATENCY_SLA_MS,
        "cache_hit_rate": None,
    }


async def _current_storage_bytes(
    session: AsyncSession, *, workspace_id: str | None = None
) -> tuple[int, int, int]:
    """The same three content-byte approximations `storage_utilization` reports live —
    shared with `record_storage_snapshot` (phase3-tasklist.md step 72) so both read the
    identical computation rather than risking two copies drifting apart. Metadata DB size
    and FTS index size are *content-byte* approximations (`octet_length`/`pg_column_size`
    sums over the relevant columns), not real Postgres storage accounting (which also
    counts indexes, TOAST, WAL, free space) — an exact per-workspace breakdown of that
    isn't possible, since Postgres sizes whole tables, not workspace-filtered row subsets.

    Returns `(object_store_bytes, metadata_db_bytes_approx, fts_index_bytes_approx)`.
    """
    ws_filter = "WHERE p.workspace_id = :workspace_id" if workspace_id else ""
    params = {"workspace_id": workspace_id} if workspace_id else {}

    db_bytes = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(octet_length(pv.content)), 0) "
                "FROM page_version pv JOIN wiki_page p ON p.page_id = pv.page_id "
                f"{ws_filter}"
            ),
            params,
        )
    ).scalar_one()
    fts_bytes = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(pg_column_size(i.tsv)), 0) "
                "FROM page_index i JOIN wiki_page p ON p.page_id = i.page_id "
                f"{ws_filter}"
            ),
            params,
        )
    ).scalar_one()
    object_store_bytes = objectstore.size_bytes(f"/{workspace_id}" if workspace_id else "/")
    return object_store_bytes, db_bytes, fts_bytes


async def record_storage_snapshot(session: AsyncSession, *, workspace_id: str) -> StorageSnapshot:
    """One point-in-time row for the trend `storage_utilization` reads back
    (phase3-tasklist.md step 72) — always workspace-scoped, never a "global" row.
    `tasks.record_storage_snapshots` calls this once per active workspace on a recurring
    schedule; a global aggregate trend (`storage_utilization(workspace_id=None)`) is
    computed by date-bucketing across every workspace's own rows at read time instead of
    ever writing one."""
    object_store_bytes, db_bytes, fts_bytes = await _current_storage_bytes(
        session, workspace_id=workspace_id
    )
    snapshot = StorageSnapshot(
        workspace_id=workspace_id,
        object_store_bytes=object_store_bytes,
        metadata_db_bytes_approx=db_bytes,
        fts_index_bytes_approx=fts_bytes,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def purge_storage_snapshots_older_than(
    session: AsyncSession, *, days: int = DEFAULT_STORAGE_SNAPSHOT_RETENTION_DAYS
) -> int:
    """Called from `tasks.record_storage_snapshots` itself rather than getting its own
    beat entry — a small housekeeping step riding along with the task that already runs on
    the right cadence, same reasoning `query_log.purge_older_than`'s own docstring gives
    for why nothing schedules it separately."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(delete(StorageSnapshot).where(StorageSnapshot.created_at < cutoff))
    await session.flush()
    return result.rowcount or 0


async def storage_utilization(
    session: AsyncSession, *, workspace_id: str | None = None, trend_days: int = DEFAULT_STORAGE_TREND_DAYS
) -> dict:
    """05 §8's Storage Utilization dashboard. `trend` is a real, ascending-by-date list of
    past `StorageSnapshot` rows within `trend_days` — workspace-scoped when `workspace_id`
    is given, date-bucketed sums across every workspace otherwise (the same scoped/
    aggregate shape every other dashboard here already has, applied to the trend query
    too). Empty, not `None`, until the first scheduled snapshot run has actually recorded
    one — `None` meant "the mechanism doesn't exist"; an empty list correctly means "it
    exists, nothing recorded yet"."""
    object_store_bytes, db_bytes, fts_bytes = await _current_storage_bytes(
        session, workspace_id=workspace_id
    )
    cutoff = datetime.now(UTC) - timedelta(days=trend_days)

    if workspace_id:
        rows = (
            await session.execute(
                select(
                    StorageSnapshot.created_at,
                    StorageSnapshot.object_store_bytes,
                    StorageSnapshot.metadata_db_bytes_approx,
                    StorageSnapshot.fts_index_bytes_approx,
                )
                .where(StorageSnapshot.workspace_id == workspace_id, StorageSnapshot.created_at >= cutoff)
                .order_by(StorageSnapshot.created_at)
            )
        ).all()
        trend = [
            {
                "date": created_at.date().isoformat(),
                "object_store_bytes": obj,
                "metadata_db_bytes_approx": db,
                "fts_index_bytes_approx": fts,
            }
            for created_at, obj, db, fts in rows
        ]
    else:
        rows = (
            await session.execute(
                text(
                    "SELECT date_trunc('day', created_at) AS day, "
                    "       SUM(object_store_bytes) AS object_store_bytes, "
                    "       SUM(metadata_db_bytes_approx) AS metadata_db_bytes_approx, "
                    "       SUM(fts_index_bytes_approx) AS fts_index_bytes_approx "
                    "FROM storage_snapshot WHERE created_at >= :cutoff "
                    "GROUP BY day ORDER BY day"
                ),
                {"cutoff": cutoff},
            )
        ).all()
        trend = [
            {
                "date": r.day.date().isoformat(),
                "object_store_bytes": r.object_store_bytes,
                "metadata_db_bytes_approx": r.metadata_db_bytes_approx,
                "fts_index_bytes_approx": r.fts_index_bytes_approx,
            }
            for r in rows
        ]

    return {
        "object_store_bytes": object_store_bytes,
        "metadata_db_bytes_approx": db_bytes,
        "fts_index_bytes_approx": fts_bytes,
        "trend": trend,
    }


async def review_queue_health(session: AsyncSession, *, workspace_id: str | None = None) -> dict:
    """05 §8's Review Queue Health dashboard — every open kind (unlike
    `ingestion_pipeline`'s SLA-only subset above), age summarized per (workspace, kind)."""
    filters = [ReviewItem.status == ReviewStatus.open]
    if workspace_id:
        filters.append(ReviewItem.workspace_id == workspace_id)
    rows = (
        await session.execute(
            select(ReviewItem.workspace_id, ReviewItem.kind, ReviewItem.created_at).where(*filters)
        )
    ).all()

    now = datetime.now(UTC)
    ages_by_group: dict[tuple[str | None, str], list[float]] = {}
    for ws, kind, created in rows:
        ages_by_group.setdefault((ws, kind.value), []).append(
            (now - created).total_seconds() / 3600
        )

    return {
        "items": [
            {
                "workspace_id": ws,
                "kind": kind,
                "open_count": len(ages),
                "oldest_age_hours": round(max(ages), 2),
                "avg_age_hours": round(sum(ages) / len(ages), 2),
            }
            for (ws, kind), ages in sorted(ages_by_group.items(), key=lambda kv: (kv[0][0] or "", kv[0][1]))
        ]
    }
