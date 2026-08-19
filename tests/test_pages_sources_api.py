"""Phase 2 step 43 — completing the REST surface: `pages` get/list (06 §1, never built
until now), the admin Raw Source Browser (`GET /sources`, 05 §7), and stubbed `connectors`
list/configure (real implementation lands with track 2e's `Connector` model, step 51).
"""

import hashlib
import uuid
from datetime import date

from karpwiki import objectstore, versioning
from karpwiki.models import AccessPolicy, PageStatus, PageType, RawSource, RawSourceStatus, Role

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
READER = {"X-Karpwiki-User": "casey"}
ADMIN = {"X-Karpwiki-User": "avery"}


async def _page(
    session,
    workspace,
    *,
    title,
    path=None,
    body="Body text.",
    tags=("a", "b"),
    status=PageStatus.published,
):
    return await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=path or f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=PageType.concept,
        title=title,
        description=f"About {title}.",
        date=date(2026, 8, 19),
        tags=list(tags),
        body=body,
        author="system:curator",
        status=status,
    )


# --- GET /pages (list) --------------------------------------------------------------------


async def test_list_pages_returns_published_pages_by_default(client, session, workspace):
    await _page(session, workspace, title="Runbook One")
    await _page(session, workspace, title="Draft Page", status=PageStatus.draft)
    await session.commit()

    r = await client.get("/pages", headers=READER, params={"workspace_id": workspace.workspace_id})
    assert r.status_code == 200
    paths = {i["path"] for i in r.json()["items"]}
    assert paths == {"concepts/runbook-one.md"}


async def test_list_pages_filters_by_page_type(client, session, workspace):
    await _page(session, workspace, title="A Concept")
    await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path="entities/an-entity.md",
        page_type=PageType.entity,
        title="An Entity",
        description="About it.",
        date=date(2026, 8, 19),
        tags=["a", "b"],
        body="Body.",
        author="system:curator",
        status=PageStatus.published,
    )
    await session.commit()

    r = await client.get(
        "/pages",
        headers=READER,
        params={"workspace_id": workspace.workspace_id, "page_type": "entity"},
    )
    assert r.status_code == 200
    assert [i["page_type"] for i in r.json()["items"]] == ["entity"]


async def test_list_pages_filters_by_tags(client, session, workspace):
    await _page(session, workspace, title="Tagged Page", tags=("ops", "shared"))
    await _page(session, workspace, title="Other Page", tags=("hr", "policy"))
    await session.commit()

    r = await client.get(
        "/pages", headers=READER, params={"workspace_id": workspace.workspace_id, "tags": "ops"}
    )
    assert r.status_code == 200
    assert [i["title"] for i in r.json()["items"]] == ["Tagged Page"]


async def test_list_pages_filters_by_date_range(client, session, workspace):
    old_page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path="concepts/old.md",
        page_type=PageType.concept,
        title="Old",
        description="d",
        date=date(2020, 1, 1),
        tags=["a", "b"],
        body="Body.",
        author="system:curator",
        status=PageStatus.published,
    )
    await _page(session, workspace, title="Recent")
    await session.commit()

    r = await client.get(
        "/pages",
        headers=READER,
        params={"workspace_id": workspace.workspace_id, "date_from": "2026-01-01"},
    )
    assert r.status_code == 200
    titles = {i["title"] for i in r.json()["items"]}
    assert titles == {"Recent"}
    assert str(old_page.page_id) not in {i["page_id"] for i in r.json()["items"]}


async def test_list_pages_draft_status_requires_contributor(client, session, workspace):
    await _page(session, workspace, title="Draft Page", status=PageStatus.draft)
    await session.commit()

    reader_resp = await client.get(
        "/pages",
        headers=READER,
        params={"workspace_id": workspace.workspace_id, "status": "draft"},
    )
    assert reader_resp.status_code == 403

    contributor_resp = await client.get(
        "/pages",
        headers=CONTRIBUTOR,
        params={"workspace_id": workspace.workspace_id, "status": "draft"},
    )
    assert contributor_resp.status_code == 200
    assert len(contributor_resp.json()["items"]) == 1


async def test_list_pages_excludes_other_workspaces(client, session, workspace, other_workspace):
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

    r = await client.get("/pages", headers=READER, params={"workspace_id": workspace.workspace_id})
    assert r.status_code == 200
    assert [i["title"] for i in r.json()["items"]] == ["Mine"]


