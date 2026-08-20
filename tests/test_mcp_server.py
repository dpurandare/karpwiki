"""Phase 2 step 45 — the MCP server (06 §2), a thin adapter over the same Common Gateway
logic the REST endpoints use.

Tool calls go through the real in-process MCP protocol (`mcp.client.client.Client`
connected directly to an `MCPServer` instance, no network) rather than calling the tool
closures directly — they aren't individually importable, and this exercises the real
`Context`/argument-validation machinery a real MCP client would. `ctx.headers` is `None`
over this in-memory transport (same as real stdio), so every test here exercises the
stdio identity path via `KARPWIKI_MCP_USER`/`_GROUPS` env vars; the streamable-HTTP
header path is covered by `_resolve_http_principal`'s own direct unit tests below plus a
real live check (spec/09-implementation-notes.md, not committed).
"""

import json
import uuid
from datetime import date

import pytest
from mcp.client.client import Client
from sqlalchemy import select

from karpwiki import mcp_server, search, versioning
from karpwiki.auth import Principal
from karpwiki.models import AccessPolicy, PageStatus, PageType, PageVersion, RawSourceStatus, Role

DUPLICATE_BODY = "The payments worker drains its queue before restart."


async def _page(session, workspace, *, title, body="Body.", status=PageStatus.published, indexed=False):
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=PageType.concept,
        title=title,
        description=f"About {title}.",
        date=date(2026, 8, 19),
        tags=["a", "b"],
        body=body,
        author="system:curator",
        status=status,
    )
    if indexed:
        version = await session.get(PageVersion, page.current_version_id)
        await search.index_page(session, page=page, version=version)
    return page


async def _call(client: Client, name: str, **kwargs):
    result = await client.call_tool(name, kwargs)
    text = result.content[0].text
    return result.is_error, (json.loads(text) if not result.is_error else text)


@pytest.fixture
def mcp_client_factory(task_db):
    """A fresh `Client` per call, wrapping a fresh `MCPServer` — env-var identity is
    cached per server instance (mirrors a real stdio process), so tests that need a
    different principal need a fresh server, not just a new env var."""

    def _make():
        return Client(mcp_server.create_mcp_server())

    return _make


# --- _resolve_http_principal / _resolve_stdio_principal (direct unit tests) ---------------


class _FakeAuthenticator:
    def __init__(self, principal: Principal | None):
        self._principal = principal

    async def authenticate(self, headers):
        return self._principal


async def test_resolve_http_principal_returns_the_authenticated_principal():
    principal = await mcp_server._resolve_http_principal(
        _FakeAuthenticator(Principal(id="deepak")), {"x-karpwiki-user": "deepak"}
    )
    assert principal.id == "deepak"


async def test_resolve_http_principal_rejects_unauthenticated():
    with pytest.raises(mcp_server.McpAuthError):
        await mcp_server._resolve_http_principal(_FakeAuthenticator(None), {})


async def test_resolve_stdio_principal_uses_env_vars(monkeypatch):
    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")
    monkeypatch.setenv("KARPWIKI_MCP_GROUPS", "eng, ops")
    from karpwiki.auth import TrustedHeaderAuthenticator

    principal = await mcp_server._resolve_stdio_principal(TrustedHeaderAuthenticator())
    assert principal.id == "deepak"
    assert principal.groups == ("eng", "ops")


async def test_resolve_stdio_principal_raises_when_unset(monkeypatch):
    monkeypatch.delenv("KARPWIKI_MCP_USER", raising=False)
    monkeypatch.delenv("KARPWIKI_MCP_TOKEN", raising=False)
    from karpwiki.auth import TrustedHeaderAuthenticator

    with pytest.raises(mcp_server.McpAuthError):
        await mcp_server._resolve_stdio_principal(TrustedHeaderAuthenticator())


