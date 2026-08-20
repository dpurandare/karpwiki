"""GET /workspace-templates* (07 §5, phase3-tasklist.md step 75)."""

from karpwiki.models import AccessPolicy, Role

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
ADMIN = {"X-Karpwiki-User": "avery"}


async def _grant_admin(session, workspace, principal="avery"):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal=principal, role=Role.admin))
    await session.commit()


async def test_list_templates_requires_only_authentication(client, workspace):
    r = await client.get("/workspace-templates", headers=CONTRIBUTOR)
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["templates"]}
    assert names == {"policy", "engineering-docs"}


async def test_list_templates_rejects_unauthenticated(client):
    r = await client.get("/workspace-templates")
    assert r.status_code == 401


async def test_get_template_returns_ready_to_apply_content(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.get(
        "/workspace-templates/policy", headers=ADMIN, params={"workspace_id": workspace.workspace_id}
    )
    assert r.status_code == 200
    body = r.json()
    assert f"workspace_id: {workspace.workspace_id}" in body["content"]


async def test_get_template_404s_for_an_unknown_template(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.get(
        "/workspace-templates/nonexistent",
        headers=ADMIN,
        params={"workspace_id": workspace.workspace_id},
    )
    assert r.status_code == 404


async def test_get_template_rejects_non_admin(client, session, workspace):
    r = await client.get(
        "/workspace-templates/policy",
        headers=CONTRIBUTOR,
        params={"workspace_id": workspace.workspace_id},
    )
    assert r.status_code == 403


async def test_fetched_template_applies_through_the_existing_schema_endpoint(client, session, workspace):
    """The full real flow this design commits to: fetch, then apply via the pre-existing
    POST /workspaces/{id}/schema — no separate "apply" mechanism needed."""
    await _grant_admin(session, workspace)
    fetched = await client.get(
        "/workspace-templates/engineering-docs",
        headers=ADMIN,
        params={"workspace_id": workspace.workspace_id},
    )
    content = fetched.json()["content"]

    applied = await client.post(
        f"/workspaces/{workspace.workspace_id}/schema",
        headers=ADMIN,
        json={"content": content, "change_summary": "bootstrapped from engineering-docs template"},
    )
    assert applied.status_code == 201
    assert applied.json()["content"] == content

    got = await client.get(f"/workspaces/{workspace.workspace_id}/schema", headers=ADMIN)
    assert got.status_code == 200
    assert "eng.design-doc" in got.json()["content"]
