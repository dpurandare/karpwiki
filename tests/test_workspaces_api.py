"""Workspace and access-policy endpoints (05 §7, 06 §1, §3) — phase2-tasklist.md step 23."""

import karpwiki.api as api_module
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from karpwiki import config
from karpwiki.api import create_app
from karpwiki.models import AccessPolicy, Role, Workspace, WorkspaceStatus

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
READER = {"X-Karpwiki-User": "casey"}
ADMIN = {"X-Karpwiki-User": "avery"}
ROOT = {"X-Karpwiki-User": "root"}


@pytest_asyncio.fixture
async def bare_client(session):
    """Like the `client` fixture, but doesn't pre-create a `workspace` — the bootstrap-
    deadlock tests below (09 §84/§86) need a genuinely empty workspace table."""
    app = create_app()

    async def _one_session():
        yield session

    app.dependency_overrides[api_module._session] = _one_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as http:
        yield http


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


async def test_list_workspaces_respects_limit_and_carries_no_next_cursor(
    client, session, workspace, other_workspace
):
    """Deliberately capped, not cursor-paginated (09 §14, phase3-tasklist.md step 66)."""
    session.add(AccessPolicy(workspace_id=other_workspace.workspace_id, principal="deepak", role=Role.reader))
    await session.commit()

    r = await client.get("/workspaces", headers=CONTRIBUTOR, params={"limit": 1})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert "next_cursor" not in body


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


async def test_bootstrap_deadlock_reproduces_on_an_empty_db_by_default(bare_client):
    """09 §84: with zero workspaces, the admin-somewhere check can never pass for anyone —
    `KARPWIKI_BOOTSTRAP_ADMIN` unset (the default) leaves this exactly as before."""
    r = await bare_client.post(
        "/workspaces", headers=ROOT, json={"workspace_id": "first-ws", "name": "First"}
    )
    assert r.status_code == 403


async def test_bootstrap_admin_creates_the_first_workspace_on_an_empty_db(
    bare_client, monkeypatch
):
    """09 §86: the one escape hatch — a specific configured identity, only while the table
    is genuinely empty. Real admin, not just a one-off pass: manageable right after."""
    monkeypatch.setattr(config, "BOOTSTRAP_ADMIN", "root")
    r = await bare_client.post(
        "/workspaces", headers=ROOT, json={"workspace_id": "first-ws", "name": "First"}
    )
    assert r.status_code == 201

    update = await bare_client.post(
        "/workspaces/first-ws", headers=ROOT, json={"description": "d"}
    )
    assert update.status_code == 200
    assert update.json()["description"] == "d"


async def test_bootstrap_admin_wrong_identity_is_still_403_on_an_empty_db(
    bare_client, monkeypatch
):
    """Naming a specific identity — not just checking the table is empty — is the whole
    point: any other authenticated caller must not be able to race for the first slot."""
    monkeypatch.setattr(config, "BOOTSTRAP_ADMIN", "root")
    r = await bare_client.post(
        "/workspaces", headers=CONTRIBUTOR, json={"workspace_id": "first-ws", "name": "First"}
    )
    assert r.status_code == 403


async def test_bootstrap_admin_bypass_only_applies_while_the_table_is_empty(
    client, session, workspace, monkeypatch
):
    """Not a standing admin grant — once any workspace exists (via the normal path here),
    the configured bootstrap identity gets the same 403 as anyone else without a real
    grant somewhere."""
    monkeypatch.setattr(config, "BOOTSTRAP_ADMIN", "root")
    r = await client.post(
        "/workspaces", headers=ROOT, json={"workspace_id": "second-ws", "name": "Second"}
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
        "fuse_access": False,
        "page_type": None,
    }

    listed = await client.get(f"/workspaces/{workspace.workspace_id}/access-policy", headers=ADMIN)
    principals = {g["principal"] for g in listed.json()["items"]}
    assert "user:morgan" in principals
    assert "avery" in principals  # the admin grant itself is visible too


async def test_list_access_policy_respects_limit_and_carries_no_next_cursor(
    client, session, workspace
):
    """Deliberately capped, not cursor-paginated (09 §14, phase3-tasklist.md step 66)."""
    await _grant_admin(session, workspace)
    await client.post(
        f"/workspaces/{workspace.workspace_id}/access-policy",
        headers=ADMIN,
        json={"principal": "user:morgan", "role": "reader"},
    )
    r = await client.get(
        f"/workspaces/{workspace.workspace_id}/access-policy", headers=ADMIN, params={"limit": 1}
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert "next_cursor" not in body


async def test_grant_access_policy_with_fuse_access(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        f"/workspaces/{workspace.workspace_id}/access-policy",
        headers=ADMIN,
        json={"principal": "user:morgan", "role": "reader", "fuse_access": True},
    )
    assert r.status_code == 201
    assert r.json()["fuse_access"] is True


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


# --- Fine-grained (page_type-scoped) grants (07 §2, phase3-tasklist.md step 70) ----------


async def test_grant_access_policy_with_a_page_type_creates_a_scoped_row(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        f"/workspaces/{workspace.workspace_id}/access-policy",
        headers=ADMIN,
        json={"principal": "user:morgan", "role": "reader", "page_type": "entity"},
    )
    assert r.status_code == 201
    assert r.json()["page_type"] == "entity"


async def test_grant_access_policy_page_type_is_a_separate_row_from_workspace_wide(
    client, session, workspace
):
    await _grant_admin(session, workspace)
    await client.post(
        f"/workspaces/{workspace.workspace_id}/access-policy",
        headers=ADMIN,
        json={"principal": "user:morgan", "role": "reader"},
    )
    await client.post(
        f"/workspaces/{workspace.workspace_id}/access-policy",
        headers=ADMIN,
        json={"principal": "user:morgan", "role": "admin", "page_type": "entity"},
    )
    listed = await client.get(f"/workspaces/{workspace.workspace_id}/access-policy", headers=ADMIN)
    rows = [g for g in listed.json()["items"] if g["principal"] == "user:morgan"]
    assert {(g["page_type"], g["role"]) for g in rows} == {(None, "reader"), ("entity", "admin")}


async def test_revoke_access_policy_with_a_page_type_leaves_the_workspace_wide_grant(
    client, session, workspace
):
    await _grant_admin(session, workspace)
    await client.post(
        f"/workspaces/{workspace.workspace_id}/access-policy",
        headers=ADMIN,
        json={"principal": "user:morgan", "role": "reader"},
    )
    await client.post(
        f"/workspaces/{workspace.workspace_id}/access-policy",
        headers=ADMIN,
        json={"principal": "user:morgan", "role": "reader", "page_type": "entity"},
    )
    r = await client.delete(
        f"/workspaces/{workspace.workspace_id}/access-policy/user:morgan",
        headers=ADMIN,
        params={"page_type": "entity"},
    )
    assert r.status_code == 204

    listed = await client.get(f"/workspaces/{workspace.workspace_id}/access-policy", headers=ADMIN)
    rows = [g for g in listed.json()["items"] if g["principal"] == "user:morgan"]
    assert [(g["page_type"], g["role"]) for g in rows] == [(None, "reader")]


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
