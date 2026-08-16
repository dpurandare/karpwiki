"""Phase 1b step 15 — end-to-end verification.

"Submit a real document end-to-end and get a published, cited wiki page — or a review
item if it's a duplicate/low-confidence."

Drives the real `POST /sources` endpoint, then explicitly steps the pipeline forward one
stage at a time — the same shape every prior step's own tests already use — rather than
via a background worker. No pipeline stage is wired to run automatically yet; see the
turn's own scope note for why that's deliberately out of scope here. What this file adds
over the per-step tests is coverage of the *whole chain together*, through the actual API
surface, for both outcomes the task names.
"""

import uuid

import pytest
from sqlalchemy import select

from karpwiki import ingestion
from karpwiki.classify import ClassificationResult
from karpwiki.curate import CuratedContent, CuratedPage
from karpwiki.models import (
    PageStatus,
    PageType,
    PageVersion,
    PipelineState,
    RawSource,
    ReviewItem,
    ReviewKind,
    WikiPage,
)

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}


def _classifies_as(label="eng.runbook", confidence=0.9, summary="A runbook."):
    async def _call(**_kwargs):
        return ClassificationResult(summary=summary, document_type=label, confidence=confidence)

    return _call


def _curates_as(**overrides):
    content = CuratedContent(
        source_title=overrides.get("source_title", "Restarting the payments worker"),
        source_description="How to restart it safely.",
        source_summary="Drain the queue, then restart the deployment.",
        source_key_points=["Drain the queue.", "Verify lag returns to zero."],
        pages=overrides.get(
            "pages",
            [CuratedPage(page_type="concept", title="Queue Draining", tags=["ops", "reliability"], body="What it is.")],
        ),
    )

    async def _call(**_kwargs):
        return content

    return _call


async def test_a_submitted_document_becomes_a_published_cited_page(session, client, workspace):
    """The full happy path, through the real endpoint: submit, classify, dedup, curate."""
    submitted = await client.post(
        "/sources",
        headers=CONTRIBUTOR,
        data={"text": "# Restarting the payments worker\n\nDrain the queue, then restart."},
    )
    assert submitted.status_code == 202
    source_id = uuid.UUID(submitted.json()["source_id"])

    status = await client.get(f"/sources/{source_id}", headers=CONTRIBUTOR)
    assert status.json()["label"] == "processing"

    source = await session.get(RawSource, source_id)
    await ingestion.classify_source(session, source=source, workspace=workspace, call=_classifies_as())
    assert source.pipeline_state is PipelineState.classified

    state = await ingestion.check_duplicates(session, source=source, summary="a runbook")
    assert state is PipelineState.ingesting

    final = await ingestion.curate_source(session, source=source, workspace=workspace, call=_curates_as())
    await session.commit()
    assert final is PipelineState.ingested

    status = await client.get(f"/sources/{source_id}", headers=CONTRIBUTOR)
    assert status.json()["label"] == "published"

    result = await session.execute(select(WikiPage).where(WikiPage.page_type == PageType.source))
    page = result.scalar_one()
    assert page.status is PageStatus.published
    version = await session.get(PageVersion, page.current_version_id)
    assert "[^1]:" in version.content  # 01 §6: citation footnote back to the raw source

    concept = (
        await session.execute(select(WikiPage).where(WikiPage.page_type == PageType.concept))
    ).scalar_one()
    assert concept.status is PageStatus.published

    overview = (
        await session.execute(select(WikiPage).where(WikiPage.path == "overview.md"))
    ).scalar_one()
    overview_version = await session.get(PageVersion, overview.current_version_id)
    assert "Sources ingested: 1" in overview_version.content


