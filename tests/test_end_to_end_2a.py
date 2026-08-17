"""Phase 2 step 29 — 2a verify: closes out the Multi-Workspace & Taxonomy Routing track.

Ties together, through the real gateway, what steps 22-28 built individually: two documents
of different document_types route to different workspaces with no workspace named in the
submission (step 24); one search query returns ranked results merged from both (step 25);
a taxonomy bulk-move relocates a batch of pages with per-batch progress (step 27).
"""

import uuid

from sqlalchemy import select

from karpwiki import bulk_move, ingestion, search
from karpwiki.classify import ClassificationResult
from karpwiki.curate import CuratedContent, CuratedPage
from karpwiki.models import AccessPolicy, PageType, RawSource, Role, WikiPage, Workspace

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
ADMIN = {"X-Karpwiki-User": "avery"}


def _classifies_as(label, confidence=0.9, summary="A doc."):
    async def _call(**_kwargs):
        return ClassificationResult(summary=summary, document_type=label, confidence=confidence)

    return _call


def _curates_as(*, title, body, tags=("ops", "shared"), extra_page=False):
    pages = [CuratedPage(page_type="concept", title=title, tags=list(tags), body=body)]
    if extra_page:
        pages.append(
            CuratedPage(
                page_type="concept", title=f"{title} Details", tags=list(tags), body=f"{body} (details)"
            )
        )
    content = CuratedContent(
        source_title=title,
        source_description=f"About {title}.",
        source_summary=body,
        source_key_points=[body],
        pages=pages,
    )

    async def _call(**_kwargs):
        return content

    return _call


async def _ingest(session, client, *, text, doc_type, title, body, extra_page=False):
    """Submit through the real gateway with no workspace named — the Classifier alone
    decides where it lands (03 §3, step 24)."""
    submitted = await client.post("/sources", headers=CONTRIBUTOR, data={"text": text})
    source_id = uuid.UUID(submitted.json()["source_id"])
    source = await session.get(RawSource, source_id)

    await ingestion.classify_source(session, source=source, call=_classifies_as(doc_type))
    await ingestion.check_duplicates(session, source=source, summary=body)
    workspace = await session.get(Workspace, source.workspace_id)
    await ingestion.curate_source(
        session,
        source=source,
        workspace=workspace,
        call=_curates_as(title=title, body=body, extra_page=extra_page),
    )
    await session.commit()
    return source


async def test_2a_end_to_end(client, session, workspace, other_workspace, monkeypatch):
    session.add_all(
        [
            AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin),
            AccessPolicy(
                workspace_id=other_workspace.workspace_id, principal="avery", role=Role.admin
            ),
            AccessPolicy(
                workspace_id=other_workspace.workspace_id,
                principal="deepak",
                role=Role.contributor,
            ),
        ]
    )
    await session.flush()

    # --- routing: two documents, no workspace named, different document_types ---
    eng_source = await _ingest(
        session,
        client,
        text="# Restarting the payments worker\n\nHow to restart the shared queue worker.",
        doc_type="eng.runbook",
        title="Restarting The Worker",
        body="Restart procedure for the shared queue worker.",
        extra_page=True,
    )
    policy_source = await _ingest(
        session,
        client,
        text="# Time off policy\n\nHow employees request time off from the shared HR system.",
        doc_type="policy.hr",
        title="Time Off Policy",
        body="Time off requests go through the shared HR system.",
    )
    assert eng_source.workspace_id == workspace.workspace_id
    assert policy_source.workspace_id == other_workspace.workspace_id

    # --- search: one query, merged ranked results from both workspaces ---
    await search.reindex_pending(session)
    await session.commit()

    r = await client.get("/search", headers=CONTRIBUTOR, params={"q": "shared"})
    assert r.status_code == 200
    workspace_ids = {i["workspace_id"] for i in r.json()["items"]}
    assert workspace_ids == {workspace.workspace_id, other_workspace.workspace_id}

    # --- bulk-move: relocate a batch of pages, with per-batch progress ---
    eng_pages = (
        await session.execute(
            select(WikiPage).where(
                WikiPage.workspace_id == workspace.workspace_id, WikiPage.page_type == PageType.concept
            )
        )
    ).scalars().all()
    page_ids = [str(p.page_id) for p in eng_pages]
    assert len(page_ids) == 2  # both curated pages from the eng.runbook source

    # Force two batches of one, so batch_count > 1 actually demonstrates per-batch
    # progress rather than trivially succeeding in a single batch.
    monkeypatch.setattr(bulk_move, "BATCH_SIZE", 1)

    preview = await client.post(
        f"/workspaces/{workspace.workspace_id}/bulk-move/preview",
        headers=ADMIN,
        json={"target_workspace_id": other_workspace.workspace_id, "page_ids": page_ids},
    )
    assert preview.status_code == 200
    assert {p["id"] for p in preview.json()["pages"]} == set(page_ids)

    execute = await client.post(
        f"/workspaces/{workspace.workspace_id}/bulk-move",
        headers=ADMIN,
        json={"target_workspace_id": other_workspace.workspace_id, "page_ids": page_ids},
    )
    assert execute.status_code == 200
    result = execute.json()
    assert result["completed"] is True
    assert set(result["moved_page_ids"]) == set(page_ids)
    assert result["batch_count"] == 2  # per-batch progress: one batch per page (BATCH_SIZE=1)

    for page_id in eng_pages:
        moved = await session.get(WikiPage, page_id.page_id)
        assert moved.workspace_id == other_workspace.workspace_id
