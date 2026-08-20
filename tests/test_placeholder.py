"""Placeholder source page lifecycle (03 §1) — phase1-tasklist step 13."""

import hashlib
import uuid

import pytest
from sqlalchemy import select

from karpwiki import ingestion, objectstore, pipeline
from karpwiki.classify import ClassificationResult
from karpwiki.curate import CuratedContent
from karpwiki.models import (
    PageStatus,
    PageType,
    PageVersion,
    PipelineState,
    RawSource,
    RawSourceStatus,
    WikiPage,
)


def test_every_pipeline_state_has_a_label():
    for state in PipelineState:
        assert pipeline.placeholder_label(state)


def test_labels_match_03_1s_visibility_table():
    assert pipeline.placeholder_label(PipelineState.classifying) == "processing"
    assert pipeline.placeholder_label(PipelineState.pending_review) == "awaiting review"
    assert pipeline.placeholder_label(PipelineState.error) == "error"
    assert pipeline.placeholder_label(PipelineState.rejected) == "rejected"


async def _submitted(session, *, filename="runbook.md", payload=b"# Runbook\n\nDrain then restart."):
    source_id = uuid.uuid4()
    key = f"/_inbox/{source_id}/{filename}"
    objectstore.write_bytes(key, payload)
    source = RawSource(
        source_id=source_id,
        object_key=key,
        filename=filename,
        content_hash=hashlib.sha256(payload).hexdigest(),
        submitted_by="user:deepak",
        pipeline_state=PipelineState.submitted,
    )
    session.add(source)
    await session.flush()
    return source


def _classifies_as(label="eng.runbook", confidence=0.9):
    async def _call(**_kwargs):
        return ClassificationResult(summary="A runbook.", document_type=label, confidence=confidence)

    return _call


async def _source_page(session, workspace, source):
    result = await session.execute(
        select(WikiPage).where(
            WikiPage.workspace_id == workspace.workspace_id,
            WikiPage.path == f"sources/{source.source_id}.md",
        )
    )
    return result.scalar_one_or_none()


async def test_classification_creates_a_draft_placeholder(session, workspace):
    source = await _submitted(session)
    await ingestion.classify_source(session, source=source, call=_classifies_as())
    await session.commit()

    page = await _source_page(session, workspace, source)
    assert page is not None
    assert page.status is PageStatus.draft
    version = await session.get(PageVersion, page.current_version_id)
    assert version.frontmatter["title"].startswith("Processing:")


async def test_no_placeholder_when_confidence_is_too_low(session, workspace):
    """No workspace is resolved on this path, so there is nothing to attach a page to."""
    source = await _submitted(session)
    await ingestion.classify_source(
        session, source=source, call=_classifies_as(confidence=0.1)
    )
    await session.commit()
    assert source.workspace_id is None
    assert (await _source_page(session, workspace, source)) is None


async def test_curation_finalizes_the_same_page_not_a_new_one(session, workspace):
    """The placeholder created at classification and the page the Curator finalizes must
    be the same row — otherwise there are two source pages for one raw_source."""
    source = await _submitted(session)
    await ingestion.classify_source(session, source=source, call=_classifies_as())
    await session.flush()
    placeholder = await _source_page(session, workspace, source)
    placeholder_id = placeholder.page_id

    await ingestion.check_duplicates(session, source=source, summary="a runbook")

    content = CuratedContent(
        source_title="Restarting the worker",
        source_description="How to restart it.",
        source_summary="Drain, then restart.",
        source_key_points=["Drain the queue."],
    )

    async def _curates_as(**_kwargs):
        return content

    await ingestion.curate_source(session, source=source, workspace=workspace, call=_curates_as)
    await session.commit()

    result = await session.execute(
        select(WikiPage).where(WikiPage.workspace_id == workspace.workspace_id, WikiPage.page_type == PageType.source)
    )
    pages = result.scalars().all()
    assert len(pages) == 1
    assert pages[0].page_id == placeholder_id
    assert pages[0].status is PageStatus.published
    version = await session.get(PageVersion, pages[0].current_version_id)
    assert version.frontmatter["title"] == "Restarting the worker"