async def test_list_pages_paginates_with_cursor(client, session, workspace):
    for i in range(3):
        await _page(session, workspace, title=f"Page {i}")
    await session.commit()

    first = await client.get(
        "/pages", headers=READER, params={"workspace_id": workspace.workspace_id, "limit": 2}
    )
    assert first.status_code == 200
    first_items = first.json()["items"]
    assert len(first_items) == 2
    next_cursor = first.json()["next_cursor"]
    assert next_cursor is not None

    second = await client.get(
        "/pages",
        headers=READER,
        params={"workspace_id": workspace.workspace_id, "limit": 2, "cursor": next_cursor},
    )
    assert second.status_code == 200
    second_items = second.json()["items"]
    assert len(second_items) == 1
    assert {i["page_id"] for i in first_items}.isdisjoint({i["page_id"] for i in second_items})


# --- GET /pages/{page_id} (get) ------------------------------------------------------------


async def test_get_page_returns_full_content(client, session, workspace):
    page = await _page(session, workspace, title="Full Page", body="The full body.")
    await session.commit()

    r = await client.get(f"/pages/{page.page_id}", headers=READER)
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "concepts/full-page.md"
    assert body["title"] == "Full Page"
    assert "The full body." in body["content"]


async def test_get_page_not_found(client, session, workspace):
    r = await client.get(f"/pages/{uuid.uuid4()}", headers=READER)
    assert r.status_code == 404


async def test_get_page_draft_requires_contributor(client, session, workspace):
    page = await _page(session, workspace, title="Draft Get", status=PageStatus.draft)
    await session.commit()

    reader_resp = await client.get(f"/pages/{page.page_id}", headers=READER)
    assert reader_resp.status_code == 403

    contributor_resp = await client.get(f"/pages/{page.page_id}", headers=CONTRIBUTOR)
    assert contributor_resp.status_code == 200


async def test_get_page_forbidden_with_no_access(client, session, other_workspace):
    page = await _page(session, other_workspace, title="Not Mine")
    await session.commit()

    r = await client.get(f"/pages/{page.page_id}", headers=READER)
    assert r.status_code == 403


# --- GET /sources (admin Raw Source Browser) -----------------------------------------------


async def _source(session, workspace, *, filename, supersedes=None, status=RawSourceStatus.active):
    source_id = uuid.uuid4()
    key = f"/{workspace.workspace_id}/sources/{source_id}/{filename}"
    payload = filename.encode()
    objectstore.write_bytes(key, payload)
    source = RawSource(
        source_id=source_id,
        workspace_id=workspace.workspace_id,
        object_key=key,
        filename=filename,
        content_hash=hashlib.sha256(payload).hexdigest(),
        submitted_by="user:deepak",
        status=status,
        supersedes=supersedes,
    )
    session.add(source)
    await session.flush()
    return source


async def test_list_sources_requires_admin(client, session, workspace):
    await _source(session, workspace, filename="a.md")
    await session.commit()

    r = await client.get("/sources", headers=READER, params={"workspace_id": workspace.workspace_id})
    assert r.status_code == 403


async def test_list_sources_shows_supersedes_chain(client, session, workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    original = await _source(session, workspace, filename="v1.md", status=RawSourceStatus.superseded)
    newer = await _source(session, workspace, filename="v2.md", supersedes=original.source_id)
    await session.commit()

    r = await client.get("/sources", headers=ADMIN, params={"workspace_id": workspace.workspace_id})
    assert r.status_code == 200
    by_id = {i["source_id"]: i for i in r.json()["items"]}
    assert by_id[str(newer.source_id)]["supersedes"] == str(original.source_id)
    assert by_id[str(original.source_id)]["supersedes"] is None
    assert by_id[str(original.source_id)]["status"] == "superseded"


async def test_list_sources_filters_by_status(client, session, workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await _source(session, workspace, filename="active.md", status=RawSourceStatus.active)
    await _source(session, workspace, filename="superseded.md", status=RawSourceStatus.superseded)
    await session.commit()

    r = await client.get(
        "/sources",
        headers=ADMIN,
        params={"workspace_id": workspace.workspace_id, "status": "superseded"},
    )
    assert r.status_code == 200
    assert [i["filename"] for i in r.json()["items"]] == ["superseded.md"]


# --- GET/POST /connectors (stub, 06 §1, real implementation is track 2e) ------------------


async def test_list_connectors_requires_admin(client, session, workspace):
    r = await client.get("/connectors", headers=READER, params={"workspace_id": workspace.workspace_id})
    assert r.status_code == 403


async def test_list_connectors_returns_empty_for_admin(client, session, workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await session.commit()

    r = await client.get("/connectors", headers=ADMIN, params={"workspace_id": workspace.workspace_id})
    assert r.status_code == 200
    assert r.json() == {"items": []}


async def test_configure_connector_rejects_non_admin(client, session, workspace):
    r = await client.post("/connectors", headers=READER)
    assert r.status_code == 403


async def test_configure_connector_not_implemented_for_admin(client, session, workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await session.commit()

    r = await client.post("/connectors", headers=ADMIN)
    assert r.status_code == 501
    assert r.json()["error"]["type"] == "not_implemented"
