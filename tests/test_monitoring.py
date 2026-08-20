"""Phase 2 step 44 — Performance Monitoring dashboards (05 §8)."""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta

from karpwiki import monitoring, objectstore, search, versioning
from karpwiki.models import (
    IndexState,
    IndexStatus,
    IndexType,
    PageStatus,
    PageType,
    PageVersion,
    QueryLog,
    RawSource,
    RawSourceStatus,
    ReviewItem,
    ReviewKind,
)


async def _page(session, workspace, *, title, body="Body.", status=PageStatus.published, indexed=False):
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=PageType.concept,
        title=title,
        description=f"About {title}.",
        date=date(2026, 8, 19),
        tags=["a", "b"],
        body=body,
        author="system:curator",
        status=status,
    )
    if indexed:
        version = await session.get(PageVersion, page.current_version_id)
        await search.index_page(session, page=page, version=version)
    return page


async def _source(session, workspace, *, filename, status=RawSourceStatus.active, days_ago=0, ingested=False):
    source_id = uuid.uuid4()
    key = f"/{workspace.workspace_id}/sources/{source_id}/{filename}"
    payload = filename.encode()
    objectstore.write_bytes(key, payload)
    source = RawSource(
        source_id=source_id,
        workspace_id=workspace.workspace_id,
        object_key=key,
        filename=filename,
        content_hash=hashlib.sha256(payload).hexdigest(),
        submitted_by="user:deepak",
        status=status,
    )
    session.add(source)
    await session.flush()
    source.created_at = datetime.now(UTC) - timedelta(days=days_ago)
    if ingested:
        source.ingested_at = datetime.now(UTC) - timedelta(days=days_ago)
    await session.flush()
    return source


# --- index_health --------------------------------------------------------------------------


async def test_index_health_reports_distribution(session, workspace):
    indexed = await _page(session, workspace, title="Indexed Page", indexed=True)
    pending = await _page(session, workspace, title="Pending Page")

    result = await monitoring.index_health(session, workspace_id=workspace.workspace_id)

    states = {(row["index_type"], row["state"]): row["count"] for row in result["distribution"]}
    assert states[("fts", "indexed")] == 1
    assert states[("fts", "pending")] == 1


async def test_index_health_flags_stuck_pages(session, workspace):
    stuck = await _page(session, workspace, title="Stuck Page")
    version = await session.get(PageVersion, stuck.current_version_id)
    version.created_at = datetime.now(UTC) - timedelta(hours=48)
    fresh = await _page(session, workspace, title="Fresh Page")
    await session.flush()

    result = await monitoring.index_health(
        session, workspace_id=workspace.workspace_id, stuck_threshold_hours=24
    )
    assert result["stuck_count"] == 1
    assert result["stuck_threshold_hours"] == 24


# --- ingestion_pipeline ----------------------------------------------------------------------


async def test_ingestion_pipeline_flags_items_past_sla(session, workspace):
    from karpwiki import review

    item = await review.create(
        session, kind=ReviewKind.duplicate, subject_ref="x", workspace_id=workspace.workspace_id
    )
    item.created_at = datetime.now(UTC) - timedelta(hours=10)
    await session.flush()

    result = await monitoring.ingestion_pipeline(
        session, workspace_id=workspace.workspace_id, sla_hours=4
    )
    assert len(result["open_items_past_sla"]) == 1
    assert result["open_items_past_sla"][0]["kind"] == "duplicate"
    assert result["open_items_past_sla"][0]["age_hours"] >= 10


async def test_ingestion_pipeline_counts_submitted_ingested_errored(session, workspace):
    from karpwiki.models import PipelineState

    await _source(session, workspace, filename="ok.md", ingested=True)
    await _source(session, workspace, filename="bad.md", status=RawSourceStatus.active)
    errored = await _source(session, workspace, filename="errored.md")
    errored.pipeline_state = PipelineState.error
    await session.flush()

    result = await monitoring.ingestion_pipeline(session, workspace_id=workspace.workspace_id, window_days=7)
    assert result["submitted_in_window"] == 3
    assert result["ingested_in_window"] == 1
    assert result["errored_in_window"] == 1
    assert result["error_rate"] > 0


async def test_ingestion_pipeline_excludes_outside_window(session, workspace):
    old = await _source(session, workspace, filename="old.md", days_ago=30)

    result = await monitoring.ingestion_pipeline(session, workspace_id=workspace.workspace_id, window_days=7)
    assert result["submitted_in_window"] == 0


# --- search_performance ----------------------------------------------------------------------


async def test_search_performance_computes_percentiles(session, workspace):
    for ms in (100, 200, 300, 400, 500):
        session.add(
            QueryLog(
                principal="user:deepak",
                query_text="q",
                resolved_workspaces=[workspace.workspace_id],
                results=[],
                duration_ms=ms,
            )
        )
    await session.flush()

    result = await monitoring.search_performance(session, workspace_id=workspace.workspace_id)
    assert result["sample_count"] == 5
    assert result["p50_ms"] == 300
    assert result["p95_ms"] is not None


async def test_search_performance_excludes_rows_with_no_duration(session, workspace):
    session.add(
        QueryLog(
            principal="user:deepak",
            query_text="q",
            resolved_workspaces=[workspace.workspace_id],
            results=[],
            duration_ms=None,
        )
    )
    await session.flush()

    result = await monitoring.search_performance(session, workspace_id=workspace.workspace_id)
    assert result["sample_count"] == 0
    assert result["p50_ms"] is None
    assert result["cache_hit_rate"] is None