async def test_retrying_classification_does_not_duplicate_the_placeholder(session, workspace):
    """03 §1 allows `pending_review -> classifying` (retry after error). If classification
    later succeeds on a retry, the placeholder must be updated in place, not duplicated."""
    source = await _submitted(session)
    await ingestion.classify_source(session, source=source, call=_classifies_as())
    await session.flush()
    first = await _source_page(session, workspace, source)

    # Simulate a retry: run the placeholder-creation path again directly.
    await ingestion._create_placeholder_source_page(session, source=source, workspace=workspace)
    await session.commit()

    result = await session.execute(
        select(WikiPage).where(WikiPage.workspace_id == workspace.workspace_id, WikiPage.page_type == PageType.source)
    )
    pages = result.scalars().all()
    assert len(pages) == 1
    assert pages[0].page_id == first.page_id


async def test_reject_is_illegal_from_a_state_nothing_ever_finds_a_source_resting_in(
    session, workspace
):
    """`classifying` only ever exists inside one atomic worker transaction (pipeline.py's
    `ABORTABLE_IF_STUCK` docstring, phase3-tasklist.md step 64) — nothing, review or
    otherwise, ever legitimately finds a source resting there to reject."""
    source = await _submitted(session)
    await pipeline.transition(
        session, source=source, to_state=PipelineState.classifying, actor="user:admin"
    )
    with pytest.raises(pipeline.IllegalTransition):
        await ingestion.reject_source(session, source=source, reason="not relevant")


async def test_reject_is_legal_from_the_stuck_pipeline_abortable_states(session, workspace):
    """Step 64 widened `reject_source`'s legal starting states beyond `pending_review` to
    also cover `submitted`/`classified`/`ingesting` — the three a Stuck-Pipeline Sweep
    abort can land on (`pipeline.ABORTABLE_IF_STUCK`)."""
    source = await _submitted(session)
    state = await ingestion.reject_source(
        session, source=source, reason="stuck pipeline sweep: aborted by admin"
    )
    assert state is PipelineState.rejected
    assert source.pipeline_state is PipelineState.rejected
    assert source.status is RawSourceStatus.rejected


async def test_reject_flips_both_pipeline_state_and_raw_source_status(session, workspace):
    """02 §3's retention axis and 09 §3's pipeline axis normally move independently;
    rejection is the one transition where both change together."""
    source = await _submitted(session)
    await ingestion.classify_source(session, source=source, call=_classifies_as())
    await ingestion.check_duplicates(session, source=source, summary="x", ingestion_policy="gated")
    assert source.pipeline_state is PipelineState.pending_review

    state = await ingestion.reject_source(session, source=source, reason="duplicate of policy X")
    await session.commit()

    assert state is PipelineState.rejected
    assert source.pipeline_state is PipelineState.rejected
    assert source.status is RawSourceStatus.rejected


async def test_reject_finalizes_the_placeholder_with_the_reason(session, workspace):
    source = await _submitted(session)
    await ingestion.classify_source(session, source=source, call=_classifies_as())
    await ingestion.check_duplicates(session, source=source, summary="x", ingestion_policy="gated")

    await ingestion.reject_source(session, source=source, reason="duplicate of policy X")
    await session.commit()

    page = await _source_page(session, workspace, source)
    version = await session.get(PageVersion, page.current_version_id)
    assert "duplicate of policy X" in version.content
    assert page.status is PageStatus.draft


async def test_rejecting_before_classification_resolved_does_not_crash(session, workspace):
    """Low-confidence classification reaches pending_review with no workspace resolved —
    there is no placeholder to finalize, and reject_source must handle that gracefully."""
    source = await _submitted(session)
    await ingestion.classify_source(
        session, source=source, call=_classifies_as(confidence=0.1)
    )
    assert source.workspace_id is None

    state = await ingestion.reject_source(session, source=source, reason="not a fit")
    await session.commit()
    assert state is PipelineState.rejected
    assert source.status is RawSourceStatus.rejected
