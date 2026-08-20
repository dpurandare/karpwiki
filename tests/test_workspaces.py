"""Workspace CRUD and access-policy management (01 §3, 05 §7, 06 §1) — phase2-tasklist.md
step 23."""

import pytest

from karpwiki import schema, workspaces
from karpwiki.models import AccessPolicy, Role, Workspace, WorkspaceStatus


async def test_create_a_workspace(session):
    ws = await workspaces.create(session, workspace_id="new-ws", name="New Workspace")
    assert ws.status is WorkspaceStatus.active
    assert ws.storage_bindings == {}
    assert await session.get(Workspace, "new-ws") is not None


async def test_create_rejects_a_duplicate_workspace_id(session, workspace):
    with pytest.raises(workspaces.DuplicateWorkspaceError):
        await workspaces.create(session, workspace_id=workspace.workspace_id, name="Dup")


async def test_update_changes_only_supplied_fields(session, workspace):
    original_bindings = workspace.storage_bindings
    updated = await workspaces.update(session, workspace=workspace, description="Updated desc.")
    assert updated.description == "Updated desc."
    assert updated.name == workspace.name  # unchanged
    assert updated.storage_bindings == original_bindings  # unchanged


async def test_update_no_longer_accepts_schema_ref(session, workspace):
    """Since phase3-tasklist.md step 59, schema_ref is derived (the current SchemaVersion's
    id, set only by schema.write()) — no longer a caller-settable free-text pointer."""
    with pytest.raises(TypeError):
        await workspaces.update(session, workspace=workspace, schema_ref="/eng-docs/SCHEMA.md")


async def test_writing_a_real_schema_sets_current_schema_version_id(session, workspace):
    version = await schema.write(
        session,
        workspace=workspace,
        content=f"workspace_id: {workspace.workspace_id}\n",
        author="user:deepak",
    )
    assert workspace.current_schema_version_id == version.version_id


async def test_archive_sets_status(session, workspace):
    archived = await workspaces.archive(session, workspace=workspace)
    assert archived.status is WorkspaceStatus.archived


async def test_grant_creates_a_new_access_policy_row(session, workspace):
    await workspaces.grant(session, workspace_id=workspace.workspace_id, principal="user:new", role=Role.reader)
    grants = await workspaces.list_access(session, workspace_id=workspace.workspace_id)
    assert {(g.principal, g.role) for g in grants} == {("user:new", Role.reader)}


async def test_grant_upgrades_an_existing_grant(session, workspace):
    await workspaces.grant(session, workspace_id=workspace.workspace_id, principal="user:x", role=Role.reader)
    await workspaces.grant(session, workspace_id=workspace.workspace_id, principal="user:x", role=Role.admin)
    grants = await workspaces.list_access(session, workspace_id=workspace.workspace_id)
    assert [(g.principal, g.role) for g in grants] == [("user:x", Role.admin)]


async def test_grant_defaults_fuse_access_to_false(session, workspace):
    granted = await workspaces.grant(
        session, workspace_id=workspace.workspace_id, principal="user:new", role=Role.reader
    )
    assert granted.fuse_access is False


async def test_grant_sets_fuse_access_on_a_new_grant(session, workspace):
    granted = await workspaces.grant(
        session,
        workspace_id=workspace.workspace_id,
        principal="user:new",
        role=Role.reader,
        fuse_access=True,
    )
    assert granted.fuse_access is True


async def test_grant_updates_fuse_access_on_an_existing_grant(session, workspace):
    await workspaces.grant(session, workspace_id=workspace.workspace_id, principal="user:x", role=Role.reader)
    updated = await workspaces.grant(
        session,
        workspace_id=workspace.workspace_id,
        principal="user:x",
        role=Role.reader,
        fuse_access=True,
    )
    assert updated.fuse_access is True


async def test_grant_omitting_fuse_access_leaves_it_unchanged(session, workspace):
    await workspaces.grant(
        session,
        workspace_id=workspace.workspace_id,
        principal="user:x",
        role=Role.reader,
        fuse_access=True,
    )
    updated = await workspaces.grant(
        session, workspace_id=workspace.workspace_id, principal="user:x", role=Role.admin
    )
    assert updated.role is Role.admin
    assert updated.fuse_access is True  # not touched — fuse_access wasn't passed


