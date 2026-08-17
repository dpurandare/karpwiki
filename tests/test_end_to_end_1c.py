"""Phase 1c step 21 — end-to-end verification.

Closes the loop step 15 (Phase 1b) left open: that file proves a submission becomes a
published, cited page. This file proves what Phase 1c added on top, through the real
gateway where an endpoint exists and through the underlying functions where none was
built (search has no HTTP endpoint — 06 §1's "full API+MCP surface" is an explicit
Phase 1 exclusion, and no step named building one; indexing is still an explicit call per
step 18's decision, 09 §21): search returns ranked, cited results; an admin can see the
queue and resolve a submission/classification/duplicate item; an admin can roll back a
page version.
"""

import uuid

from sqlalchemy import select

from karpwiki import ingestion, search, versioning
from karpwiki.classify import ClassificationResult
from karpwiki.curate import CuratedContent, CuratedPage
from karpwiki.models import (
    AccessPolicy,
    PageStatus,
    PageType,
    PageVersion,
    PipelineState,
    RawSource,
    ReviewKind,
    Role,
    VersionTrigger,
    WikiPage,
)

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
ADMIN = {"X-Karpwiki-User": "avery"}


async def _grant_admin(session, workspace, principal="avery"):
    session.add(
        AccessPolicy(workspace_id=workspace.workspace_id, principal=principal, role=Role.admin)
    )
    await session.flush()


def _classifies_as(label="eng.runbook", confidence=0.9, summary="A runbook."):
    async def _call(**_kwargs):
        return ClassificationResult(summary=summary, document_type=label, confidence=confidence)

    return _call


def _curates_as(**overrides):
    content = CuratedContent(
        source_title=overrides.get("source_title", "Restarting the Payments Worker"),
        source_description=overrides.get(
            "source_description", "Runbook for safely restarting the payments worker."
        ),
        source_summary="Drain the queue, then restart the deployment.",
        source_key_points=["Drain the queue.", "Verify lag returns to zero."],
        pages=overrides.get(
            "pages",
            [
                CuratedPage(
                    page_type="concept",
                    title="Queue Draining",
                    tags=["ops", "reliability"],
                    body="Draining a queue before restart avoids dropped messages.",
                )
            ],
        ),
    )

    async def _call(**_kwargs):
        return content

    return _call


async def _ingest(session, client, workspace, *, text, **curator_overrides):
    """Submit through the real endpoint, then drive classify -> dedup -> curate the same
    way every prior step's tests do (no stage is wired to run automatically, 09 §21)."""
    submitted = await client.post("/sources", headers=CONTRIBUTOR, data={"text": text})
    source_id = uuid.UUID(submitted.json()["source_id"])
    source = await session.get(RawSource, source_id)

    await ingestion.classify_source(session, source=source, call=_classifies_as())
    await ingestion.check_duplicates(session, source=source, summary="a runbook")
    await ingestion.curate_source(
        session, source=source, workspace=workspace, call=_curates_as(**curator_overrides)
    )
    await session.commit()
    return source


async def test_a_published_page_is_ranked_and_cited_by_search(session, client, workspace):
    await _ingest(
        session,
        client,
        workspace,
        text="# Restarting the payments worker\n\nDrain the queue, then restart.",
    )

    # Indexing is an explicit sweep, not automatic (step 18, 09 §21).
    indexed = await search.reindex_pending(session)
    assert indexed  # at least the source page landed pending and got indexed
    await session.commit()

    source_page = (
        await session.execute(select(WikiPage).where(WikiPage.page_type == PageType.source))
    ).scalar_one()
    version = await session.get(PageVersion, source_page.current_version_id)
    assert "[^1]:" in version.content  # 01 §6 citation footnote, carried through to search

    # Body match.
    hits = await search.search(session, query="restarting", workspace_ids=[workspace.workspace_id])
    assert any(h.page_id == source_page.page_id for h in hits)

    # Catalog-match boost (04 §3, 09 §20): a term only in `description` still finds the page.
    hits = await search.search(session, query="runbook", workspace_ids=[workspace.workspace_id])
    assert [h.page_id for h in hits][0] == source_page.page_id


