"""Real SCHEMA.md storage endpoints (01 §7, 09 §6, phase3-tasklist.md step 59)."""

from karpwiki.models import AccessPolicy, Role

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
ADMIN = {"X-Karpwiki-User": "avery"}


async def _grant_admin(session, workspace, principal="avery"):
    session.add(
        AccessPolicy(workspace_id=workspace.workspace_id, principal=principal, role=Role.admin)
    )
    await session.flush()


async def test_get_schema_with_none_configured_is_404(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.get(f"/workspaces/{workspace.workspace_id}/schema", headers=ADMIN)
    assert r.status_code == 404


async def test_write_and_get_schema(client, session, workspace):
    await _grant_admin(session, workspace)
    content = f"workspace_id: {workspace.workspace_id}\ningestion_policy: gated\n"
    r = await client.post(
        f"/workspaces/{workspace.workspace_id}/schema",
        headers=ADMIN,
        json={"content": content, "change_summary": "initial schema"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["content"] == content
    assert body["change_summary"] == "initial schema"
    assert body["author"] == "user:avery"

    got = await client.get(f"/workspaces/{workspace.workspace_id}/schema", headers=ADMIN)
    assert got.status_code == 200
    assert got.json()["content"] == content


async def test_write_schema_rejects_invalid_content(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        f"/workspaces/{workspace.workspace_id}/schema", headers=ADMIN, json={"content": "not: [valid"}
    )
    assert r.status_code == 400


async def test_write_schema_rejects_mismatched_workspace_id(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        f"/workspaces/{workspace.workspace_id}/schema",
        headers=ADMIN,
        json={"content": "workspace_id: some-other-ws\n"},
    )
    assert r.status_code == 400


async def test_schema_endpoints_require_admin(client, session, workspace):
    r = await client.get(f"/workspaces/{workspace.workspace_id}/schema", headers=CONTRIBUTOR)
    assert r.status_code == 403
    r = await client.post(
        f"/workspaces/{workspace.workspace_id}/schema",
        headers=CONTRIBUTOR,
        json={"content": f"workspace_id: {workspace.workspace_id}\n"},
    )
    assert r.status_code == 403


async def test_list_schema_versions(client, session, workspace):
    await _grant_admin(session, workspace)
    await client.post(
        f"/workspaces/{workspace.workspace_id}/schema",
        headers=ADMIN,
        json={"content": f"workspace_id: {workspace.workspace_id}\n"},
    )
    await client.post(
        f"/workspaces/{workspace.workspace_id}/schema",
        headers=ADMIN,
        json={"content": f"workspace_id: {workspace.workspace_id}\ningestion_policy: gated\n"},
    )
    r = await client.get(f"/workspaces/{workspace.workspace_id}/schema/versions", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    # Newest first, and list items don't carry full content (matches page-version's own shape).
    assert "content" not in body["items"][0]


async def test_rollback_schema(client, session, workspace):
    await _grant_admin(session, workspace)
    first = await client.post(
        f"/workspaces/{workspace.workspace_id}/schema",
        headers=ADMIN,
        json={"content": f"workspace_id: {workspace.workspace_id}\n"},
    )
    first_id = first.json()["version_id"]
    await client.post(
        f"/workspaces/{workspace.workspace_id}/schema",
        headers=ADMIN,
        json={"content": f"workspace_id: {workspace.workspace_id}\ningestion_policy: gated\n"},
    )

    r = await client.post(
        f"/workspaces/{workspace.workspace_id}/schema/rollback",
        headers=ADMIN,
        json={"target_version_id": first_id},
    )
    assert r.status_code == 200
    assert r.json()["content"] == f"workspace_id: {workspace.workspace_id}\n"
    assert r.json()["restored_from_version_id"] == first_id

    current = await client.get(f"/workspaces/{workspace.workspace_id}/schema", headers=ADMIN)
    assert current.json()["content"] == f"workspace_id: {workspace.workspace_id}\n"


async def test_create_workspace_no_longer_accepts_schema_ref(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        "/workspaces",
        headers=ADMIN,
        json={"workspace_id": "new-ws-api", "name": "New", "schema_ref": "ignored"},
    )
    # Pydantic's default (extra="ignore") silently drops the unknown field rather than
    # erroring — confirms the field simply has no effect any more, not that requests break.
    assert r.status_code == 201
    assert r.json()["schema_ref"] is None
