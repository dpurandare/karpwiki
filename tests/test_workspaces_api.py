"""Workspace and access-policy endpoints (05 §7, 06 §1, §3) — phase2-tasklist.md step 23."""

from karpwiki.models import AccessPolicy, Role, Workspace, WorkspaceStatus

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
READER = {"X-Karpwiki-User": "casey"}
ADMIN = {"X-Karpwiki-User": "avery"}


async def _grant_admin(session, workspace, principal="avery"):
    session.add(
        AccessPolicy(workspace_id=workspace.workspace_id, principal=principal, role=Role.admin)
    )
    await session.flush()


async def test_list_returns_only_accessible_workspaces(client, session, workspace, other_workspace):
    r = await client.get("/workspaces", headers=CONTRIBUTOR)
    assert r.status_code == 200
    ids = {w["workspace_id"] for w in r.json()["items"]}
    assert ids == {workspace.workspace_id}  # deepak is contributor on `workspace` only


async def test_get_a_workspace_the_caller_cannot_access_is_404(client, session, other_workspace):
    r = await client.get(f"/workspaces/{other_workspace.workspace_id}", headers=CONTRIBUTOR)
    assert r.status_code == 404


async def test_get_a_workspace_reader_can_access(client, session, workspace):
    r = await client.get(f"/workspaces/{workspace.workspace_id}", headers=READER)
    assert r.status_code == 200
    assert r.json()["workspace_id"] == workspace.workspace_id
    assert r.json()["status"] == WorkspaceStatus.active.value


async def test_create_requires_admin_somewhere(client):
    r = await client.post(
        "/workspaces", headers=CONTRIBUTOR, json={"workspace_id": "new-ws", "name": "New"}
    )
    assert r.status_code == 403


async def test_create_grants_the_creator_admin_so_it_is_manageable(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        "/workspaces",
        headers=ADMIN,
        json={"workspace_id": "brand-new", "name": "Brand New", "description": "d"},
    )
    assert r.status_code == 201
    assert r.json()["status"] == WorkspaceStatus.active.value

    # The creator can now administer it — e.g. update it — with no separate grant step.
    update = await client.post(
        "/workspaces/brand-new", headers=ADMIN, json={"description": "updated"}
    )
    assert update.status_code == 200
    assert update.json()["description"] == "updated"


async def test_create_rejects_a_duplicate_workspace_id(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        "/workspaces", headers=ADMIN, json={"workspace_id": workspace.workspace_id, "name": "Dup"}
    )
    assert r.status_code == 409


async def test_update_requires_admin_in_that_workspace(client, session, workspace):
    r = await client.post(
        "/workspaces/" + workspace.workspace_id, headers=CONTRIBUTOR, json={"description": "x"}
    )
    assert r.status_code == 403


async def test_archive_a_workspace(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(f"/workspaces/{workspace.workspace_id}/archive", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["status"] == WorkspaceStatus.archived.value

    stored = await session.get(Workspace, workspace.workspace_id)
    assert stored.status is WorkspaceStatus.archived


async def test_grant_and_list_access_policy(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        f"/workspaces/{workspace.workspace_id}/access-policy",
        headers=ADMIN,
        json={"principal": "user:morgan", "role": "reader"},
    )
    assert r.status_code == 201
    assert r.json() == {
        "workspace_id": workspace.workspace_id,
        "principal": "user:morgan",
        "role": "reader",
    }

    listed = await client.get(f"/workspaces/{workspace.workspace_id}/access-policy", headers=ADMIN)
    principals = {g["principal"] for g in listed.json()["items"]}
    assert "user:morgan" in principals
    assert "avery" in principals  # the admin grant itself is visible too


async def test_revoke_access_policy(client, session, workspace):
    await _grant_admin(session, workspace)
    await client.post(
        f"/workspaces/{workspace.workspace_id}/access-policy",
        headers=ADMIN,
        json={"principal": "user:morgan", "role": "reader"},
    )
    r = await client.delete(
        f"/workspaces/{workspace.workspace_id}/access-policy/user:morgan", headers=ADMIN
    )
    assert r.status_code == 204

    listed = await client.get(f"/workspaces/{workspace.workspace_id}/access-policy", headers=ADMIN)
    assert "user:morgan" not in {g["principal"] for g in listed.json()["items"]}


async def test_access_policy_management_requires_admin(client, session, workspace):
    r = await client.get(f"/workspaces/{workspace.workspace_id}/access-policy", headers=CONTRIBUTOR)
    assert r.status_code == 403

    r = await client.post(
        f"/workspaces/{workspace.workspace_id}/access-policy",
        headers=CONTRIBUTOR,
        json={"principal": "user:morgan", "role": "admin"},
    )
    assert r.status_code == 403


async def test_admin_switches_a_workspace_onto_the_dedicated_backend(client, session, workspace):
    """phase2-tasklist.md step 26: dedicated_index has to be reachable through the API, or
    an admin has no way to opt a workspace in short of a raw DB write."""
    await _grant_admin(session, workspace)
    assert (await session.get(Workspace, workspace.workspace_id)).dedicated_index is False

    r = await client.post(
        f"/workspaces/{workspace.workspace_id}", headers=ADMIN, json={"dedicated_index": True}
    )
    assert r.status_code == 200
    assert r.json()["dedicated_index"] is True

    stored = await session.get(Workspace, workspace.workspace_id)
    assert stored.dedicated_index is True
