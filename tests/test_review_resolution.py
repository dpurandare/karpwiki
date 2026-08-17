"""Admin resolution of review items (03 §3-5, 05 §1) — phase1-tasklist step 19."""

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from karpwiki import ingestion, objectstore, pipeline, review, search, versioning
from karpwiki.curate import MergedPage
from karpwiki.models import (
    ContentShape,
    PageStatus,
    PageType,
    PageVersion,
    PipelineState,
    RawSource,
    RawSourceStatus,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
)

BODY = (
    "The payments worker drains its queue before restart. Operators run a rollout restart "
    "and verify that consumer lag returns to zero within five minutes."
)


async def _submitted_source(session, *, filename="f.md", payload=b"content"):
    source_id = uuid.uuid4()
    key = f"/_inbox/{source_id}/{filename}"
    objectstore.write_bytes(key, payload)
    source = RawSource(
        source_id=source_id,
        object_key=key,
        filename=filename,
        content_hash="deadbeef",
        submitted_by="user:deepak",
        pipeline_state=PipelineState.submitted,
    )
    session.add(source)
    await session.flush()
    return source


async def _classification_pending_review(session):
    """A source parked `pending_review` by low classification confidence (03 §3)."""
    source = await _submitted_source(session)
    await pipeline.transition(
        session, source=source, to_state=PipelineState.classifying, actor="system:classifier"
    )
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.pending_review,
        actor="system:classifier",
        detail={"reason": "confidence below threshold", "candidates": []},
    )
    item = await review.create(
        session, kind=ReviewKind.classification, subject_ref=str(source.source_id)
    )
    return source, item


# --- classification --------------------------------------------------------------------


async def test_resolve_classification_assigns_workspace_and_classifies(session, workspace):
    source, item = await _classification_pending_review(session)

    state = await ingestion.resolve_classification(
        session, item=item, workspace=workspace, document_type="eng.runbook", actor="user:admin"
    )
    await session.commit()

    assert state is PipelineState.classified
    assert source.workspace_id == workspace.workspace_id
    assert source.pipeline_state is PipelineState.classified
    assert item.status is ReviewStatus.resolved
    assert item.resolved_action == "eng.runbook"


async def test_resolve_classification_rejects_a_type_outside_the_taxonomy(session, workspace):
    source, item = await _classification_pending_review(session)
    with pytest.raises(ingestion.InvalidResolutionError):
        await ingestion.resolve_classification(
            session, item=item, workspace=workspace, document_type="not.a.real.type", actor="a"
        )


async def test_resolve_classification_rejects_the_wrong_kind(session, workspace):
    item = await review.create(session, kind=ReviewKind.submission, subject_ref="src-1")
    with pytest.raises(ingestion.InvalidResolutionError):
        await ingestion.resolve_classification(
            session, item=item, workspace=workspace, document_type="eng.runbook", actor="a"
        )


# --- submission --------------------------------------------------------------------------


async def test_resolve_submission_acknowledges(session, workspace):
    item = await review.create(session, kind=ReviewKind.submission, subject_ref="src-1")
    resolved = await ingestion.resolve_submission(session, item=item, actor="user:admin")
    assert resolved.status is ReviewStatus.resolved
    assert resolved.resolved_action == "acknowledge"


async def test_resolve_submission_rejects_the_wrong_kind(session, workspace):
    item = await review.create(
        session, kind=ReviewKind.duplicate, subject_ref="src-1", workspace_id=workspace.workspace_id
    )
    with pytest.raises(ingestion.InvalidResolutionError):
        await ingestion.resolve_submission(session, item=item, actor="user:admin")


# --- duplicate: near-duplicate evidence (reject / keep_both / merge) -------------------


async def _near_duplicate_pending_review(session, workspace, *, source_body=None):
    target_page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path="concepts/restarting-payments.md",
        page_type=PageType.concept,
        title="Restarting Payments",
        description="Restart runbook.",
        date=date(2026, 8, 14),
        tags=["ops", "payments"],
        body=BODY,
        author="system:curator",
        status=PageStatus.published,
    )
    version = await session.get(PageVersion, target_page.current_version_id)
    await search.index_page(session, page=target_page, version=version)

    source_id = uuid.uuid4()
    payload = (source_body or BODY).encode()
    key = f"/{workspace.workspace_id}/sources/{source_id}/dup.md"
    objectstore.write_bytes(key, payload)
    source = RawSource(
        source_id=source_id,
        workspace_id=workspace.workspace_id,
        object_key=key,
        filename="dup.md",
        content_hash="deadbeef2",
        submitted_by="user:deepak",
        pipeline_state=PipelineState.classified,
    )
    session.add(source)
    await session.flush()

    state = await ingestion.check_duplicates(session, source=source, summary=BODY)
    assert state is PipelineState.pending_review
    result = await session.execute(
        select(ReviewItem).where(ReviewItem.subject_ref == str(source.source_id))
    )
    return source, result.scalar_one(), target_page