async def test_admin_sees_and_acknowledges_the_submission_item(session, client, workspace):
    await _grant_admin(session, workspace)
    submitted = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "New doc."})
    source_id = submitted.json()["source_id"]

    listed = await client.get("/review-items", headers=ADMIN)
    assert listed.status_code == 200
    item = next(i for i in listed.json()["items"] if i["subject_ref"] == source_id)
    assert item["kind"] == ReviewKind.submission.value

    resolved = await client.post(
        f"/review-items/{item['review_id']}/resolve", headers=ADMIN, json={"action": "acknowledge"}
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"


async def test_admin_resolves_a_low_confidence_classification_item(session, client, workspace):
    await _grant_admin(session, workspace)
    submitted = await client.post(
        "/sources", headers=CONTRIBUTOR, data={"text": "Ambiguous content of unclear type."}
    )
    source_id = uuid.UUID(submitted.json()["source_id"])
    source = await session.get(RawSource, source_id)
    state = await ingestion.classify_source(
        session, source=source, call=_classifies_as(confidence=0.1)
    )
    await session.commit()
    assert state is PipelineState.pending_review

    listed = await client.get("/review-items", headers=ADMIN, params={"kind": "classification"})
    item = next(i for i in listed.json()["items"] if i["subject_ref"] == str(source_id))

    resolved = await client.post(
        f"/review-items/{item['review_id']}/resolve",
        headers=ADMIN,
        json={"action": "eng.runbook"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["pipeline_state"] == PipelineState.classified.value

    await session.refresh(source)
    assert source.workspace_id == workspace.workspace_id


async def test_admin_resolves_a_duplicate_item(session, client, workspace):
    await _grant_admin(session, workspace)
    body = "# Restarting the payments worker\n\nDrain the queue, then restart."
    await _ingest(session, client, workspace, text=body)

    second = await client.post("/sources", headers=CONTRIBUTOR, data={"text": body})
    second_id = uuid.UUID(second.json()["source_id"])
    second_source = await session.get(RawSource, second_id)
    await ingestion.classify_source(session, source=second_source, call=_classifies_as())
    state = await ingestion.check_duplicates(session, source=second_source, summary="a runbook")
    await session.commit()
    assert state is PipelineState.pending_review

    listed = await client.get("/review-items", headers=ADMIN, params={"kind": "duplicate"})
    item = next(i for i in listed.json()["items"] if i["subject_ref"] == str(second_id))

    resolved = await client.post(
        f"/review-items/{item['review_id']}/resolve", headers=ADMIN, json={"action": "reject"}
    )
    assert resolved.status_code == 200
    assert resolved.json()["pipeline_state"] == PipelineState.rejected.value


async def test_admin_rolls_back_a_page_version(session, client, workspace):
    await _grant_admin(session, workspace)
    await _ingest(
        session,
        client,
        workspace,
        text="# Restarting the payments worker\n\nDrain the queue, then restart.",
    )

    concept_page = (
        await session.execute(select(WikiPage).where(WikiPage.page_type == PageType.concept))
    ).scalar_one()
    original_version_id = concept_page.current_version_id

    await versioning.write_version(
        session,
        page=concept_page,
        body="Replaced content that shouldn't have shipped.",
        author="user:deepak",
        trigger=VersionTrigger.manual_edit,
    )
    await session.commit()

    history = await client.get(f"/pages/{concept_page.page_id}/versions", headers=ADMIN)
    assert len(history.json()["items"]) == 2

    rolled_back = await client.post(
        f"/pages/{concept_page.page_id}/rollback",
        headers=ADMIN,
        json={"target_version_id": str(original_version_id)},
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["restored_from_version_id"] == str(original_version_id)

    await session.refresh(concept_page)
    current = await session.get(PageVersion, concept_page.current_version_id)
    assert "shouldn't have shipped" not in current.content
    assert "Draining a queue" in current.content

    history = await client.get(f"/pages/{concept_page.page_id}/versions", headers=ADMIN)
    assert len(history.json()["items"]) == 3
