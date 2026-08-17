"""GET /search spanning both backends — shared Postgres + dedicated OpenSearch
(04 §4, 02 §4) — phase2-tasklist.md step 26."""

from datetime import date

from sqlalchemy import select

from karpwiki import search, versioning
from karpwiki.models import AccessPolicy, PageStatus, PageType, PageVersion, QueryLog, Role

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
ADMIN = {"X-Karpwiki-User": "avery"}


async def _page(session, workspace, *, title, body):
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
        status=PageStatus.published,
    )
    version = await session.get(PageVersion, page.current_version_id)
    await search.index_page(session, page=page, version=version)
    return page


async def test_search_merges_results_from_both_backends(client, session, workspace, dedicated_workspace):
    await _page(session, workspace, title="Shared Page", body="shared term in postgres")
    session.add(
        AccessPolicy(
            workspace_id=dedicated_workspace.workspace_id, principal="deepak", role=Role.reader
        )
    )
    await session.flush()
    await _page(session, dedicated_workspace, title="Dedicated Page", body="shared term in opensearch")

    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "shared term"})
    assert r.status_code == 200
    titles = {i["title"] for i in r.json()["items"]}
    assert titles == {"Shared Page", "Dedicated Page"}
    workspace_ids = {i["workspace_id"] for i in r.json()["items"]}
    assert workspace_ids == {workspace.workspace_id, dedicated_workspace.workspace_id}


async def test_dedicated_only_result_has_a_normalized_score(client, session, dedicated_workspace):
    session.add(
        AccessPolicy(
            workspace_id=dedicated_workspace.workspace_id, principal="deepak", role=Role.reader
        )
    )
    await session.flush()
    await _page(session, dedicated_workspace, title="Only Page", body="unique dedicated term")

    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "unique dedicated term"})
    items = r.json()["items"]
    assert len(items) == 1
    # A single-result normalization maps the only score to 1.0 (search.merge_federated's
    # zero-range case).
    assert items[0]["score"] == 1.0


async def test_toggling_a_workspace_dedicated_via_the_api_takes_effect_on_new_content(
    client, session, workspace
):
    """The full round trip: an admin flips dedicated_index through POST /workspaces/{id}
    (not a raw DB write), and content indexed *after* that shows up via the dedicated
    backend's search path. (Content indexed *before* the toggle stays wherever it already
    was — this only covers new writes, the same "future routing only" scope 05 §7 already
    gives taxonomy reassignment.)"""
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await session.flush()

    toggle = await client.post(
        f"/workspaces/{workspace.workspace_id}", headers=ADMIN, json={"dedicated_index": True}
    )
    assert toggle.status_code == 200

    await _page(session, workspace, title="Post-Toggle Page", body="routed after toggling")

    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "routed after toggling"})
    assert [i["title"] for i in r.json()["items"]] == ["Post-Toggle Page"]


async def test_query_log_records_the_merged_result_set(client, session, workspace, dedicated_workspace):
    await _page(session, workspace, title="Shared Page", body="shared term")
    session.add(
        AccessPolicy(
            workspace_id=dedicated_workspace.workspace_id, principal="deepak", role=Role.reader
        )
    )
    await session.flush()
    await _page(session, dedicated_workspace, title="Dedicated Page", body="shared term")

    await client.get("/search", headers=CONTRIBUTOR, params={"q": "shared term"})

    logged = (await session.execute(select(QueryLog))).scalars().all()
    assert len(logged) == 1
    assert set(logged[0].resolved_workspaces) == {workspace.workspace_id, dedicated_workspace.workspace_id}
    assert len(logged[0].results) == 2
