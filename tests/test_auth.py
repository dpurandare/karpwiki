"""Fine-grained (per-page_type) access control (07 §2, phase3-tasklist.md step 70) —
`auth.py`'s new page-scoped checks. Every other `auth.py` function (`effective_role`,
`has_role`, `any_workspace_with_role`) is exercised indirectly through the REST/MCP
endpoints elsewhere, matching this project's existing convention; these two are pure
enough, and non-trivial enough, to unit-test directly.
"""

from datetime import date

from karpwiki import auth, versioning
from karpwiki.auth import Principal
from karpwiki.models import AccessPolicy, PageStatus, PageType, Role


async def _page(session, workspace, *, page_type=PageType.concept, title="Doc"):
    return await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"{page_type.value}/{title.lower().replace(' ', '-')}.md",
        page_type=page_type,
        title=title,
        description=f"About {title}.",
        date=date(2026, 8, 20),
        tags=["a", "b"],
        body="Body text.",
        author="system:curator",
        status=PageStatus.published,
    )


def test_page_type_scope_format():
    assert auth.page_type_scope(PageType.concept) == "page_type:concept"


async def test_has_role_for_page_unrestricted_falls_back_to_workspace_role(session, workspace):
    page = await _page(session, workspace)
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="user:x", role=Role.reader))
    await session.flush()

    principal = Principal(id="user:x")
    assert await auth.has_role_for_page(
        session, principal=principal, page=page, required=Role.reader
    )
    assert not await auth.has_role_for_page(
        session, principal=principal, page=page, required=Role.contributor
    )


async def test_has_role_for_page_no_workspace_role_at_all(session, workspace):
    page = await _page(session, workspace)
    principal = Principal(id="user:ghost")
    assert not await auth.has_role_for_page(
        session, principal=principal, page=page, required=Role.reader
    )


async def test_has_role_for_page_restricted_type_denies_plain_workspace_reader(session, workspace):
    """The moment ANY scoped grant exists for `concept` in this workspace, the workspace-
    wide role alone stops being enough for a `concept` page — matches `07` §2's "visible
    only to a subset of readers" framing."""
    page = await _page(session, workspace)
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="user:x", role=Role.reader))
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="user:someone-else",
            role=Role.reader,
            scope="page_type:concept",
        )
    )
    await session.flush()

    principal = Principal(id="user:x")
    assert not await auth.has_role_for_page(
        session, principal=principal, page=page, required=Role.reader
    )


async def test_has_role_for_page_restricted_type_grants_the_scoped_principal(session, workspace):
    page = await _page(session, workspace)
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="user:x", role=Role.reader))
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="user:x",
            role=Role.reader,
            scope="page_type:concept",
        )
    )
    await session.flush()

    principal = Principal(id="user:x")
    assert await auth.has_role_for_page(
        session, principal=principal, page=page, required=Role.reader
    )


async def test_has_role_for_page_admin_always_bypasses_restriction(session, workspace):
    page = await _page(session, workspace)
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="user:x", role=Role.admin))
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="user:someone-else",
            role=Role.reader,
            scope="page_type:concept",
        )
    )
    await session.flush()

    principal = Principal(id="user:x")
    assert await auth.has_role_for_page(
        session, principal=principal, page=page, required=Role.reader
    )


async def test_has_role_for_page_scope_grant_via_group(session, workspace):
    page = await _page(session, workspace)
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="user:x", role=Role.reader))
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="group:legal",
            role=Role.reader,
            scope="page_type:concept",
        )
    )
    await session.flush()

    principal = Principal(id="user:x", groups=("group:legal",))
    assert await auth.has_role_for_page(
        session, principal=principal, page=page, required=Role.reader
    )


async def test_visible_page_types_nothing_restricted_returns_every_type(session, workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="user:x", role=Role.reader))
    await session.flush()
    visible = await auth.visible_page_types(
        session, principal=Principal(id="user:x"), workspace_id=workspace.workspace_id, required=Role.reader
    )
    assert visible == set(PageType)


async def test_visible_page_types_excludes_a_restricted_type_with_no_scoped_grant(session, workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="user:x", role=Role.reader))
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="user:someone-else",
            role=Role.reader,
            scope="page_type:concept",
        )
    )
    await session.flush()
    visible = await auth.visible_page_types(
        session, principal=Principal(id="user:x"), workspace_id=workspace.workspace_id, required=Role.reader
    )
    assert PageType.concept not in visible
    assert PageType.entity in visible  # unrestricted type stays visible


async def test_visible_page_types_includes_a_restricted_type_with_a_scoped_grant(session, workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="user:x", role=Role.reader))
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="user:x",
            role=Role.reader,
            scope="page_type:concept",
        )
    )
    await session.flush()
    visible = await auth.visible_page_types(
        session, principal=Principal(id="user:x"), workspace_id=workspace.workspace_id, required=Role.reader
    )
    assert PageType.concept in visible


async def test_visible_page_types_admin_sees_everything(session, workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="user:x", role=Role.admin))
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="user:someone-else",
            role=Role.reader,
            scope="page_type:concept",
        )
    )
    await session.flush()
    visible = await auth.visible_page_types(
        session, principal=Principal(id="user:x"), workspace_id=workspace.workspace_id, required=Role.reader
    )
    assert visible == set(PageType)


async def test_effective_role_scope_default_is_workspace_wide_unchanged(session, workspace):
    """Regression: `effective_role`'s new `scope` parameter defaults to `""`, so every
    pre-step-70 call site (bare `effective_role(...)`) is unaffected."""
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="user:x", role=Role.contributor))
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="user:x",
            role=Role.admin,
            scope="page_type:concept",
        )
    )
    await session.flush()
    held = await auth.effective_role(
        session, principal=Principal(id="user:x"), workspace_id=workspace.workspace_id
    )
    assert held is Role.contributor  # the scoped admin grant must not leak in
