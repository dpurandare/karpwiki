"""GET /search — federated resolution, taxonomy pre-filter, query_log (04 §1, §4-8) —
phase2-tasklist.md step 25."""

from datetime import date

from sqlalchemy import select

from karpwiki import search, versioning
from karpwiki.models import AccessPolicy, PageStatus, PageType, PageVersion, QueryLog, Role

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
READER = {"X-Karpwiki-User": "casey"}


async def _page(session, workspace, *, title, body, status=PageStatus.published):
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=PageType.concept,
        title=title,
        description=f"About {title}.",
        date=date(2026, 8, 14),
        tags=["a", "b"],
        body=body,
        author="system:curator",
        status=status,
    )
    version = await session.get(PageVersion, page.current_version_id)
    await search.index_page(session, page=page, version=version)
    return page


async def test_search_returns_ranked_cited_results(client, session, workspace):
    await _page(
        session,
        workspace,
        title="Restarting Payments",
        body="Drain the queue, then restart. [^1]\n\n[^1]: restart.pdf, p. 2",
    )

    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "drain"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Restarting Payments"
    assert items[0]["workspace_id"] == workspace.workspace_id
    assert items[0]["citations"] == ["[^1]: restart.pdf, p. 2"]
    assert "drain" in items[0]["excerpt"].lower()


async def test_search_only_covers_accessible_workspaces(client, session, workspace, other_workspace):
    await _page(session, workspace, title="Eng Page", body="shared term")
    await _page(session, other_workspace, title="Policy Page", body="shared term")

    # deepak is contributor on `workspace` only (client fixture) — no grant on other_workspace.
    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "shared"})
    titles = {i["title"] for i in r.json()["items"]}
    assert titles == {"Eng Page"}


async def test_search_federates_across_every_accessible_workspace(client, session, workspace, other_workspace):
    session.add(
        AccessPolicy(workspace_id=other_workspace.workspace_id, principal="deepak", role=Role.reader)
    )
    await session.flush()
    await _page(session, workspace, title="Eng Page", body="shared term")
    await _page(session, other_workspace, title="Policy Page", body="shared term")

    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "shared"})
    titles = {i["title"] for i in r.json()["items"]}
    assert titles == {"Eng Page", "Policy Page"}


async def test_explicit_workspace_id_is_intersected_not_expanded(client, session, workspace, other_workspace):
    """other_workspace is not accessible to deepak — naming it explicitly must not grant it."""
    await _page(session, workspace, title="Eng Page", body="shared term")
    await _page(session, other_workspace, title="Policy Page", body="shared term")

    r = await client.get(
        "/search",
        headers=CONTRIBUTOR,
        params={"q": "shared", "workspace_id": [workspace.workspace_id, other_workspace.workspace_id]},
    )
    titles = {i["title"] for i in r.json()["items"]}
    assert titles == {"Eng Page"}


async def test_taxonomy_prefilter_narrows_to_the_matching_workspace(
    client, session, workspace, other_workspace
):
    """04 §4: a query matching a document_type's own label narrows the default (unscoped)
    search to that type's workspace — `policy.hr` belongs to `other_workspace`."""
    session.add(
        AccessPolicy(workspace_id=other_workspace.workspace_id, principal="deepak", role=Role.reader)
    )
    await session.flush()
    await _page(session, workspace, title="Eng Page", body="policy hr discussion")
    await _page(session, other_workspace, title="Policy Page", body="policy hr discussion")

    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "policy hr"})
    titles = {i["title"] for i in r.json()["items"]}
    assert titles == {"Policy Page"}


async def test_drafts_require_contributor_not_just_reader(client, session, workspace):
    await _page(session, workspace, title="Draft Page", body="shared term", status=PageStatus.draft)

    reader_result = await client.get(
        "/search", headers=READER, params={"q": "shared", "include_drafts": True}
    )
    assert reader_result.json()["items"] == []

    contributor_result = await client.get(
        "/search", headers=CONTRIBUTOR, params={"q": "shared", "include_drafts": True}
    )
    assert [i["title"] for i in contributor_result.json()["items"]] == ["Draft Page"]


async def test_page_type_and_tag_filters(client, session, workspace):
    await _page(session, workspace, title="A Concept", body="shared term")
    entity_page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path="entities/an-entity.md",
        page_type=PageType.entity,
        title="An Entity",
        description="An entity.",
        date=date(2026, 8, 14),
        tags=["a", "b"],
        body="shared term",
        author="system:curator",
        status=PageStatus.published,
    )
    version = await session.get(PageVersion, entity_page.current_version_id)
    await search.index_page(session, page=entity_page, version=version)

    r = await client.get(
        "/search", headers=CONTRIBUTOR, params={"q": "shared", "page_type": ["entity"]}
    )
    assert [i["title"] for i in r.json()["items"]] == ["An Entity"]


async def test_every_search_call_is_logged(client, session, workspace):
    await _page(session, workspace, title="Eng Page", body="shared term")
    await client.get("/search", headers=CONTRIBUTOR, params={"q": "shared"})

    logged = (await session.execute(select(QueryLog))).scalars().all()
    assert len(logged) == 1
    assert logged[0].query_text == "shared"
    assert logged[0].principal == "deepak"
    assert logged[0].resolved_workspaces == [workspace.workspace_id]
    assert len(logged[0].results) == 1


async def test_a_query_with_no_accessible_workspaces_is_still_logged(client, session):
    r = await client.get("/search", headers={"X-Karpwiki-User": "nobody"}, params={"q": "anything"})
    assert r.status_code == 200
    assert r.json()["items"] == []

    logged = (await session.execute(select(QueryLog))).scalars().all()
    assert len(logged) == 1
    assert logged[0].resolved_workspaces == []
