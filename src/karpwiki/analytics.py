"""Usage Analytics (phase3-tasklist.md step 73) — usage trends over time (search volume,
submission volume, active workspaces), building on step 72's real trend-array shape and
step 68's `query_feedback` signal. Not one of `05` §8's five named Performance Monitoring
dashboards (that module stays scoped to operational health) and not listed as a row in
`07` §5's Platform Operations table either — the tasklist step names a real gap in both,
so this is its own module and its own `GET /analytics/*` REST namespace, admin-gated the
same way `/metrics/*` already is.

Every trend here reads directly off existing per-event timestamp columns
(`query_log.created_at`, `raw_source.created_at`, `query_feedback.created_at`) — unlike
step 72's storage figures, none of these needed a new periodic-snapshot table: the
underlying events already carry a real timestamp to bucket by.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_USAGE_TREND_DAYS = 30


async def usage_trends(
    session: AsyncSession, *, workspace_id: str | None = None, window_days: int = DEFAULT_USAGE_TREND_DAYS
) -> dict:
    """Real, ascending-by-date lists within `window_days` — scoped to one workspace when
    given, aggregated across every workspace otherwise (the same optional-scope shape
    `monitoring.py`'s own dashboards already use). `search_volume` counts `query_log` rows
    (a search can resolve several workspaces at once — `resolved_workspaces = ANY`, same
    filter `monitoring.search_performance` already uses); `submission_volume` counts
    `raw_source` rows (a source with no resolved workspace yet only counts toward the
    aggregate, never a scoped view); `feedback` is up/down counts from `query_feedback`,
    joined through `wiki_page` for workspace scoping since feedback rows carry a `page_id`,
    not a `workspace_id` of their own."""
    cutoff = datetime.now(UTC) - timedelta(days=window_days)

    search_filters = ["created_at >= :cutoff"]
    search_params: dict = {"cutoff": cutoff}
    if workspace_id:
        search_filters.append(":workspace_id = ANY(resolved_workspaces)")
        search_params["workspace_id"] = workspace_id
    search_rows = (
        await session.execute(
            text(
                "SELECT date_trunc('day', created_at) AS day, count(*) AS count "
                "FROM query_log "
                f"WHERE {' AND '.join(search_filters)} "
                "GROUP BY day ORDER BY day"
            ),
            search_params,
        )
    ).all()

    submission_filters = ["created_at >= :cutoff"]
    submission_params: dict = {"cutoff": cutoff}
    if workspace_id:
        submission_filters.append("workspace_id = :workspace_id")
        submission_params["workspace_id"] = workspace_id
    submission_rows = (
        await session.execute(
            text(
                "SELECT date_trunc('day', created_at) AS day, count(*) AS count "
                "FROM raw_source "
                f"WHERE {' AND '.join(submission_filters)} "
                "GROUP BY day ORDER BY day"
            ),
            submission_params,
        )
    ).all()

    feedback_join = ""
    feedback_filters = ["qf.created_at >= :cutoff"]
    feedback_params: dict = {"cutoff": cutoff}
    if workspace_id:
        feedback_join = "JOIN wiki_page p ON p.page_id = qf.page_id"
        feedback_filters.append("p.workspace_id = :workspace_id")
        feedback_params["workspace_id"] = workspace_id
    feedback_rows = (
        await session.execute(
            text(
                "SELECT date_trunc('day', qf.created_at) AS day, "
                "       count(*) FILTER (WHERE qf.rating = 'up') AS up, "
                "       count(*) FILTER (WHERE qf.rating = 'down') AS down "
                "FROM query_feedback qf "
                f"{feedback_join} "
                f"WHERE {' AND '.join(feedback_filters)} "
                "GROUP BY day ORDER BY day"
            ),
            feedback_params,
        )
    ).all()

    return {
        "window_days": window_days,
        "search_volume": [{"date": r.day.date().isoformat(), "count": r.count} for r in search_rows],
        "submission_volume": [
            {"date": r.day.date().isoformat(), "count": r.count} for r in submission_rows
        ],
        "feedback": [
            {"date": r.day.date().isoformat(), "up": r.up, "down": r.down} for r in feedback_rows
        ],
    }


async def active_workspaces_trend(
    session: AsyncSession, *, window_days: int = DEFAULT_USAGE_TREND_DAYS
) -> list[dict]:
    """Global only — same reasoning `monitoring.queue_depths()` already established for
    Celery queue depth: "active workspaces" mixes every workspace by definition, so a
    per-workspace scope isn't a coherent question to ask of it. A workspace counts as
    active on a given day if it had a real search or submission event that day —
    `Workspace` itself has no `created_at`/activity column of its own to trend against, so
    activity in these two event tables is the only real signal available."""
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    rows = (
        await session.execute(
            text(
                "SELECT day, count(DISTINCT workspace_id) AS count FROM ("
                "  SELECT date_trunc('day', created_at) AS day, unnest(resolved_workspaces) AS workspace_id "
                "  FROM query_log WHERE created_at >= :cutoff "
                "  UNION ALL "
                "  SELECT date_trunc('day', created_at) AS day, workspace_id "
                "  FROM raw_source WHERE created_at >= :cutoff AND workspace_id IS NOT NULL"
                ") activity "
                "GROUP BY day ORDER BY day"
            ),
            {"cutoff": cutoff},
        )
    ).all()
    return [{"date": r.day.date().isoformat(), "count": r.count} for r in rows]
