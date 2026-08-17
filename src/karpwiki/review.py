"""Review item creation and resolution (03 §5, §3, §4; 02 §3; 05 §1) — phase1-tasklist
steps 14 and 19.

Three kinds, each created at the point a source needs admin attention:

- `submission` — every new source, unconditionally, at `submitted` (03 §5). Informational;
  nothing resolves it automatically.
- `classification` — the routing gate in `classify.route` refuses (03 §3).
- `duplicate` — `dedup.check` finds a blocking match (03 §4).

`workspace_id` is often `None` here — see `models.ReviewItem`'s docstring for why that's
correct rather than a gap.

Resolution is generic bookkeeping only: closing the item and writing the audit trail
(05 §1's `admin_action_log`). The kind-specific pipeline side effects a resolution
triggers — routing a classification, acting on a duplicate — live in `ingestion.py`,
which calls `resolve` here once its own work is done; keeping that split avoids a circular
import (`ingestion.py` already imports this module).
"""

import base64
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AdminActionLog, ReviewItem, ReviewKind, ReviewStatus

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


class AlreadyResolvedError(ValueError):
    """The item is not `open` — resolving it again would silently overwrite the record."""


async def create(
    session: AsyncSession,
    *,
    kind: ReviewKind,
    subject_ref: str,
    workspace_id: str | None = None,
    severity: str | None = None,
    proposed_action: str | None = None,
) -> ReviewItem:
    item = ReviewItem(
        workspace_id=workspace_id,
        kind=kind,
        subject_ref=subject_ref,
        severity=severity,
        proposed_action=proposed_action,
    )
    session.add(item)
    await session.flush()
    return item


async def list_items(
    session: AsyncSession,
    *,
    admin_workspaces: list[str],
    workspace_id: str | None = None,
    kind: ReviewKind | None = None,
    status: ReviewStatus | None = ReviewStatus.open,
    severity: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> tuple[list[ReviewItem], str | None]:
    """05 §1's consolidated queue: every kind, filterable, across workspaces the caller
    can access — plus items with no workspace yet (09 §22 explains why that's not a gap).

    Cursor-paginated per 09 §14: newest first, `(created_at, review_id)` as the sort key
    and tiebreak, `limit` capped at `MAX_LIST_LIMIT`.
    """
    limit = min(limit, MAX_LIST_LIMIT)
    stmt = select(ReviewItem).where(
        (ReviewItem.workspace_id.is_(None)) | (ReviewItem.workspace_id.in_(admin_workspaces))
    )
    if workspace_id is not None:
        stmt = stmt.where(ReviewItem.workspace_id == workspace_id)
    if kind is not None:
        stmt = stmt.where(ReviewItem.kind == kind)
    if status is not None:
        stmt = stmt.where(ReviewItem.status == status)
    if severity is not None:
        stmt = stmt.where(ReviewItem.severity == severity)
    if cursor is not None:
        created_at, review_id = _decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(ReviewItem.created_at, ReviewItem.review_id) < tuple_(created_at, review_id)
        )

    stmt = stmt.order_by(ReviewItem.created_at.desc(), ReviewItem.review_id.desc()).limit(
        limit + 1
    )
    items = list((await session.execute(stmt)).scalars())

    next_cursor = None
    if len(items) > limit:
        items = items[:limit]
        last = items[-1]
        next_cursor = _encode_cursor(last.created_at, last.review_id)
    return items, next_cursor


def _encode_cursor(created_at, review_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{review_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    created_at, review_id = raw.rsplit("|", 1)
    return datetime.fromisoformat(created_at), uuid.UUID(review_id)


async def resolve(
    session: AsyncSession, *, item: ReviewItem, action: str, actor: str, detail: dict | None = None
) -> ReviewItem:
    """Close an open item and write its `admin_action_log` entry (05 §1).

    Generic on purpose — no kind-specific validation or side effect happens here; the
    caller (`ingestion.py`) has already done that work by the time this runs.
    """
    if item.status is not ReviewStatus.open:
        raise AlreadyResolvedError(f"review item {item.review_id} is already {item.status.value}")

    item.status = ReviewStatus.resolved
    item.resolved_action = action
    item.resolved_by = actor
    item.resolved_at = datetime.now(UTC)

    session.add(
        AdminActionLog(
            actor=actor,
            action=f"resolve_review_item:{item.kind.value}",
            workspace_id=item.workspace_id,
            subject_ref=str(item.review_id),
            detail={"action": action, **(detail or {})},
        )
    )
    await session.flush()
    return item