async def test_resolve_duplicate_reject(session, workspace):
    source, item, _ = await _near_duplicate_pending_review(session, workspace)
    state = await ingestion.resolve_duplicate(
        session, item=item, source=source, action="reject", actor="user:admin"
    )
    await session.commit()

    assert state is PipelineState.rejected
    assert source.status is RawSourceStatus.rejected
    assert item.status is ReviewStatus.resolved
    assert item.resolved_action == "reject"


async def test_resolve_duplicate_keep_both_proceeds_to_ingesting(session, workspace):
    source, item, _ = await _near_duplicate_pending_review(session, workspace)
    state = await ingestion.resolve_duplicate(
        session, item=item, source=source, action="keep_both", actor="user:admin"
    )
    assert state is PipelineState.ingesting
    assert item.status is ReviewStatus.resolved


async def test_resolve_duplicate_rejects_an_unknown_action(session, workspace):
    source, item, _ = await _near_duplicate_pending_review(session, workspace)
    with pytest.raises(ingestion.InvalidResolutionError):
        await ingestion.resolve_duplicate(
            session, item=item, source=source, action="bogus", actor="user:admin"
        )


async def test_resolve_duplicate_supersede_without_evidence_fails(session, workspace):
    """A near-duplicate verdict has no prior *source* to supersede, only a matched page."""
    source, item, _ = await _near_duplicate_pending_review(session, workspace)
    with pytest.raises(ingestion.InvalidResolutionError):
        await ingestion.resolve_duplicate(
            session, item=item, source=source, action="supersede", actor="user:admin"
        )


async def test_resolve_duplicate_merge_updates_the_matched_page(session, workspace):
    source, item, target_page = await _near_duplicate_pending_review(
        session, workspace, source_body="Extra detail about restarting payments safely."
    )

    async def _merge_call(**_kwargs):
        return MergedPage(body="Merged body.", change_summary="Merged duplicate submission.")

    state = await ingestion.resolve_duplicate(
        session, item=item, source=source, action="merge", actor="user:admin", call=_merge_call
    )
    await session.commit()

    assert state is PipelineState.ingested
    current = await session.get(PageVersion, target_page.current_version_id)
    assert "Merged body." in current.content
    assert current.change_summary == "Merged duplicate submission."
    assert item.status is ReviewStatus.resolved
    assert item.resolved_action == "merge"


async def test_resolve_duplicate_merge_failure_leaves_the_item_open_for_retry(session, workspace):
    source, item, _ = await _near_duplicate_pending_review(session, workspace)

    async def _boom(**_kwargs):
        raise RuntimeError("boom")

    state = await ingestion.resolve_duplicate(
        session, item=item, source=source, action="merge", actor="user:admin", call=_boom
    )

    assert state is PipelineState.error
    assert item.status is ReviewStatus.open


# --- duplicate: newer-version evidence (supersede) --------------------------------------


async def _supersede_pending_review(session, workspace):
    old = RawSource(
        workspace_id=workspace.workspace_id,
        object_key=f"/{workspace.workspace_id}/sources/{uuid.uuid4()}/old.json",
        filename="old.json",
        content_hash="old-hash",
        submitted_by="user:deepak",
        content_shape=ContentShape.structured_data,
        artifact_identity="payments-api",
        source_version="2.9",
        pipeline_state=PipelineState.ingested,
    )
    session.add(old)
    await session.flush()

    new_id = uuid.uuid4()
    key = f"/{workspace.workspace_id}/sources/{new_id}/new.json"
    objectstore.write_bytes(key, b'{"name": "payments-api", "version": "2.10"}')
    new = RawSource(
        source_id=new_id,
        workspace_id=workspace.workspace_id,
        object_key=key,
        filename="new.json",
        content_hash="new-hash",
        submitted_by="user:deepak",
        content_shape=ContentShape.structured_data,
        artifact_identity="payments-api",
        source_version="2.10",
        pipeline_state=PipelineState.classified,
    )
    session.add(new)
    await session.flush()

    state = await ingestion.check_duplicates(session, source=new, summary="x")
    assert state is PipelineState.pending_review
    result = await session.execute(
        select(ReviewItem).where(ReviewItem.subject_ref == str(new.source_id))
    )
    return old, new, result.scalar_one()


async def test_resolve_duplicate_supersede_marks_the_old_source(session, workspace):
    old, new, item = await _supersede_pending_review(session, workspace)

    state = await ingestion.resolve_duplicate(
        session, item=item, source=new, action="supersede", actor="user:admin"
    )
    await session.commit()

    assert state is PipelineState.ingesting
    assert old.status is RawSourceStatus.superseded
    assert item.resolved_action == "supersede"


async def test_resolve_duplicate_merge_without_a_matched_page_fails(session, workspace):
    """A newer-version verdict has a prior *source*, not a matched *page* — merge needs
    the latter (09 §22)."""
    old, new, item = await _supersede_pending_review(session, workspace)
    with pytest.raises(ingestion.InvalidResolutionError):
        await ingestion.resolve_duplicate(
            session, item=item, source=new, action="merge", actor="user:admin"
        )
