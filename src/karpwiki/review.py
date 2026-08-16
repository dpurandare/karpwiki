"""Review item creation (03 §5, §3, §4; 02 §3) — phase1-tasklist step 14.

Three kinds, each created at the point a source needs admin attention:

- `submission` — every new source, unconditionally, at `submitted` (03 §5). Informational;
  nothing resolves it automatically.
- `classification` — the routing gate in `classify.route` refuses (03 §3).
- `duplicate` — `dedup.check` finds a blocking match (03 §4).

`workspace_id` is often `None` here — see `models.ReviewItem`'s docstring for why that's
correct rather than a gap. Resolution (an admin acting on an item) is a later step; this
module only creates rows.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from .models import ReviewItem, ReviewKind


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