async def test_resolve_stdio_principal_prefers_token_when_set(monkeypatch):
    """KARPWIKI_MCP_TOKEN synthesizes an Authorization header, not the trusted-header
    shape — the only env var that can possibly satisfy a real OidcAuthenticator."""
    monkeypatch.setenv("KARPWIKI_MCP_TOKEN", "abc123")
    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")  # should be ignored when a token is set

    captured = {}

    class _CapturingAuthenticator:
        async def authenticate(self, headers):
            captured.update(headers)
            return Principal(id="from-token")

    principal = await mcp_server._resolve_stdio_principal(_CapturingAuthenticator())
    assert principal.id == "from-token"
    assert captured == {"authorization": "Bearer abc123"}


# --- tool registration ----------------------------------------------------------------------


def test_all_ten_tools_are_registered():
    srv = mcp_server.create_mcp_server()
    names = {t.name for t in srv._tool_manager.list_tools()}
    assert names == {
        "wiki_search",
        "wiki_get_page",
        "wiki_list_pages",
        "wiki_list_workspaces",
        "wiki_submit",
        "wiki_get_source_status",
        "wiki_list_review_items",
        "wiki_resolve_review_item",
        "wiki_get_page_versions",
        "wiki_rollback_page",
    }


async def test_tool_call_without_stdio_identity_configured_errors(mcp_client_factory, monkeypatch):
    monkeypatch.delenv("KARPWIKI_MCP_USER", raising=False)
    async with mcp_client_factory() as client:
        is_error, text = await _call(client, "wiki_list_workspaces")
        assert is_error
        assert "KARPWIKI_MCP_USER" in text


# --- wiki_search / wiki_get_page / wiki_list_pages / wiki_list_workspaces -----------------


async def test_wiki_search_finds_a_page(client, session, workspace, mcp_client_factory, monkeypatch):
    await _page(session, workspace, title="Restarting Payments", body=DUPLICATE_BODY, indexed=True)
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")

    async with mcp_client_factory() as mcp_client:
        is_error, body = await _call(mcp_client, "wiki_search", q="payments worker")
    assert not is_error
    assert any(i["path"] == "concepts/restarting-payments.md" for i in body["items"])


async def test_wiki_get_page_draft_requires_contributor(client, session, workspace, mcp_client_factory, monkeypatch):
    page = await _page(session, workspace, title="Draft Page", status=PageStatus.draft)
    await session.commit()

    monkeypatch.setenv("KARPWIKI_MCP_USER", "casey")  # reader only
    async with mcp_client_factory() as mcp_client:
        is_error, _ = await _call(mcp_client, "wiki_get_page", page_id=str(page.page_id))
    assert is_error

    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")  # contributor
    async with mcp_client_factory() as mcp_client:
        is_error, body = await _call(mcp_client, "wiki_get_page", page_id=str(page.page_id))
    assert not is_error
    assert body["title"] == "Draft Page"


async def test_wiki_get_page_resolves_links_the_same_way_the_rest_endpoint_does(
    client, session, workspace, mcp_client_factory, monkeypatch
):
    """01 §3, phase3-tasklist.md step 63 — an agent following a citation via MCP sees the
    same resolved/AuthZ-checked `links` field a REST client does."""
    target = await _page(session, workspace, title="MCP Target")
    linker = await _page(session, workspace, title="MCP Linker", body="See [t](concepts/mcp-target.md).")
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "casey")

    async with mcp_client_factory() as mcp_client:
        is_error, body = await _call(mcp_client, "wiki_get_page", page_id=str(linker.page_id))
    assert not is_error
    assert body["links"] == [
        {
            "page_id": str(target.page_id),
            "workspace_id": workspace.workspace_id,
            "path": "concepts/mcp-target.md",
            "link_type": "cross_reference",
        }
    ]


async def test_wiki_list_pages_filters_by_workspace(
    client, session, workspace, other_workspace, mcp_client_factory, monkeypatch
):
    await _page(session, workspace, title="Mine")
    await versioning.create_page(
        session,
        workspace_id=other_workspace.workspace_id,
        path="concepts/theirs.md",
        page_type=PageType.concept,
        title="Theirs",
        description="d",
        date=date(2026, 8, 19),
        tags=["a", "b"],
        body="Body.",
        author="system:curator",
        status=PageStatus.published,
    )
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")

    async with mcp_client_factory() as mcp_client:
        is_error, body = await _call(mcp_client, "wiki_list_pages", workspace_id=workspace.workspace_id)
    assert not is_error
    assert [i["title"] for i in body["items"]] == ["Mine"]


