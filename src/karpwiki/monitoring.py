"""Performance Monitoring (05 §8, phase2-tasklist.md step 44) — read-only metrics for
the Admin Console. `00` §1 scopes this repo to "admin console scope, not pixel-level UI
design," so this module (and its `GET /metrics/*` endpoints in `api.py`) is the backend
data a real dashboard would render, not a UI.

Two accepted gaps, documented here rather than silently missing:

- **Cache hit rate** (05 §8's Search Performance row): `02` §6's optional cache layer was
  never built in this implementation — flagged roadmap-only since phase2-tasklist.md step
  34's writeup. `search_performance()` reports `cache_hit_rate: None` rather than a fake
  number.
- **Storage trend** (05 §8's Storage Utilization row): no metrics-history/time-series
  mechanism exists anywhere in this codebase. `storage_utilization()` reports a current
  snapshot only.

`index_health`/`ingestion_pipeline`/`search_performance`/`review_queue_health` all accept
an optional `workspace_id` — scoped to one workspace when given, aggregated across every
workspace otherwise, the same optional-scope shape `document_types.py`'s list functions
already use. `queue_depths()` is the one exception: a Celery queue mixes every workspace's
work, so per-workspace depth isn't a coherent concept — it's reported globally.
"""

from datetime import UTC, datetime, timedelta

import redis.asyncio as redis
from sqlalchemy import func, select, text
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
    WikiPage,
)

# 06 §1's already-decided SLA: submission/classification/duplicate items gate a
# placeholder page going live; reindex/prune have none (processed in maintenance batches).
SLA_KINDS = (ReviewKind.submission, ReviewKind.classification, ReviewKind.duplicate)
DEFAULT_REVIEW_SLA_HOURS = 4
DEFAULT_SEARCH_LATENCY_SLA_MS = 1000  # 06 §1: p95 < 1s
DEFAULT_STUCK_THRESHOLD_HOURS = 24
DEFAULT_WINDOW_DAYS = 7


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


async def storage_utilization(session: AsyncSession, *, workspace_id: str | None = None) -> dict:
    """05 §8's Storage Utilization dashboard — a current snapshot only (this module's
    docstring names the trend gap). Metadata DB size and FTS index size are *content-byte*
    approximations (`octet_length`/`pg_column_size` sums over the relevant columns), not
    real Postgres storage accounting (which also counts indexes, TOAST, WAL, free space) —
    an exact per-workspace breakdown of that isn't possible, since Postgres sizes whole
    tables, not workspace-filtered row subsets."""
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

    return {
        "object_store_bytes": object_store_bytes,
        "metadata_db_bytes_approx": db_bytes,
        "fts_index_bytes_approx": fts_bytes,
        "trend": None,
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