async def test_revoke_removes_a_grant(session, workspace):
    await workspaces.grant(session, workspace_id=workspace.workspace_id, principal="user:x", role=Role.reader)
    await workspaces.revoke(session, workspace_id=workspace.workspace_id, principal="user:x")
    assert await workspaces.list_access(session, workspace_id=workspace.workspace_id) == []


async def test_revoke_a_missing_grant_is_a_no_op(session, workspace):
    await workspaces.revoke(session, workspace_id=workspace.workspace_id, principal="user:ghost")


# --- scope (page_type-scoped grants, 07 §2, phase3-tasklist.md step 70) ------------------


async def test_grant_defaults_scope_to_workspace_wide(session, workspace):
    granted = await workspaces.grant(
        session, workspace_id=workspace.workspace_id, principal="user:new", role=Role.reader
    )
    assert granted.scope == ""


async def test_grant_with_a_scope_is_a_separate_row_from_the_workspace_wide_grant(session, workspace):
    await workspaces.grant(session, workspace_id=workspace.workspace_id, principal="user:x", role=Role.reader)
    await workspaces.grant(
        session,
        workspace_id=workspace.workspace_id,
        principal="user:x",
        role=Role.admin,
        scope="page_type:policy",
    )
    grants = await workspaces.list_access(session, workspace_id=workspace.workspace_id)
    assert {(g.scope, g.role) for g in grants} == {
        ("", Role.reader),
        ("page_type:policy", Role.admin),
    }


async def test_grant_upgrades_an_existing_scoped_grant_not_the_workspace_wide_one(session, workspace):
    await workspaces.grant(session, workspace_id=workspace.workspace_id, principal="user:x", role=Role.reader)
    await workspaces.grant(
        session,
        workspace_id=workspace.workspace_id,
        principal="user:x",
        role=Role.reader,
        scope="page_type:policy",
    )
    await workspaces.grant(
        session,
        workspace_id=workspace.workspace_id,
        principal="user:x",
        role=Role.admin,
        scope="page_type:policy",
    )
    grants = await workspaces.list_access(session, workspace_id=workspace.workspace_id)
    assert {(g.scope, g.role) for g in grants} == {
        ("", Role.reader),
        ("page_type:policy", Role.admin),
    }


async def test_revoke_with_a_scope_removes_only_that_row(session, workspace):
    await workspaces.grant(session, workspace_id=workspace.workspace_id, principal="user:x", role=Role.reader)
    await workspaces.grant(
        session,
        workspace_id=workspace.workspace_id,
        principal="user:x",
        role=Role.admin,
        scope="page_type:policy",
    )
    await workspaces.revoke(
        session, workspace_id=workspace.workspace_id, principal="user:x", scope="page_type:policy"
    )
    grants = await workspaces.list_access(session, workspace_id=workspace.workspace_id)
    assert {(g.scope, g.role) for g in grants} == {("", Role.reader)}


async def test_list_for_principal_returns_only_accessible_workspaces(session, workspace, other_workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="user:deepak", role=Role.reader))
    await session.flush()
    result = await workspaces.list_for_principal(session, principal_keys=("user:deepak",))
    assert [w.workspace_id for w in result] == [workspace.workspace_id]


async def test_list_for_principal_includes_group_grants(session, workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="group:eng", role=Role.contributor))
    await session.flush()
    result = await workspaces.list_for_principal(session, principal_keys=("user:deepak", "group:eng"))
    assert [w.workspace_id for w in result] == [workspace.workspace_id]


# --- Deliberately capped, not cursor-paginated (09 §14, phase3-tasklist.md step 66) ------


async def test_list_for_principal_respects_limit(session, workspace, other_workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="user:deepak", role=Role.reader))
    session.add(AccessPolicy(workspace_id=other_workspace.workspace_id, principal="user:deepak", role=Role.reader))
    await session.flush()
    result = await workspaces.list_for_principal(session, principal_keys=("user:deepak",), limit=1)
    assert len(result) == 1


async def test_list_access_respects_limit(session, workspace):
    await workspaces.grant(session, workspace_id=workspace.workspace_id, principal="user:a", role=Role.reader)
    await workspaces.grant(session, workspace_id=workspace.workspace_id, principal="user:b", role=Role.reader)
    result = await workspaces.list_access(session, workspace_id=workspace.workspace_id, limit=1)
    assert len(result) == 1