async def test_wiki_list_workspaces_returns_accessible_only(
    client, session, workspace, other_workspace, mcp_client_factory, monkeypatch
):
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")
    async with mcp_client_factory() as mcp_client:
        is_error, body = await _call(mcp_client, "wiki_list_workspaces")
    assert not is_error
    ids = {w["workspace_id"] for w in body["items"]}
    assert workspace.workspace_id in ids
    assert other_workspace.workspace_id not in ids


# --- wiki_submit / wiki_get_source_status --------------------------------------------------


async def test_wiki_submit_dispatches_classification(
    client, session, workspace, mcp_client_factory, dispatched, monkeypatch
):
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")
    async with mcp_client_factory() as mcp_client:
        is_error, body = await _call(mcp_client, "wiki_submit", text="A brand new runbook.")
    assert not is_error
    assert body["pipeline_state"] == "submitted"
    assert dispatched["classify_source"] == [body["source_id"]]


async def test_wiki_submit_requires_contributor(client, session, workspace, mcp_client_factory, monkeypatch):
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "casey")  # reader only
    async with mcp_client_factory() as mcp_client:
        is_error, _ = await _call(mcp_client, "wiki_submit", text="Should not be allowed.")
    assert is_error


async def test_wiki_get_source_status_submitter_only(
    client, session, workspace, mcp_client_factory, dispatched, monkeypatch
):
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")
    async with mcp_client_factory() as mcp_client:
        _, submitted = await _call(mcp_client, "wiki_submit", text="Mine to check.")

    monkeypatch.setenv("KARPWIKI_MCP_USER", "casey")
    async with mcp_client_factory() as mcp_client:
        is_error, _ = await _call(
            mcp_client, "wiki_get_source_status", source_id=submitted["source_id"]
        )
    assert is_error

    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")
    async with mcp_client_factory() as mcp_client:
        is_error, body = await _call(
            mcp_client, "wiki_get_source_status", source_id=submitted["source_id"]
        )
    assert not is_error
    assert body["source_id"] == submitted["source_id"]


# --- wiki_list_review_items / wiki_resolve_review_item -------------------------------------


async def test_wiki_list_review_items_requires_admin(
    client, session, workspace, mcp_client_factory, dispatched, monkeypatch
):
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "casey")  # reader only
    async with mcp_client_factory() as mcp_client:
        is_error, _ = await _call(mcp_client, "wiki_list_review_items")
    assert is_error


async def test_wiki_resolve_review_item_end_to_end(
    client, session, workspace, mcp_client_factory, dispatched, monkeypatch
):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")
    async with mcp_client_factory() as mcp_client:
        _, submitted = await _call(mcp_client, "wiki_submit", text="Needs a decision.")

    monkeypatch.setenv("KARPWIKI_MCP_USER", "avery")
    async with mcp_client_factory() as mcp_client:
        is_error, items = await _call(
            mcp_client, "wiki_list_review_items", workspace_id=None, kind="submission"
        )
        assert not is_error
        [item] = [i for i in items["items"] if i["subject_ref"] == submitted["source_id"]]

        is_error, body = await _call(
            mcp_client, "wiki_resolve_review_item", review_id=item["review_id"], action="acknowledge"
        )
    assert not is_error
    assert body["status"] == "resolved"


# --- wiki_get_page_versions / wiki_rollback_page --------------------------------------------


