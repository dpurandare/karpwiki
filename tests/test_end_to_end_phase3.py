"""Phase 3 step 79 — closing verify. `07` §6's own stated Phase 3 exit criteria: "Admin
staff can run the Platform without manual intervention outside the review queue." Each test
here ties several already-independently-tested features together in one continuous flow,
the same shape `test_end_to_end_2e.py` (Phase 2's own closing verify) already established,
rather than re-testing any one feature in isolation again.

**A real correction to this step's own tasklist text, found while implementing it**: the
tasklist names "a per-tag-scoped reader (step 70)" — step 70 only ever built `page_type`
scoping; `tag` scoping was explicitly deferred at the time (`09` §70's own decision log).
The test below exercises `page_type` scoping, the real feature, and the tasklist text was
corrected to match rather than silently left wrong.

**FUSE mount**: step 58 confirmed via `AskUserQuestion` that actually calling
`fsspec.fuse.run` needs a host kernel FUSE driver (macFUSE) never installed in this
session, by deliberate scope decision — that boundary is not revisited here. What *is*
demonstrated is the real substance of "no gateway round trip": `wiki_mount.
scoped_filesystem` (the same fsspec-backed, read-only view a real FUSE mount would wrap)
reading `index.md` directly from the object store, with no `client`/FastAPI call anywhere
in the test.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from mcp.client.client import Client
from sqlalchemy import select

from karpwiki import (
    ingestion,
    mcp_server,
    objectstore,
    query_log,
    review,
    search,
    tasks,
    versioning,
    wiki_export,
    wiki_mount,
)
from karpwiki.auth import Principal
from karpwiki.models import (
    AccessPolicy,
    FeedbackRating,
    PageStatus,
    PageType,
    PageVersion,
    ReviewItem,
    ReviewKind,
    Role,
)

class _FakeSink:
    def __init__(self):
        self.calls = []

    async def notify_review_sla_breach(self, **kwargs):
        self.calls.append(("review_sla_breach", kwargs))

    async def notify_search_latency_sla_breach(self, **kwargs):
        self.calls.append(("search_latency_sla_breach", kwargs))

    async def notify_source_ingested(self, **kwargs):
        pass

    async def notify_source_rejected(self, **kwargs):
        pass

    async def notify_source_merged(self, **kwargs):
        pass

    async def notify_connector_auth_failure(self, connector, message):
        pass


async def _call(client: Client, name: str, **kwargs):
    result = await client.call_tool(name, kwargs)
    text = result.content[0].text
    return result.is_error


@pytest.fixture
def mcp_client_factory(task_db):
    """Same fixture `test_mcp_server.py` defines — a fresh `Client`/`MCPServer` per call,
    since env-var identity is cached per server instance (mirrors a real stdio process)."""

    def _make():
        return Client(mcp_server.create_mcp_server())

    return _make


# --- 1. A real threshold breach fires a real notification, no admin polling (step 67) ------------


async def test_sla_breach_fires_a_real_notification_with_no_admin_polling(session, workspace, task_db):
    """Nothing in this test ever calls a `/metrics/*` or `/review-items` dashboard —
    the beat-scheduled sweep is what surfaces the breach."""
    item = await review.create(
        session, kind=ReviewKind.duplicate, subject_ref="src-1", workspace_id=workspace.workspace_id
    )
    item.created_at = datetime.now(UTC) - timedelta(hours=10)  # past the default 4h SLA
    await session.commit()

    sink = _FakeSink()
    await tasks._notify_sla_breaches(notification_sink=sink)

    [call] = [c for c in sink.calls if c[0] == "review_sla_breach"]
    _, kwargs = call
    assert kwargs["workspace_id"] == workspace.workspace_id
    assert kwargs["kind"] == "duplicate"
    assert kwargs["oldest_age_hours"] >= 10


# --- 2. A real low-feedback page surfaces to the Advisor, no manual sweep (step 68) ---------------


async def _page(session, workspace, *, title, body):
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=PageType.concept,
        title=title,
        description=f"About {title}.",
        date=date(2026, 8, 20),
        tags=["a", "b"],
        body=body,
        author="system:curator",
        status=PageStatus.published,
    )
    version = await session.get(PageVersion, page.current_version_id)
    await search.index_page(session, page=page, version=version)
    return page


async def test_low_feedback_page_surfaces_via_the_real_scheduled_sweep(session, workspace, task_db):
    """The admin never flags this page directly — real feedback submissions plus the real
    beat-scheduled staleness sweep are the only things that raise the review item."""
    page = await _page(session, workspace, title="Flaky Runbook", body="Body text.")
    entry = await query_log.record(
        session,
        principal="user:x",
        query_text="flaky",
        resolved_workspaces=[workspace.workspace_id],
        results=[{"page_id": str(page.page_id), "score": 1.0}],
    )
    for principal in ("a", "b", "c"):
        await query_log.submit_feedback(
            session,
            query_id=entry.query_id,
            page_id=page.page_id,
            principal=principal,
            rating=FeedbackRating.down,
        )
    await session.commit()

    await tasks._detect_staleness_tiered(workspace.workspace_id)

    review_items = (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace.workspace_id, ReviewItem.kind == ReviewKind.reindex
            )
        )
    ).scalars().all()
    [item] = review_items
    reasons = {p["reason"] for p in item.detail["pages"]}
    assert "low_feedback" in reasons
    assert str(page.page_id) in {p["page_id"] for p in item.detail["pages"]}


# --- 3. A page_type-scoped reader is restricted identically through REST and MCP (step 70) -------


async def test_page_type_scoped_reader_restricted_through_both_rest_and_mcp(
    client, session, workspace, mcp_client_factory, monkeypatch
):
    concept_page = await _page(session, workspace, title="Open Concept", body="Visible to everyone.")
    entity_page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path="entities/restricted-entity.md",
        page_type=PageType.entity,
        title="Restricted Entity",
        description="Scoped content.",
        date=date(2026, 8, 20),
        tags=["a", "b"],
        body="Entity body.",
        author="system:curator",
        status=PageStatus.published,
    )
    # Granting ANY principal a page_type:entity-scoped grant restricts that type for the
    # WHOLE workspace (auth.has_role_for_page's own documented behavior) — "morgan" gets
    # the scoped grant; "casey" (the shared `client` fixture's own default workspace-wide
    # reader) does not, and should now be denied the entity page specifically.
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="morgan",
            role=Role.reader,
            scope="page_type:entity",
        )
    )
    await session.commit()

    # REST: casey sees the unrestricted concept page, not the now-restricted entity page.
    concept_r = await client.get(f"/pages/{concept_page.page_id}", headers={"X-Karpwiki-User": "casey"})
    assert concept_r.status_code == 200
    entity_r = await client.get(f"/pages/{entity_page.page_id}", headers={"X-Karpwiki-User": "casey"})
    assert entity_r.status_code == 403

    # MCP: the identical restriction, same principal, same two pages.
    monkeypatch.setenv("KARPWIKI_MCP_USER", "casey")
    async with mcp_client_factory() as mcp_client:
        concept_denied = await _call(mcp_client, "wiki_get_page", page_id=str(concept_page.page_id))
        entity_denied = await _call(mcp_client, "wiki_get_page", page_id=str(entity_page.page_id))
    assert concept_denied is False
    assert entity_denied is True

    # The scoped grant holder ("morgan") is let through on both surfaces too.
    morgan_entity_r = await client.get(
        f"/pages/{entity_page.page_id}", headers={"X-Karpwiki-User": "morgan"}
    )
    assert morgan_entity_r.status_code == 200
    monkeypatch.setenv("KARPWIKI_MCP_USER", "morgan")
    async with mcp_client_factory() as mcp_client:
        morgan_denied = await _call(mcp_client, "wiki_get_page", page_id=str(entity_page.page_id))
    assert morgan_denied is False


# --- 4. The real, current index.md is directly readable with no gateway round trip (steps 58/60) -


async def test_index_md_is_directly_readable_from_the_object_store_no_gateway(session, workspace):
    await _page(session, workspace, title="Catalogued Concept", body="Content.")
    await ingestion.refresh_index(session, workspace_id=workspace.workspace_id)
    await session.commit()

    # Grant fuse_access for a real AuthZ check, matching step 58's own gate — no gateway
    # call anywhere below, direct fsspec reads only.
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="deepak",
            role=Role.reader,
            fuse_access=True,
        )
    )
    await session.commit()

    await wiki_mount.check_fuse_access(
        session, principal_keys=Principal(id="deepak").policy_keys, workspace_id=workspace.workspace_id
    )

    fs = wiki_mount.scoped_filesystem(workspace.workspace_id)
    content = fs.cat("index.md").decode()
    assert "Catalogued Concept" in content

    # Confirms this is the SAME real content the object store actually holds, read via a
    # second, independent path (objectstore.py directly) — not a coincidence of the
    # scoped view's own implementation.
    direct = objectstore.read_text(wiki_export.export_path(workspace.workspace_id, "index.md"))
    assert content == direct
