"""Query logging (04 §8, 02 §5) — phase2-tasklist.md step 25.

Every `search` call is recorded here regardless of outcome, including zero-result and
empty-query attempts — 04 §8 says "every search call," not "every successful one."
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from .models import QueryLog

# 09 §8's decision: 90 days, full detail, then purged — the retention window is itself the
# privacy boundary, no separate anonymization step.
RETENTION_DAYS = 90


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


async def purge_older_than(session: AsyncSession, *, days: int = RETENTION_DAYS) -> int:
    """09 §8's retention window. Nothing schedules this yet — it exists to be called once
    the async layer (phase2-tasklist.md step 30+) can run it on a recurring job, the same
    position every other still-manual maintenance operation is in before then."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(delete(QueryLog).where(QueryLog.created_at < cutoff))
    await session.flush()
    return result.rowcount or 0
