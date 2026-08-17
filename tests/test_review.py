"""Review item listing and resolution bookkeeping (05 §1, 02 §5) — phase1-tasklist step 19."""

import pytest
from sqlalchemy import select

from karpwiki import review
from karpwiki.models import AdminActionLog, ReviewKind, ReviewStatus


async def test_list_items_paginates_without_gaps_or_duplicates(session, workspace):
    for i in range(5):
        await review.create(session, kind=ReviewKind.submission, subject_ref=f"src-{i}")
    await session.commit()

    seen = []
    cursor = None
    for _ in range(10):
        items, cursor = await review.list_items(
            session, admin_workspaces=[workspace.workspace_id], limit=2, cursor=cursor
        )
        seen.extend(i.subject_ref for i in items)
        if cursor is None:
            break

    assert sorted(seen) == [f"src-{i}" for i in range(5)]
    assert len(seen) == len(set(seen))


async def test_list_items_includes_workspace_less_items_for_any_admin(session, workspace):
    """09 §22: a submission/classification item with no workspace yet is visible to an
    admin of any workspace, not hidden until one resolves."""
    await review.create(session, kind=ReviewKind.submission, subject_ref="src-1")
    await session.commit()

    items, _ = await review.list_items(session, admin_workspaces=[workspace.workspace_id])
    assert [i.subject_ref for i in items] == ["src-1"]

    items, _ = await review.list_items(session, admin_workspaces=["some-other-workspace"])
    assert [i.subject_ref for i in items] == ["src-1"]


async def test_list_items_excludes_a_workspace_scoped_item_outside_access(session, workspace):
    await review.create(
        session,
        kind=ReviewKind.duplicate,
        subject_ref="src-1",
        workspace_id=workspace.workspace_id,
    )
    await session.commit()

    items, _ = await review.list_items(session, admin_workspaces=["some-other-workspace"])
    assert items == []


async def test_list_items_filters_by_kind_status_and_severity(session, workspace):
    await review.create(session, kind=ReviewKind.submission, subject_ref="src-1")
    dup = await review.create(
        session,
        kind=ReviewKind.duplicate,
        subject_ref="src-2",
        workspace_id=workspace.workspace_id,
        severity="high",
    )
    await session.commit()

    items, _ = await review.list_items(
        session, admin_workspaces=[workspace.workspace_id], kind=ReviewKind.duplicate
    )
    assert [i.subject_ref for i in items] == ["src-2"]

    items, _ = await review.list_items(
        session, admin_workspaces=[workspace.workspace_id], severity="high"
    )
    assert [i.subject_ref for i in items] == ["src-2"]

    await review.resolve(session, item=dup, action="reject", actor="user:admin")
    items, _ = await review.list_items(
        session, admin_workspaces=[workspace.workspace_id], status=ReviewStatus.resolved
    )
    assert [i.subject_ref for i in items] == ["src-2"]
    items, _ = await review.list_items(session, admin_workspaces=[workspace.workspace_id])
    assert [i.subject_ref for i in items] == ["src-1"]


async def test_resolve_closes_the_item_and_writes_the_admin_action_log(session, workspace):
    item = await review.create(
        session,
        kind=ReviewKind.duplicate,
        subject_ref="src-1",
        workspace_id=workspace.workspace_id,
    )
    await session.commit()

    resolved = await review.resolve(
        session, item=item, action="reject", actor="user:admin", detail={"note": "spam"}
    )
    assert resolved.status is ReviewStatus.resolved
    assert resolved.resolved_action == "reject"
    assert resolved.resolved_by == "user:admin"
    assert resolved.resolved_at is not None

    logs = list(
        (
            await session.execute(
                select(AdminActionLog).where(AdminActionLog.subject_ref == str(item.review_id))
            )
        ).scalars()
    )
    assert len(logs) == 1
    assert logs[0].actor == "user:admin"
    assert logs[0].action == "resolve_review_item:duplicate"
    assert logs[0].detail == {"action": "reject", "note": "spam"}


async def test_resolve_rejects_an_already_resolved_item(session, workspace):
    item = await review.create(session, kind=ReviewKind.submission, subject_ref="src-1")
    await session.commit()

    await review.resolve(session, item=item, action="acknowledge", actor="user:admin")
    with pytest.raises(review.AlreadyResolvedError):
        await review.resolve(session, item=item, action="acknowledge", actor="user:admin")
