"""GET /search spanning both backends — shared Postgres + dedicated OpenSearch
(04 §4, 02 §4) — phase2-tasklist.md step 26."""

from datetime import date

from sqlalchemy import select

from karpwiki import dedicated_index, search, versioning
from karpwiki.models import AccessPolicy, PageStatus, PageType, PageVersion, QueryLog, Role

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
READER = {"X-Karpwiki-User": "casey"}
ADMIN = {"X-Karpwiki-User": "avery"}


async def _page(session, workspace, *, title, body, page_type=PageType.concept):
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=page_type,
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


# --- Partial failure (09 §14, phase3-tasklist.md step 62) -------------------------------


async def test_a_down_dedicated_backend_degrades_gracefully(
    client, session, workspace, dedicated_workspace, monkeypatch
):
    """One unavailable index must not fail the whole query (09 §14) — a real 200 with the
    shared-index results it still has, `partial: true`, and the down workspace named."""
    await _page(session, workspace, title="Shared Page", body="shared term")
    session.add(
        AccessPolicy(
            workspace_id=dedicated_workspace.workspace_id, principal="deepak", role=Role.reader
        )
    )
    await session.flush()

    async def _broken_search(**_kwargs):
        raise ConnectionError("opensearch unreachable")

    monkeypatch.setattr(dedicated_index, "search", _broken_search)

    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "shared term"})
    assert r.status_code == 200
    body = r.json()
    assert body["partial"] is True
    assert body["unavailable"] == [dedicated_workspace.workspace_id]
    # The shared index is still up — its results are still served, not dropped.
    assert [i["title"] for i in body["items"]] == ["Shared Page"]


async def test_a_down_shared_backend_degrades_gracefully_and_the_session_recovers(
    client, session, workspace, dedicated_workspace, monkeypatch
):
    """The shared-index failure path also has to roll the session back and keep working —
    proven by the `query_log` write (after the failure) succeeding, not just the response."""
    session.add(
        AccessPolicy(
            workspace_id=dedicated_workspace.workspace_id, principal="deepak", role=Role.reader
        )
    )
    await session.flush()
    await _page(session, dedicated_workspace, title="Dedicated Page", body="shared term")

    async def _broken_search(*_args, **_kwargs):
        raise ConnectionError("postgres unreachable")

    monkeypatch.setattr(search, "search", _broken_search)

    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "shared term"})
    assert r.status_code == 200
    body = r.json()
    assert body["partial"] is True
    assert body["unavailable"] == [workspace.workspace_id]
    assert [i["title"] for i in body["items"]] == ["Dedicated Page"]

    logged = (await session.execute(select(QueryLog))).scalars().all()
    assert len(logged) == 1  # the query_log write after the failure still succeeded


async def test_a_fully_successful_search_never_carries_the_partial_field(client, session, workspace):
    """09 §14: "single-workspace operations... never carry the field" — read here as the
    non-degraded case never carrying it either, not just always-false."""
    await _page(session, workspace, title="Shared Page", body="shared term")

    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "shared term"})
    assert r.status_code == 200
    body = r.json()
    assert "partial" not in body
    assert "unavailable" not in body


async def test_both_backends_down_returns_a_fully_partial_empty_result(
    client, session, workspace, dedicated_workspace, monkeypatch
):
    session.add(
        AccessPolicy(
            workspace_id=dedicated_workspace.workspace_id, principal="deepak", role=Role.reader
        )
    )
    await session.flush()

    async def _broken(*_args, **_kwargs):
        raise ConnectionError("unreachable")

    monkeypatch.setattr(search, "search", _broken)
    monkeypatch.setattr(dedicated_index, "search", _broken)

    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "anything"})
    assert r.status_code == 200
    body = r.json()
    assert body["partial"] is True
    assert set(body["unavailable"]) == {workspace.workspace_id, dedicated_workspace.workspace_id}
    assert body["items"] == []


# --- Fine-grained (page_type) access control (07 §2, phase3-tasklist.md step 70) ---------


async def test_search_omits_a_scope_restricted_result_for_a_plain_reader(client, session, workspace):
    await _page(session, workspace, title="Open Concept", body="widget rollout notes")
    await _page(
        session, workspace, title="Restricted Entity", body="widget rollout notes", page_type=PageType.entity
    )
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="someone-else",
            role=Role.reader,
            scope="page_type:entity",
        )
    )
    await session.commit()

    r = await client.get("/search", headers=READER, params={"q": "widget rollout"})
    assert r.status_code == 200
    titles = {i["title"] for i in r.json()["items"]}
    assert titles == {"Open Concept"}


async def test_search_includes_a_restricted_result_for_the_scoped_reader(client, session, workspace):
    await _page(
        session, workspace, title="Restricted Entity", body="widget rollout notes", page_type=PageType.entity
    )
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="casey",
            role=Role.reader,
            scope="page_type:entity",
        )
    )
    await session.commit()

    r = await client.get("/search", headers=READER, params={"q": "widget rollout"})
    assert r.status_code == 200
    titles = {i["title"] for i in r.json()["items"]}
    assert titles == {"Restricted Entity"}


# --- Search result feedback loop (07 §4, phase3-tasklist.md step 68) ---------------------


async def test_search_response_carries_a_query_id(client, session, workspace):
    await _page(session, workspace, title="A Page", body="findable term")
    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "findable term"})
    assert r.status_code == 200
    assert r.json()["query_id"]


async def test_submit_feedback_succeeds_for_the_querying_principal(client, session, workspace):
    page = await _page(session, workspace, title="A Page", body="findable term")
    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "findable term"})
    query_id = r.json()["query_id"]

    fb = await client.post(
        f"/search/{query_id}/feedback",
        headers=CONTRIBUTOR,
        json={"page_id": str(page.page_id), "rating": "up"},
    )
    assert fb.status_code == 201
    assert fb.json()["rating"] == "up"
    assert fb.json()["query_id"] == query_id


async def test_submit_feedback_forbidden_for_a_different_principal(client, session, workspace):
    page = await _page(session, workspace, title="A Page", body="findable term")
    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "findable term"})
    query_id = r.json()["query_id"]

    fb = await client.post(
        f"/search/{query_id}/feedback",
        headers=READER,
        json={"page_id": str(page.page_id), "rating": "up"},
    )
    assert fb.status_code == 403


async def test_submit_feedback_rejects_a_page_not_shown(client, session, workspace):
    await _page(session, workspace, title="Shown", body="findable term")
    other = await _page(session, workspace, title="Not Shown", body="unrelated")
    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "findable term"})
    query_id = r.json()["query_id"]

    fb = await client.post(
        f"/search/{query_id}/feedback",
        headers=CONTRIBUTOR,
        json={"page_id": str(other.page_id), "rating": "down"},
    )
    assert fb.status_code == 400


async def test_submit_feedback_unknown_query_is_404(client, session, workspace):
    import uuid

    fb = await client.post(
        f"/search/{uuid.uuid4()}/feedback",
        headers=CONTRIBUTOR,
        json={"page_id": str(uuid.uuid4()), "rating": "up"},
    )
    assert fb.status_code == 404
