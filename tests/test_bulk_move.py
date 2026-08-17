"""Taxonomy bulk-move admin action (05 §7, 09 §11) — phase2-tasklist.md step 27."""

import hashlib
import uuid
from datetime import date

import pytest
from sqlalchemy import select

from karpwiki import bulk_move, dedicated_index, objectstore, search, versioning
from karpwiki.models import (
    AccessPolicy,
    AdminActionLog,
    PageStatus,
    PageType,
    PageVersion,
    PipelineState,
    RawSource,
    Role,
    VersionTrigger,
    WikiPage,
)

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
ADMIN = {"X-Karpwiki-User": "avery"}


async def _page(session, workspace, *, title="Runbook", body="Body text.", trigger=VersionTrigger.ingest):
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=PageType.concept,
        title=title,
        description=f"About {title}.",
        date=date(2026, 8, 17),
        tags=["a", "b"],
        body=body,
        author="system:curator",
        status=PageStatus.published,
        trigger=trigger,
    )
    return page


async def _source(session, workspace, *, filename="notes.md", payload=b"# Notes"):
    source_id = uuid.uuid4()
    key = f"/{workspace.workspace_id}/sources/{source_id}/{filename}"
    objectstore.write_bytes(key, payload)
    source = RawSource(
        source_id=source_id,
        workspace_id=workspace.workspace_id,
        object_key=key,
        filename=filename,
        content_hash=hashlib.sha256(payload).hexdigest(),
        submitted_by="user:deepak",
        pipeline_state=PipelineState.ingested,
    )
    session.add(source)
    await session.flush()
    return source