async def test_wiki_rollback_page_end_to_end(
    client, session, workspace, mcp_client_factory, dispatched, monkeypatch
):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    page = await _page(session, workspace, title="Rollback Target", body="v1 body")
    original_version_id = page.current_version_id
    await versioning.write_version(
        session, page=page, body="v2 body", author="system:test", trigger=versioning.VersionTrigger.manual_edit
    )
    await session.commit()

    monkeypatch.setenv("KARPWIKI_MCP_USER", "casey")  # reader only
    async with mcp_client_factory() as mcp_client:
        is_error, _ = await _call(
            mcp_client,
            "wiki_get_page_versions",
            page_id=str(page.page_id),
        )
    assert is_error

    monkeypatch.setenv("KARPWIKI_MCP_USER", "avery")
    async with mcp_client_factory() as mcp_client:
        is_error, versions_body = await _call(mcp_client, "wiki_get_page_versions", page_id=str(page.page_id))
        assert not is_error
        assert len(versions_body["items"]) == 2

        is_error, rollback_body = await _call(
            mcp_client,
            "wiki_rollback_page",
            page_id=str(page.page_id),
            target_version_id=str(original_version_id),
        )
    assert not is_error
    assert dispatched["reindex"] == [str(page.page_id)]

    refreshed = await session.get(type(page), page.page_id)
    await session.refresh(refreshed)
    new_version = await session.get(PageVersion, refreshed.current_version_id)
    assert "v1 body" in new_version.content


# --- wiki_submit on-behalf-of delegation (09 §5, phase2-tasklist.md step 46) --------------


async def test_wiki_submit_acting_as_succeeds_when_both_have_contributor(
    client, session, workspace, mcp_client_factory, dispatched, monkeypatch
):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="alice", role=Role.contributor))
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")  # the agent's own credential

    async with mcp_client_factory() as mcp_client:
        is_error, body = await _call(
            mcp_client, "wiki_submit", text="On alice's behalf.", acting_as="user:alice"
        )
    assert not is_error
    assert dispatched["classify_source"] == [body["source_id"]]

    from karpwiki.models import IngestionLog

    source_id = uuid.UUID(body["source_id"])
    entry = (
        await session.execute(
            select(IngestionLog).where(IngestionLog.source_id == source_id, IngestionLog.from_state.is_(None))
        )
    ).scalar_one()
    assert entry.actor == "user:alice"
    assert entry.detail["acting_agent"] == "user:deepak"

    from karpwiki.models import RawSource

    source = await session.get(RawSource, source_id)
    assert source.submitted_by == "user:alice"


async def test_wiki_submit_acting_as_rejects_when_represented_user_lacks_access(
    client, session, workspace, mcp_client_factory, monkeypatch
):
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")  # contributor
    async with mcp_client_factory() as mcp_client:
        # casey is only a reader on `workspace` (client fixture default) -- not enough.
        is_error, text = await _call(
            mcp_client, "wiki_submit", text="Should be rejected.", acting_as="user:casey"
        )
    assert is_error
    assert "contributor" in text


async def test_wiki_submit_acting_as_rejects_when_agent_lacks_access(
    client, session, workspace, mcp_client_factory, monkeypatch
):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="alice", role=Role.contributor))
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "casey")  # reader only -- the agent itself lacks access
    async with mcp_client_factory() as mcp_client:
        is_error, _ = await _call(
            mcp_client, "wiki_submit", text="Should be rejected.", acting_as="user:alice"
        )
    assert is_error


async def test_wiki_submit_acting_as_rejects_malformed_claim(
    client, session, workspace, mcp_client_factory, monkeypatch
):
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")
    async with mcp_client_factory() as mcp_client:
        is_error, text = await _call(mcp_client, "wiki_submit", text="Bad claim.", acting_as="alice")
    assert is_error
    assert "user:<id>" in text


async def test_wiki_submit_without_acting_as_is_unaffected(
    client, session, workspace, mcp_client_factory, dispatched, monkeypatch
):
    """No behavior change to the plain (non-delegated) path — submitted_by is still the
    calling identity itself, no acting_agent detail recorded."""
    await session.commit()
    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")
    async with mcp_client_factory() as mcp_client:
        is_error, body = await _call(mcp_client, "wiki_submit", text="Ordinary submission.")
    assert not is_error

    from karpwiki.models import IngestionLog, RawSource

    source_id = uuid.UUID(body["source_id"])
    source = await session.get(RawSource, source_id)
    assert source.submitted_by == "user:deepak"
    entry = (
        await session.execute(
            select(IngestionLog).where(IngestionLog.source_id == source_id, IngestionLog.from_state.is_(None))
        )
    ).scalar_one()
    assert "acting_agent" not in entry.detail