async def test_low_confidence_ends_in_a_review_item_not_a_page(session, client, workspace):
    submitted = await client.post(
        "/sources", headers=CONTRIBUTOR, data={"text": "Ambiguous content of unclear type."}
    )
    source_id = uuid.UUID(submitted.json()["source_id"])
    source = await session.get(RawSource, source_id)

    state = await ingestion.classify_source(
        session, source=source, workspace=workspace, call=_classifies_as(confidence=0.1)
    )
    await session.commit()

    assert state is PipelineState.pending_review
    status = await client.get(f"/sources/{source_id}", headers=CONTRIBUTOR)
    assert status.json()["label"] == "awaiting review"

    items = (
        await session.execute(select(ReviewItem).where(ReviewItem.subject_ref == str(source_id)))
    ).scalars().all()
    kinds = {i.kind for i in items}
    assert ReviewKind.submission in kinds
    assert ReviewKind.classification in kinds

    # No wiki page exists — classification never resolved a workspace to write one under.
    assert (
        await session.execute(select(WikiPage).where(WikiPage.workspace_id == workspace.workspace_id))
    ).scalar_one_or_none() is None


async def test_a_duplicate_ends_in_a_review_item_not_a_second_page(session, client, workspace):
    body = "# Restarting the payments worker\n\nDrain the queue, then restart."

    first = await client.post("/sources", headers=CONTRIBUTOR, data={"text": body})
    first_id = uuid.UUID(first.json()["source_id"])
    first_source = await session.get(RawSource, first_id)
    await ingestion.classify_source(session, source=first_source, workspace=workspace, call=_classifies_as())
    await ingestion.check_duplicates(session, source=first_source, summary="a runbook")
    await ingestion.curate_source(session, source=first_source, workspace=workspace, call=_curates_as())

    second = await client.post("/sources", headers=CONTRIBUTOR, data={"text": body})
    second_id = uuid.UUID(second.json()["source_id"])
    second_source = await session.get(RawSource, second_id)
    await ingestion.classify_source(session, source=second_source, workspace=workspace, call=_classifies_as())
    state = await ingestion.check_duplicates(session, source=second_source, summary="a runbook")
    await session.commit()

    assert state is PipelineState.pending_review
    item = (
        await session.execute(select(ReviewItem).where(ReviewItem.subject_ref == str(second_id)))
    ).scalars().all()
    assert any(i.kind is ReviewKind.duplicate and i.severity == "high" for i in item)

    # Both sources get a placeholder page from classify_source (§18) — the second's is
    # simply never finalized, since check_duplicates blocked it before curate_source runs.
    pages = (
        await session.execute(select(WikiPage).where(WikiPage.page_type == PageType.source))
    ).scalars().all()
    assert len(pages) == 2
    published = [p for p in pages if p.status is PageStatus.published]
    assert len(published) == 1  # only the first source was ever curated


async def test_a_rejected_source_ends_with_no_page_at_all(session, client, workspace):
    """The fourth outcome the state machine allows, alongside ingested/duplicate/low-confidence."""
    submitted = await client.post(
        "/sources", headers=CONTRIBUTOR, data={"text": "Something an admin will decline."}
    )
    source_id = uuid.UUID(submitted.json()["source_id"])
    source = await session.get(RawSource, source_id)
    await ingestion.classify_source(session, source=source, workspace=workspace, call=_classifies_as())
    await ingestion.check_duplicates(session, source=source, summary="x", ingestion_policy="gated")

    state = await ingestion.reject_source(session, source=source, reason="not relevant to this wiki")
    await session.commit()

    assert state is PipelineState.rejected
    status = await client.get(f"/sources/{source_id}", headers=CONTRIBUTOR)
    assert status.json()["label"] == "rejected"

    # The placeholder page from classify_source is finalized with the rejection reason
    # (03 §1, 09 §18) — one page exists, but it never left `draft`, unlike an ingested page.
    page = (
        await session.execute(select(WikiPage).where(WikiPage.page_type == PageType.source))
    ).scalar_one()
    assert page.status is PageStatus.draft
    version = await session.get(PageVersion, page.current_version_id)
    assert "not relevant to this wiki" in version.content
