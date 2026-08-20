"""Document-type taxonomy endpoints (05 §7, 06 §1) — phase2-tasklist.md step 22."""

from karpwiki.models import AccessPolicy, DocumentType, Role

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
ADMIN = {"X-Karpwiki-User": "avery"}


async def _grant_admin(session, workspace, principal="avery"):
    session.add(
        AccessPolicy(workspace_id=workspace.workspace_id, principal=principal, role=Role.admin)
    )
    await session.flush()


async def test_listing_requires_admin_somewhere(client):
    r = await client.get("/document-types", headers=CONTRIBUTOR)
    assert r.status_code == 403


async def test_admin_lists_types_for_their_own_workspace(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.get(
        "/document-types", headers=ADMIN, params={"workspace_id": workspace.workspace_id}
    )
    assert r.status_code == 200
    codes = {i["type_code"] for i in r.json()["items"]}
    assert codes == {"eng.design-doc", "eng.runbook"}


async def test_listing_a_workspace_without_admin_there_is_forbidden(
    client, session, workspace, other_workspace
):
    await _grant_admin(session, workspace)
    r = await client.get(
        "/document-types", headers=ADMIN, params={"workspace_id": other_workspace.workspace_id}
    )
    assert r.status_code == 403


async def test_listing_without_a_filter_spans_every_admin_workspace(
    client, session, workspace, other_workspace
):
    await _grant_admin(session, workspace)
    await _grant_admin(session, other_workspace)
    r = await client.get("/document-types", headers=ADMIN)
    codes = {i["type_code"] for i in r.json()["items"]}
    assert codes == {"eng.design-doc", "eng.runbook", "policy.hr"}


async def test_listing_respects_limit_and_carries_no_next_cursor(client, session, workspace):
    """Deliberately capped, not cursor-paginated (09 §14, phase3-tasklist.md step 66)."""
    await _grant_admin(session, workspace)
    r = await client.get(
        "/document-types",
        headers=ADMIN,
        params={"workspace_id": workspace.workspace_id, "limit": 1},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert "next_cursor" not in body


async def test_create_a_document_type(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        "/document-types",
        headers=ADMIN,
        json={
            "type_code": "eng.postmortem",
            "workspace_id": workspace.workspace_id,
            "description": "Incident postmortems.",
        },
    )
    assert r.status_code == 201
    assert r.json() == {
        "type_code": "eng.postmortem",
        "workspace_id": workspace.workspace_id,
        "description": "Incident postmortems.",
    }
    assert await session.get(DocumentType, "eng.postmortem") is not None


async def test_create_without_admin_in_the_target_workspace_is_forbidden(
    client, session, workspace, other_workspace
):
    await _grant_admin(session, workspace)
    r = await client.post(
        "/document-types",
        headers=ADMIN,
        json={"type_code": "policy.leave", "workspace_id": other_workspace.workspace_id},
    )
    assert r.status_code == 403


async def test_create_rejects_a_duplicate_type_code(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        "/document-types",
        headers=ADMIN,
        json={"type_code": "eng.runbook", "workspace_id": workspace.workspace_id},
    )
    assert r.status_code == 409


async def test_update_renames_and_describes(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        "/document-types/eng.runbook",
        headers=ADMIN,
        json={"new_type_code": "eng.oncall-runbook", "description": "On-call runbooks."},
    )
    assert r.status_code == 200
    assert r.json()["type_code"] == "eng.oncall-runbook"
    assert r.json()["description"] == "On-call runbooks."
    assert await session.get(DocumentType, "eng.runbook") is None


async def test_update_reassigning_requires_admin_in_the_target_workspace(
    client, session, workspace, other_workspace
):
    await _grant_admin(session, workspace)  # not admin in other_workspace
    r = await client.post(
        "/document-types/eng.runbook",
        headers=ADMIN,
        json={"workspace_id": other_workspace.workspace_id},
    )
    assert r.status_code == 403


async def test_update_reassigning_with_admin_in_both_workspaces_succeeds(
    client, session, workspace, other_workspace
):
    await _grant_admin(session, workspace)
    await _grant_admin(session, other_workspace)
    r = await client.post(
        "/document-types/eng.runbook",
        headers=ADMIN,
        json={"workspace_id": other_workspace.workspace_id},
    )
    assert r.status_code == 200
    assert r.json()["workspace_id"] == other_workspace.workspace_id


async def test_update_on_a_type_the_admin_does_not_own_is_forbidden(
    client, session, workspace, other_workspace
):
    await _grant_admin(session, other_workspace)  # admin elsewhere, not on `workspace`
    r = await client.post(
        "/document-types/eng.runbook", headers=ADMIN, json={"description": "hijacked"}
    )
    assert r.status_code == 403


async def test_delete_a_document_type(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.delete("/document-types/eng.runbook", headers=ADMIN)
    assert r.status_code == 204
    assert await session.get(DocumentType, "eng.runbook") is None


async def test_delete_a_missing_type_is_404(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.delete("/document-types/does.not.exist", headers=ADMIN)
    assert r.status_code == 404
