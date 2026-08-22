"""Query logging (04 §8, 02 §5) — phase2-tasklist.md step 25. Search result feedback
(07 §4, phase3-tasklist.md step 68) lives here too, not a separate module — every feedback
row is meaningless without the `QueryLog` row it references, and there's little enough
logic on either side to warrant splitting them.

Every `search` call is recorded here regardless of outcome, including zero-result and
empty-query attempts — 04 §8 says "every search call," not "every successful one."
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import FeedbackRating, QueryFeedback, QueryLog

# 09 §8's decision: 90 days, full detail, then purged — the retention window is itself the
# privacy boundary, no separate anonymization step.
RETENTION_DAYS = 90


class InvalidFeedbackError(ValueError):
    """The named query call doesn't exist, or `page_id` wasn't among its own results —
    the one data-integrity invariant worth enforcing here (not an AuthZ concern, so it
    stays a plain `ValueError`, matching `ingestion.InvalidResolutionError`'s own split of
    "business-rule check lives in the service function, role check lives in api.py")."""


async def record(
    session: AsyncSession,
    *,
    principal: str,
    query_text: str,
    resolved_workspaces: list[str],
    results: list[dict],
    duration_ms: int | None = None,
) -> QueryLog:
    entry = QueryLog(
        principal=principal,
        query_text=query_text,
        resolved_workspaces=resolved_workspaces,
        results=results,
        duration_ms=duration_ms,
    )
    session.add(entry)
    await session.flush()
    return entry


async def submit_feedback(
    session: AsyncSession,
    *,
    query_id: uuid.UUID,
    page_id: uuid.UUID,
    principal: str,
    rating: FeedbackRating,
) -> QueryFeedback:
    """Record one thumbs-up/down on one result of one search call (07 §4, phase3-tasklist.md
    step 68). `page_id` must be one of that call's own `results` — cheap to check here since
    `results` is already the recorded evidence of what the caller actually saw, and it keeps
    the signal honest (no rating a page nobody was ever shown)."""
    query = await session.get(QueryLog, query_id)
    if query is None:
        raise InvalidFeedbackError(f"no search call {query_id}")
    if not any(r.get("page_id") == str(page_id) for r in query.results):
        raise InvalidFeedbackError(f"page {page_id} was not a result of search call {query_id}")
    entry = QueryFeedback(query_id=query_id, page_id=page_id, principal=principal, rating=rating)
    session.add(entry)
    await session.flush()
    return entry


async def purge_older_than(session: AsyncSession, *, days: int = RETENTION_DAYS) -> int:
    """09 §8's retention window, run on a recurring schedule by
    `tasks.purge_query_log` (`KARPWIKI_QUERY_LOG_PURGE_INTERVAL_HOURS`).

    Deletes each expired call's `query_feedback` rows first: that FK has no
    `ON DELETE CASCADE` (nothing in this schema uses one), so deleting the `query_log`
    row on its own raises `ForeignKeyViolationError` the moment any result was ever
    rated. Purging them together is also what 09 §8 actually requires — a feedback row
    carries its own `principal`, so leaving it behind would keep identifiable search
    activity past the window that is itself the privacy boundary.

    Returns the number of `query_log` rows purged; feedback rows go with them.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    expired = select(QueryLog.query_id).where(QueryLog.created_at < cutoff)
    await session.execute(
        delete(QueryFeedback).where(QueryFeedback.query_id.in_(expired))
    )
    result = await session.execute(delete(QueryLog).where(QueryLog.created_at < cutoff))
    await session.flush()
    return result.rowcount or 0
