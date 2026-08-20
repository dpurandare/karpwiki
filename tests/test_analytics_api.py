"""Phase 3 step 73 — the `GET /analytics/*` admin usage-trend endpoints."""

from karpwiki.models import AccessPolicy, Role

READER = {"X-Karpwiki-User": "casey"}
ADMIN = {"X-Karpwiki-User": "avery"}


async def test_usage_trends_endpoint_rejects_non_admin(client, session, workspace):
    r = await client.get(
        "/analytics/usage-trends", headers=READER, params={"workspace_id": workspace.workspace_id}
    )
    assert r.status_code == 403


async def test_usage_trends_endpoint_allows_admin_scoped_to_workspace(client, session, workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await session.commit()

    r = await client.get(
        "/analytics/usage-trends", headers=ADMIN, params={"workspace_id": workspace.workspace_id}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["search_volume"] == []
    assert body["submission_volume"] == []
    assert body["feedback"] == []
    assert body["active_workspaces"] == []


async def test_usage_trends_endpoint_rejects_admin_in_a_different_workspace(
    client, session, workspace, other_workspace
):
    session.add(
        AccessPolicy(workspace_id=other_workspace.workspace_id, principal="avery", role=Role.admin)
    )
    await session.commit()

    r = await client.get(
        "/analytics/usage-trends", headers=ADMIN, params={"workspace_id": workspace.workspace_id}
    )
    assert r.status_code == 403


async def test_usage_trends_endpoint_allows_admin_anywhere_when_workspace_omitted(
    client, session, other_workspace
):
    session.add(
        AccessPolicy(workspace_id=other_workspace.workspace_id, principal="avery", role=Role.admin)
    )
    await session.commit()

    r = await client.get("/analytics/usage-trends", headers=ADMIN)
    assert r.status_code == 200


async def test_usage_trends_endpoint_active_workspaces_is_always_global(
    client, session, workspace, other_workspace
):
    """`active_workspaces` is merged in unconditionally, the same way
    `ingestion_pipeline_metrics` always merges in global `queue_depths` regardless of
    `workspace_id` scope."""
    from karpwiki import query_log

    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await session.commit()
    await query_log.record(
        session,
        principal="user:x",
        query_text="q",
        resolved_workspaces=[other_workspace.workspace_id],
        results=[],
    )
    await session.commit()

    r = await client.get(
        "/analytics/usage-trends", headers=ADMIN, params={"workspace_id": workspace.workspace_id}
    )
    assert r.status_code == 200
    assert sum(e["count"] for e in r.json()["active_workspaces"]) == 1