async def test_search_performance_scopes_by_workspace(session, workspace, other_workspace):
    session.add(
        QueryLog(
            principal="user:deepak",
            query_text="q",
            resolved_workspaces=[other_workspace.workspace_id],
            results=[],
            duration_ms=999,
        )
    )
    await session.flush()

    result = await monitoring.search_performance(session, workspace_id=workspace.workspace_id)
    assert result["sample_count"] == 0


async def test_search_performance_flags_sla_breach(session, workspace):
    session.add(
        QueryLog(
            principal="user:deepak",
            query_text="q",
            resolved_workspaces=[workspace.workspace_id],
            results=[],
            duration_ms=2000,
        )
    )
    await session.flush()

    result = await monitoring.search_performance(session, workspace_id=workspace.workspace_id)
    assert result["p95_breaches_sla"] is True


# --- storage_utilization ----------------------------------------------------------------------


async def test_storage_utilization_reports_snapshot(session, workspace):
    await _page(session, workspace, title="Sized Page", body="x" * 500, indexed=True)
    await _source(session, workspace, filename="raw.md")

    result = await monitoring.storage_utilization(session, workspace_id=workspace.workspace_id)
    assert result["metadata_db_bytes_approx"] > 0
    assert result["fts_index_bytes_approx"] > 0
    assert result["object_store_bytes"] > 0
    assert result["trend"] == []


async def test_storage_utilization_trend_empty_before_any_snapshot(session, workspace):
    """Empty, not `None` — `None` used to mean the mechanism didn't exist at all
    (phase3-tasklist.md step 72); now it means the mechanism exists but no scheduled run
    has recorded anything yet."""
    result = await monitoring.storage_utilization(session, workspace_id=workspace.workspace_id)
    assert result["trend"] == []


async def test_record_storage_snapshot_persists_current_figures(session, workspace):
    await _page(session, workspace, title="Snap Page", body="y" * 300, indexed=True)

    snapshot = await monitoring.record_storage_snapshot(session, workspace_id=workspace.workspace_id)
    assert snapshot.workspace_id == workspace.workspace_id
    assert snapshot.metadata_db_bytes_approx > 0
    assert snapshot.object_store_bytes >= 0

    result = await monitoring.storage_utilization(session, workspace_id=workspace.workspace_id)
    [entry] = result["trend"]
    assert entry["metadata_db_bytes_approx"] == snapshot.metadata_db_bytes_approx


async def test_storage_utilization_trend_scoped_by_workspace(session, workspace, other_workspace):
    await monitoring.record_storage_snapshot(session, workspace_id=workspace.workspace_id)
    await monitoring.record_storage_snapshot(session, workspace_id=other_workspace.workspace_id)

    result = await monitoring.storage_utilization(session, workspace_id=workspace.workspace_id)
    assert len(result["trend"]) == 1


async def test_storage_utilization_trend_aggregates_across_workspaces(session, workspace, other_workspace):
    await monitoring.record_storage_snapshot(session, workspace_id=workspace.workspace_id)
    await monitoring.record_storage_snapshot(session, workspace_id=other_workspace.workspace_id)

    result = await monitoring.storage_utilization(session)
    assert len(result["trend"]) == 1
    assert result["trend"][0]["object_store_bytes"] >= 0


async def test_storage_utilization_trend_excludes_snapshots_outside_window(session, workspace):
    snapshot = await monitoring.record_storage_snapshot(session, workspace_id=workspace.workspace_id)
    snapshot.created_at = datetime.now(UTC) - timedelta(days=monitoring.DEFAULT_STORAGE_TREND_DAYS + 1)
    await session.flush()

    result = await monitoring.storage_utilization(session, workspace_id=workspace.workspace_id)
    assert result["trend"] == []


async def test_purge_storage_snapshots_older_than_removes_expired_rows(session, workspace):
    old = await monitoring.record_storage_snapshot(session, workspace_id=workspace.workspace_id)
    old.created_at = datetime.now(UTC) - timedelta(days=200)
    recent = await monitoring.record_storage_snapshot(session, workspace_id=workspace.workspace_id)
    await session.flush()

    purged = await monitoring.purge_storage_snapshots_older_than(session, days=90)
    assert purged == 1

    result = await monitoring.storage_utilization(session, workspace_id=workspace.workspace_id, trend_days=365)
    assert [e["object_store_bytes"] for e in result["trend"]] == [recent.object_store_bytes]


# --- review_queue_health ----------------------------------------------------------------------


async def test_review_queue_health_groups_by_kind_and_age(session, workspace):
    from karpwiki import review

    old_item = await review.create(
        session, kind=ReviewKind.prune, subject_ref="x", workspace_id=workspace.workspace_id
    )
    old_item.created_at = datetime.now(UTC) - timedelta(hours=48)
    await review.create(
        session, kind=ReviewKind.prune, subject_ref="y", workspace_id=workspace.workspace_id
    )
    await session.flush()

    result = await monitoring.review_queue_health(session, workspace_id=workspace.workspace_id)
    [row] = result["items"]
    assert row["kind"] == "prune"
    assert row["open_count"] == 2
    assert row["oldest_age_hours"] >= 48


# --- queue_depths (real redis) -----------------------------------------------------------------


async def test_queue_depths_returns_all_known_queues():
    """Doesn't push fake data onto a real queue name — this dev environment's real worker
    containers actively consume from these same names on the same broker, so a
    hand-crafted (non-Celery-shaped) message could be popped and crash a live worker.
    Just confirms the function connects and reports every queue `tasks.QUEUES` names."""
    from karpwiki.tasks import QUEUES

    result = await monitoring.queue_depths()
    assert set(result.keys()) == set(QUEUES)
    assert all(isinstance(v, int) and v >= 0 for v in result.values())
