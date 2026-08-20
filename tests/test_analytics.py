"""Usage Analytics (phase3-tasklist.md step 73) — search/submission/feedback volume trends
and the global active-workspaces trend."""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta

from karpwiki import analytics, objectstore, query_log, versioning
from karpwiki.models import FeedbackRating, PageStatus, PageType, RawSource, RawSourceStatus


async def _page(session, workspace, *, title="Doc"):
    return await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=PageType.concept,
        title=title,
        description=f"About {title}.",
        date=date(2026, 8, 20),
        tags=["a", "b"],
        body="Body text.",
        author="system:curator",
        status=PageStatus.published,
    )


async def _source(session, workspace_id, *, days_ago=0):
    source_id = uuid.uuid4()
    key = f"/{workspace_id or '_inbox'}/sources/{source_id}/f.md"
    payload = b"content"
    objectstore.write_bytes(key, payload)
    source = RawSource(
        source_id=source_id,
        workspace_id=workspace_id,
        object_key=key,
        filename="f.md",
        content_hash=hashlib.sha256(payload).hexdigest(),
        submitted_by="user:x",
        status=RawSourceStatus.active,
    )
    session.add(source)
    await session.flush()
    source.created_at = datetime.now(UTC) - timedelta(days=days_ago)
    await session.flush()
    return source


async def _search(session, workspace_id, *, days_ago=0):
    entry = await query_log.record(
        session, principal="user:x", query_text="q", resolved_workspaces=[workspace_id], results=[]
    )
    entry.created_at = datetime.now(UTC) - timedelta(days=days_ago)
    await session.flush()
    return entry


# --- usage_trends: search_volume ----------------------------------------------------------------


async def test_usage_trends_counts_search_volume_scoped(session, workspace, other_workspace):
    await _search(session, workspace.workspace_id)
    await _search(session, other_workspace.workspace_id)

    result = await analytics.usage_trends(session, workspace_id=workspace.workspace_id)
    assert sum(e["count"] for e in result["search_volume"]) == 1


async def test_usage_trends_counts_search_volume_aggregate(session, workspace, other_workspace):
    await _search(session, workspace.workspace_id)
    await _search(session, other_workspace.workspace_id)

    result = await analytics.usage_trends(session)
    assert sum(e["count"] for e in result["search_volume"]) == 2


async def test_usage_trends_excludes_search_outside_window(session, workspace):
    await _search(session, workspace.workspace_id)
    old = await _search(session, workspace.workspace_id)
    old.created_at = datetime.now(UTC) - timedelta(days=100)
    await session.flush()

    result = await analytics.usage_trends(session, workspace_id=workspace.workspace_id, window_days=30)
    assert sum(e["count"] for e in result["search_volume"]) == 1


# --- usage_trends: submission_volume ------------------------------------------------------------


async def test_usage_trends_counts_submission_volume_scoped(session, workspace, other_workspace):
    await _source(session, workspace.workspace_id)
    await _source(session, other_workspace.workspace_id)

    result = await analytics.usage_trends(session, workspace_id=workspace.workspace_id)
    assert sum(e["count"] for e in result["submission_volume"]) == 1


async def test_usage_trends_submission_volume_aggregate_includes_unresolved_workspace(session, workspace):
    await _source(session, workspace.workspace_id)
    await _source(session, None)  # still-submitted, workspace not resolved yet

    result = await analytics.usage_trends(session)
    assert sum(e["count"] for e in result["submission_volume"]) == 2


async def test_usage_trends_submission_volume_scoped_excludes_unresolved_workspace(session, workspace):
    await _source(session, None)

    result = await analytics.usage_trends(session, workspace_id=workspace.workspace_id)
    assert sum(e["count"] for e in result["submission_volume"]) == 0


# --- usage_trends: feedback ----------------------------------------------------------------------


async def test_usage_trends_feedback_counts_up_and_down_scoped(session, workspace):
    page = await _page(session, workspace)
    entry = await query_log.record(
        session,
        principal="user:x",
        query_text="q",
        resolved_workspaces=[workspace.workspace_id],
        results=[{"page_id": str(page.page_id), "score": 1.0}],
    )
    await query_log.submit_feedback(
        session, query_id=entry.query_id, page_id=page.page_id, principal="a", rating=FeedbackRating.up
    )
    await query_log.submit_feedback(
        session, query_id=entry.query_id, page_id=page.page_id, principal="b", rating=FeedbackRating.down
    )
    await query_log.submit_feedback(
        session, query_id=entry.query_id, page_id=page.page_id, principal="c", rating=FeedbackRating.down
    )

    result = await analytics.usage_trends(session, workspace_id=workspace.workspace_id)
    [entry_row] = result["feedback"]
    assert entry_row["up"] == 1
    assert entry_row["down"] == 2


async def test_usage_trends_feedback_scoped_excludes_other_workspace(session, workspace, other_workspace):
    page = await _page(session, other_workspace)
    entry = await query_log.record(
        session,
        principal="user:x",
        query_text="q",
        resolved_workspaces=[other_workspace.workspace_id],
        results=[{"page_id": str(page.page_id), "score": 1.0}],
    )
    await query_log.submit_feedback(
        session, query_id=entry.query_id, page_id=page.page_id, principal="a", rating=FeedbackRating.up
    )

    result = await analytics.usage_trends(session, workspace_id=workspace.workspace_id)
    assert result["feedback"] == []


# --- active_workspaces_trend (global only) -------------------------------------------------------


async def test_active_workspaces_trend_counts_distinct_workspaces_from_search_and_submission(
    session, workspace, other_workspace
):
    await _search(session, workspace.workspace_id)
    await _source(session, other_workspace.workspace_id)

    result = await analytics.active_workspaces_trend(session)
    assert sum(e["count"] for e in result) == 2


async def test_active_workspaces_trend_counts_a_workspace_once_per_day_regardless_of_event_count(
    session, workspace
):
    await _search(session, workspace.workspace_id)
    await _search(session, workspace.workspace_id)
    await _source(session, workspace.workspace_id)

    result = await analytics.active_workspaces_trend(session)
    assert sum(e["count"] for e in result) == 1


async def test_active_workspaces_trend_empty_with_no_activity(session):
    result = await analytics.active_workspaces_trend(session)
    assert result == []
