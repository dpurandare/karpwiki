"""`connectors` API (06 §1, 05 §7, 09 §13) — phase2-tasklist.md step 51."""

from karpwiki import connectors
from karpwiki.models import AccessPolicy, Connector, ConnectorState, Role

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
READER = {"X-Karpwiki-User": "casey"}
ADMIN = {"X-Karpwiki-User": "avery"}


async def _grant_admin(session, workspace, principal="avery"):
    session.add(
        AccessPolicy(workspace_id=workspace.workspace_id, principal=principal, role=Role.admin)
    )
    await session.flush()


# --- GET /connectors -----------------------------------------------------------------------


async def test_list_connectors_requires_admin(client, session, workspace):
    r = await client.get("/connectors", headers=READER, params={"workspace_id": workspace.workspace_id})
    assert r.status_code == 403


async def test_list_connectors_for_a_workspace(client, session, workspace, other_workspace):
    await _grant_admin(session, workspace)
    await connectors.create(session, workspace_id=workspace.workspace_id, type="git")
    await connectors.create(session, workspace_id=other_workspace.workspace_id, type="website")
    await session.commit()

    r = await client.get("/connectors", headers=ADMIN, params={"workspace_id": workspace.workspace_id})
    assert r.status_code == 200
    assert [i["type"] for i in r.json()["items"]] == ["git"]


async def test_list_connectors_without_a_filter_spans_every_admin_workspace(
    client, session, workspace, other_workspace
):
    await _grant_admin(session, workspace)
    await _grant_admin(session, other_workspace)
    await connectors.create(session, workspace_id=workspace.workspace_id, type="git")
    await connectors.create(session, workspace_id=other_workspace.workspace_id, type="website")
    await session.commit()

    r = await client.get("/connectors", headers=ADMIN)
    assert r.status_code == 200
    assert {i["type"] for i in r.json()["items"]} == {"git", "website"}


async def test_list_connectors_without_admin_anywhere_is_forbidden(client, session, workspace):
    r = await client.get("/connectors", headers=CONTRIBUTOR)
    assert r.status_code == 403


async def test_list_connectors_respects_limit_and_carries_no_next_cursor(
    client, session, workspace
):
    """Deliberately capped, not cursor-paginated (09 §14, phase3-tasklist.md step 66)."""
    await _grant_admin(session, workspace)
    await connectors.create(session, workspace_id=workspace.workspace_id, type="git")
    await connectors.create(session, workspace_id=workspace.workspace_id, type="website")
    await session.commit()

    r = await client.get(
        "/connectors",
        headers=ADMIN,
        params={"workspace_id": workspace.workspace_id, "limit": 1},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert "next_cursor" not in body


# --- POST /connectors (create) --------------------------------------------------------------


async def test_create_a_connector(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        "/connectors",
        headers=ADMIN,
        json={
            "workspace_id": workspace.workspace_id,
            "type": "git",
            "config": {"repo_url": "https://example.com/repo.git"},
            "credential_ref": "vault:kv/connectors/git-main",
            "schedule": {"interval_minutes": 30},
            "ingestion_policy": "gated",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["type"] == "git"
    assert body["workspace_id"] == workspace.workspace_id
    assert body["config"] == {"repo_url": "https://example.com/repo.git"}
    assert body["credential_ref"] == "vault:kv/connectors/git-main"
    assert body["schedule"] == {"interval_minutes": 30}
    assert body["ingestion_policy"] == "gated"
    assert body["state"] == "enabled"
    assert body["last_run_at"] is None

    connector = await session.get(Connector, body["connector_id"])
    assert connector is not None


async def test_create_grants_the_connector_principal_contributor(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        "/connectors", headers=ADMIN, json={"workspace_id": workspace.workspace_id, "type": "git"}
    )
    assert r.status_code == 201
    connector_id = r.json()["connector_id"]

    policy = await session.get(
        AccessPolicy, (workspace.workspace_id, f"connector:{connector_id}", "")
    )
    assert policy is not None
    assert policy.role is Role.contributor


async def test_create_defaults_ingestion_policy_to_auto(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        "/connectors", headers=ADMIN, json={"workspace_id": workspace.workspace_id, "type": "git"}
    )
    assert r.status_code == 201
    assert r.json()["ingestion_policy"] == "auto"


async def test_create_rejects_an_invalid_ingestion_policy(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        "/connectors",
        headers=ADMIN,
        json={"workspace_id": workspace.workspace_id, "type": "git", "ingestion_policy": "yolo"},
    )
    assert r.status_code == 400


async def test_create_without_admin_in_the_target_workspace_is_forbidden(
    client, session, workspace, other_workspace
):
    await _grant_admin(session, workspace)  # not admin in other_workspace
    r = await client.post(
        "/connectors", headers=ADMIN, json={"workspace_id": other_workspace.workspace_id, "type": "git"}
    )
    assert r.status_code == 403


async def test_create_rejects_a_reader(client, session, workspace):
    r = await client.post(
        "/connectors", headers=READER, json={"workspace_id": workspace.workspace_id, "type": "git"}
    )
    assert r.status_code == 403


# --- POST /connectors/{id} (update) ---------------------------------------------------------


async def test_update_reconfigures_schedule_and_policy(client, session, workspace):
    await _grant_admin(session, workspace)
    connector = await connectors.create(session, workspace_id=workspace.workspace_id, type="git")
    await session.commit()

    r = await client.post(
        f"/connectors/{connector.connector_id}",
        headers=ADMIN,
        json={"schedule": {"interval_minutes": 5}, "ingestion_policy": "gated"},
    )
    assert r.status_code == 200
    assert r.json()["schedule"] == {"interval_minutes": 5}
    assert r.json()["ingestion_policy"] == "gated"


async def test_update_disables_a_connector(client, session, workspace):
    await _grant_admin(session, workspace)
    connector = await connectors.create(session, workspace_id=workspace.workspace_id, type="git")
    await session.commit()

    r = await client.post(
        f"/connectors/{connector.connector_id}", headers=ADMIN, json={"state": "disabled"}
    )
    assert r.status_code == 200
    assert r.json()["state"] == "disabled"
    await session.refresh(connector)
    assert connector.state is ConnectorState.disabled


async def test_update_rejects_an_invalid_ingestion_policy(client, session, workspace):
    await _grant_admin(session, workspace)
    connector = await connectors.create(session, workspace_id=workspace.workspace_id, type="git")
    await session.commit()

    r = await client.post(
        f"/connectors/{connector.connector_id}", headers=ADMIN, json={"ingestion_policy": "yolo"}
    )
    assert r.status_code == 400


async def test_update_on_a_connector_the_admin_does_not_own_is_forbidden(
    client, session, workspace, other_workspace
):
    await _grant_admin(session, other_workspace)  # admin elsewhere, not on `workspace`
    connector = await connectors.create(session, workspace_id=workspace.workspace_id, type="git")
    await session.commit()

    r = await client.post(
        f"/connectors/{connector.connector_id}", headers=ADMIN, json={"state": "disabled"}
    )
    assert r.status_code == 403


async def test_update_a_missing_connector_is_404(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        "/connectors/00000000-0000-0000-0000-000000000000",
        headers=ADMIN,
        json={"state": "disabled"},
    )
    assert r.status_code == 404