async def _admin_on_both(session, workspace, other_workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    session.add(
        AccessPolicy(workspace_id=other_workspace.workspace_id, principal="avery", role=Role.admin)
    )
    await session.flush()


# --- module-level tests (bulk_move.py directly) ---------------------------------------


async def test_preview_classifies_pages_and_sources(session, workspace, other_workspace):
    to_move = await _page(session, workspace, title="To Move")
    already_there = await _page(session, other_workspace, title="Already There")
    src = await _source(session, workspace)
    missing_id = uuid.uuid4()

    result = await bulk_move.preview(
        session,
        source_workspace_id=workspace.workspace_id,
        target_workspace_id=other_workspace.workspace_id,
        page_ids=[to_move.page_id, already_there.page_id, missing_id],
        source_ids=[src.source_id],
    )

    assert [i.id for i in result.pages] == [to_move.page_id]
    assert [i.id for i in result.sources] == [src.source_id]
    reasons = {i.id: i.reason for i in result.skipped_pages}
    assert reasons[already_there.page_id] == "already_at_target"
    assert reasons[missing_id] == "not_found"


async def test_execute_batch_moves_page_and_writes_new_version(session, workspace, other_workspace):
    page = await _page(session, workspace, title="Move Me", body="original body")

    result = await bulk_move.execute_batch(
        session,
        source_workspace_id=workspace.workspace_id,
        target_workspace_id=other_workspace.workspace_id,
        page_ids=[page.page_id],
        source_ids=[],
        actor="user:avery",
    )

    assert result.moved_page_ids == [page.page_id]
    await session.refresh(page)
    assert page.workspace_id == other_workspace.workspace_id

    version = await session.get(PageVersion, page.current_version_id)
    assert version.trigger is VersionTrigger.manual_edit
    assert version.frontmatter["workspace_id"] == other_workspace.workspace_id
    assert "original body" in version.content

    log = (await session.execute(select(AdminActionLog))).scalars().one()
    assert log.action == "bulk_move"
    assert str(page.page_id) in log.detail["moved_page_ids"]


async def test_execute_batch_moves_source_and_relocates_object(session, workspace, other_workspace):
    src = await _source(session, workspace, payload=b"important notes")
    old_key = src.object_key

    result = await bulk_move.execute_batch(
        session,
        source_workspace_id=workspace.workspace_id,
        target_workspace_id=other_workspace.workspace_id,
        page_ids=[],
        source_ids=[src.source_id],
        actor="user:avery",
    )

    assert result.moved_source_ids == [src.source_id]
    assert src.workspace_id == other_workspace.workspace_id
    assert src.object_key == f"/{other_workspace.workspace_id}/sources/{src.source_id}/notes.md"
    assert objectstore.read_bytes(src.object_key) == b"important notes"
    with pytest.raises(FileNotFoundError):
        objectstore.read_bytes(old_key)


async def test_execute_batch_is_resumable(session, workspace, other_workspace):
    """Calling execute_batch again with the same ids after a partial/complete move is a
    safe no-op for whatever already moved (09 §11: "resumes or retries")."""
    page = await _page(session, workspace, title="Idempotent Page")
    await bulk_move.execute_batch(
        session,
        source_workspace_id=workspace.workspace_id,
        target_workspace_id=other_workspace.workspace_id,
        page_ids=[page.page_id],
        source_ids=[],
        actor="user:avery",
    )

    second = await bulk_move.execute_batch(
        session,
        source_workspace_id=workspace.workspace_id,
        target_workspace_id=other_workspace.workspace_id,
        page_ids=[page.page_id],
        source_ids=[],
        actor="user:avery",
    )
    assert second.moved_page_ids == []


async def test_execute_batch_cleans_up_opensearch_when_leaving_a_dedicated_workspace(
    session, dedicated_workspace, other_workspace
):
    page = await _page(session, dedicated_workspace, title="Dedicated Leaver", body="unique leaver term")
    version = await session.get(PageVersion, page.current_version_id)
    await search.index_page(session, page=page, version=version)

    hits = await dedicated_index.search(query="unique leaver term", workspace_ids=[dedicated_workspace.workspace_id])
    assert any(h.page_id == page.page_id for h in hits)

    await bulk_move.execute_batch(
        session,
        source_workspace_id=dedicated_workspace.workspace_id,
        target_workspace_id=other_workspace.workspace_id,
        page_ids=[page.page_id],
        source_ids=[],
        actor="user:avery",
    )

    hits = await dedicated_index.search(
        query="unique leaver term", workspace_ids=[dedicated_workspace.workspace_id, other_workspace.workspace_id]
    )
    assert not any(h.page_id == page.page_id for h in hits)


# --- API-level tests (preview/execute endpoints, auth, batching, partial failure) -------


async def test_bulk_move_preview_requires_admin_in_both_workspaces(client, session, workspace, other_workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await session.flush()
    page = await _page(session, workspace, title="Needs Both Admin")

    r = await client.post(
        f"/workspaces/{workspace.workspace_id}/bulk-move/preview",
        headers=ADMIN,
        json={"target_workspace_id": other_workspace.workspace_id, "page_ids": [str(page.page_id)]},
    )
    assert r.status_code == 403


async def test_bulk_move_preview_and_execute_round_trip(client, session, workspace, other_workspace):
    await _admin_on_both(session, workspace, other_workspace)
    page = await _page(session, workspace, title="Round Trip Page")
    src = await _source(session, workspace)

    preview = await client.post(
        f"/workspaces/{workspace.workspace_id}/bulk-move/preview",
        headers=ADMIN,
        json={
            "target_workspace_id": other_workspace.workspace_id,
            "page_ids": [str(page.page_id)],
            "source_ids": [str(src.source_id)],
        },
    )
    assert preview.status_code == 200
    body = preview.json()
    assert [p["id"] for p in body["pages"]] == [str(page.page_id)]
    assert [s["id"] for s in body["sources"]] == [str(src.source_id)]

    execute = await client.post(
        f"/workspaces/{workspace.workspace_id}/bulk-move",
        headers=ADMIN,
        json={
            "target_workspace_id": other_workspace.workspace_id,
            "page_ids": [str(page.page_id)],
            "source_ids": [str(src.source_id)],
        },
    )
    assert execute.status_code == 200
    result = execute.json()
    assert result["completed"] is True
    assert result["moved_page_ids"] == [str(page.page_id)]
    assert result["moved_source_ids"] == [str(src.source_id)]

    moved_page = await session.get(WikiPage, page.page_id)
    assert moved_page.workspace_id == other_workspace.workspace_id


async def test_bulk_move_execute_batches_and_halts_without_rolling_back_prior_batches(
    client, session, workspace, other_workspace, monkeypatch
):
    await _admin_on_both(session, workspace, other_workspace)
    monkeypatch.setattr(bulk_move, "BATCH_SIZE", 1)

    good_page = await _page(session, workspace, title="Good Page")
    bad_page = await _page(session, workspace, title="Bad Page")

    real_execute_batch = bulk_move.execute_batch
    calls = 0

    async def _flaky(session, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated batch failure")
        return await real_execute_batch(session, **kwargs)

    import karpwiki.api as api_module

    monkeypatch.setattr(api_module.bulk_move, "execute_batch", _flaky)

    r = await client.post(
        f"/workspaces/{workspace.workspace_id}/bulk-move",
        headers=ADMIN,
        json={
            "target_workspace_id": other_workspace.workspace_id,
            "page_ids": [str(good_page.page_id), str(bad_page.page_id)],
        },
    )
    assert r.status_code == 200
    result = r.json()
    assert result["completed"] is False
    assert result["error"] == "simulated batch failure"
    assert result["moved_page_ids"] == [str(good_page.page_id)]

    moved = await session.get(WikiPage, good_page.page_id)
    assert moved.workspace_id == other_workspace.workspace_id
    not_moved = await session.get(WikiPage, bad_page.page_id)
    assert not_moved.workspace_id == workspace.workspace_id
