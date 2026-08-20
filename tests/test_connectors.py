"""Connector CRUD (02 §3, 05 §7, 09 §13) — phase2-tasklist.md step 51."""

import inspect

from karpwiki import connectors
from karpwiki.models import AccessPolicy, ConnectorState, Role


async def test_create_grants_the_connector_principal_contributor(session, workspace):
    connector = await connectors.create(session, workspace_id=workspace.workspace_id, type="git")
    policy = await session.get(
        AccessPolicy, (workspace.workspace_id, f"connector:{connector.connector_id}", "")
    )
    assert policy is not None
    assert policy.role is Role.contributor


async def test_create_defaults(session, workspace):
    connector = await connectors.create(session, workspace_id=workspace.workspace_id, type="git")
    assert connector.config == {}
    assert connector.credential_ref is None
    assert connector.schedule == {}
    assert connector.ingestion_policy == "auto"
    assert connector.state is ConnectorState.enabled
    assert connector.last_sync_cursor == {}
    assert connector.last_run_at is None


async def test_create_stores_provided_fields(session, workspace):
    connector = await connectors.create(
        session,
        workspace_id=workspace.workspace_id,
        type="git",
        config={"repo_url": "https://example.com/repo.git", "branch": "main"},
        credential_ref="vault:kv/connectors/git-main",
        schedule={"interval_minutes": 30},
        ingestion_policy="gated",
    )
    assert connector.config == {"repo_url": "https://example.com/repo.git", "branch": "main"}
    assert connector.credential_ref == "vault:kv/connectors/git-main"
    assert connector.schedule == {"interval_minutes": 30}
    assert connector.ingestion_policy == "gated"


async def test_list_for_workspace_scopes_correctly(session, workspace, other_workspace):
    await connectors.create(session, workspace_id=workspace.workspace_id, type="git")
    await connectors.create(session, workspace_id=other_workspace.workspace_id, type="website")

    mine = await connectors.list_for_workspace(session, workspace_id=workspace.workspace_id)
    assert [c.type for c in mine] == ["git"]


async def test_list_for_workspaces_spans_a_set(session, workspace, other_workspace):
    await connectors.create(session, workspace_id=workspace.workspace_id, type="git")
    await connectors.create(session, workspace_id=other_workspace.workspace_id, type="website")

    found = await connectors.list_for_workspaces(
        session, workspace_ids=[workspace.workspace_id, other_workspace.workspace_id]
    )
    assert {c.type for c in found} == {"git", "website"}
    assert await connectors.list_for_workspaces(session, workspace_ids=[]) == []


async def test_update_only_changes_supplied_fields(session, workspace):
    connector = await connectors.create(
        session, workspace_id=workspace.workspace_id, type="git", config={"branch": "main"}
    )
    updated = await connectors.update(session, connector=connector, schedule={"interval_minutes": 15})
    assert updated.schedule == {"interval_minutes": 15}
    assert updated.config == {"branch": "main"}  # untouched
    assert updated.type == "git"  # untouched


async def test_update_disables_a_connector(session, workspace):
    connector = await connectors.create(session, workspace_id=workspace.workspace_id, type="git")
    updated = await connectors.update(session, connector=connector, state=ConnectorState.disabled)
    assert updated.state is ConnectorState.disabled


async def test_update_leaves_workspace_id_unreassignable(session, workspace):
    """No `workspace_id` param exists on `update` at all — 09 §13's "exactly one workspace,
    never several" boundary, unlike `document_types.update`'s reassignment support."""
    assert "workspace_id" not in inspect.signature(connectors.update).parameters
