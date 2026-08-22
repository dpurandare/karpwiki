"""Query logging (04 §8, 02 §5) and search result feedback (07 §4, phase3-tasklist.md
step 68) — phase2-tasklist.md step 25."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from karpwiki import query_log, versioning
from karpwiki.models import FeedbackRating, PageStatus, PageType, QueryFeedback, QueryLog


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


async def test_record_and_purge_older_than(session):
    entry = await query_log.record(
        session, principal="user:x", query_text="q", resolved_workspaces=["ws"], results=[]
    )
    entry.created_at = datetime.now(UTC) - timedelta(days=100)
    await session.flush()
    purged = await query_log.purge_older_than(session, days=90)
    assert purged == 1
    assert await session.get(QueryLog, entry.query_id) is None


async def test_purge_takes_rated_calls_feedback_with_it(session, workspace):
    """`query_feedback.query_id` has no ON DELETE CASCADE, so purging a rated call used to
    raise ForeignKeyViolationError — and leaving the row behind would keep an identifiable
    `principal` past 09 §8's window, which is itself the privacy boundary."""
    page = await _page(session, workspace)
    rated = await query_log.record(
        session,
        principal="user:x",
        query_text="rated",
        resolved_workspaces=[workspace.workspace_id],
        results=[{"page_id": str(page.page_id), "score": 1.0}],
    )
    await query_log.submit_feedback(
        session,
        query_id=rated.query_id,
        page_id=page.page_id,
        principal="user:x",
        rating=FeedbackRating.down,
    )
    fresh = await query_log.record(
        session, principal="user:x", query_text="fresh", resolved_workspaces=["ws"], results=[]
    )
    rated.created_at = datetime.now(UTC) - timedelta(days=100)
    await session.flush()

    assert await query_log.purge_older_than(session, days=90) == 1
    assert await session.get(QueryLog, rated.query_id) is None
    assert await session.get(QueryLog, fresh.query_id) is not None
    remaining = (
        await session.execute(select(QueryFeedback).where(QueryFeedback.query_id == rated.query_id))
    ).scalars().all()
    assert remaining == []


async def test_submit_feedback_creates_a_row(session, workspace):
    page = await _page(session, workspace)
    entry = await query_log.record(
        session,
        principal="user:x",
        query_text="q",
        resolved_workspaces=[workspace.workspace_id],
        results=[{"page_id": str(page.page_id), "score": 1.0}],
    )
    feedback = await query_log.submit_feedback(
        session, query_id=entry.query_id, page_id=page.page_id, principal="user:x", rating=FeedbackRating.up
    )
    assert feedback.rating is FeedbackRating.up
    assert feedback.query_id == entry.query_id
    assert feedback.page_id == page.page_id


async def test_submit_feedback_rejects_an_unknown_query(session, workspace):
    page = await _page(session, workspace)
    with pytest.raises(query_log.InvalidFeedbackError):
        await query_log.submit_feedback(
            session, query_id=uuid.uuid4(), page_id=page.page_id, principal="user:x", rating=FeedbackRating.up
        )


async def test_submit_feedback_rejects_a_page_not_in_the_results(session, workspace):
    page = await _page(session, workspace, title="Shown")
    other = await _page(session, workspace, title="Not Shown")
    entry = await query_log.record(
        session,
        principal="user:x",
        query_text="q",
        resolved_workspaces=[workspace.workspace_id],
        results=[{"page_id": str(page.page_id), "score": 1.0}],
    )
    with pytest.raises(query_log.InvalidFeedbackError):
        await query_log.submit_feedback(
            session, query_id=entry.query_id, page_id=other.page_id, principal="user:x", rating=FeedbackRating.down
        )


async def test_submit_feedback_allows_more_than_one_rating_over_time(session, workspace):
    """Pure append, no uniqueness constraint (matches every other log stream here)."""
    page = await _page(session, workspace)
    entry = await query_log.record(
        session,
        principal="user:x",
        query_text="q",
        resolved_workspaces=[workspace.workspace_id],
        results=[{"page_id": str(page.page_id), "score": 1.0}],
    )
    first = await query_log.submit_feedback(
        session, query_id=entry.query_id, page_id=page.page_id, principal="user:x", rating=FeedbackRating.up
    )
    second = await query_log.submit_feedback(
        session, query_id=entry.query_id, page_id=page.page_id, principal="user:x", rating=FeedbackRating.down
    )
    assert first.feedback_id != second.feedback_id
