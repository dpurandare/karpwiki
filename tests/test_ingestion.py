"""Classification orchestration (03 §3, steps 9-10) — database and object store, no network."""

import hashlib
import uuid

import pytest

from karpwiki import classify, ingestion, objectstore, pipeline
from karpwiki.classify import ClassificationResult
from karpwiki.models import ContentShape, PipelineState, RawSource


async def _submitted(session, *, filename="restart-worker-runbook.md", payload=b"# Runbook"):
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


def _returns(label="eng.runbook", confidence=0.9, summary="A runbook."):
    async def _call(**_kwargs):
        return ClassificationResult(summary=summary, document_type=label, confidence=confidence)

    return _call


def _raises(exc):
    async def _call(**_kwargs):
        raise exc

    return _call


async def test_accepted_classification_routes_and_relocates(session, workspace):
    source = await _submitted(session)
    staged = source.object_key

    state = await ingestion.classify_source(
        session, source=source, call=_returns()
    )
    await session.commit()

    assert state is PipelineState.classified
    assert source.workspace_id == workspace.workspace_id
    # Readiness item 0.6: the object moves under its workspace prefix once known.
    assert source.object_key == (
        f"/{workspace.workspace_id}/sources/{source.source_id}/{source.filename}"
    )
    assert objectstore.read_bytes(source.object_key) == b"# Runbook"
    assert not objectstore.exists(staged)


async def test_the_deterministic_pre_step_runs_before_the_model(session, workspace):
    source = await _submitted(
        session, filename="payments-api.json", payload=b'{"name": "payments-api", "version": "2.1"}'
    )
    await ingestion.classify_source(
        session, source=source, call=_returns(confidence=0.4)
    )
    await session.commit()

    # Refused by the gate, yet shape and identity are still recorded — they are not the
    # model's output and do not depend on it.
    assert source.pipeline_state is PipelineState.pending_review
    assert source.content_shape is ContentShape.structured_data
    assert source.artifact_identity == "payments-api"
    assert source.source_version == "2.1"


async def test_low_confidence_parks_the_source_unrouted(session, workspace):
    source = await _submitted(session)
    staged = source.object_key

    state = await ingestion.classify_source(
        session, source=source, call=_returns(confidence=0.2)
    )
    await session.commit()

    assert state is PipelineState.pending_review
    assert source.workspace_id is None
    # Not relocated: no workspace was decided, so there is no prefix to move it under.
    assert source.object_key == staged


async def test_low_confidence_creates_a_classification_review_item(session, workspace):
    """03 §5's table: default resolved_action is None — the admin picks, nothing pre-filled."""
    from sqlalchemy import select

    from karpwiki.models import ReviewItem, ReviewKind

    source = await _submitted(session)
    await ingestion.classify_source(session, source=source, call=_returns(confidence=0.2))
    await session.commit()

    result = await session.execute(
        select(ReviewItem).where(ReviewItem.subject_ref == str(source.source_id))
    )
    item = result.scalar_one()
    assert item.kind is ReviewKind.classification
    assert item.workspace_id is None
    assert item.proposed_action is None


async def test_a_successful_classification_creates_no_review_item(session, workspace):
    from sqlalchemy import select

    from karpwiki.models import ReviewItem

    source = await _submitted(session)
    await ingestion.classify_source(session, source=source, call=_returns())
    await session.commit()

    result = await session.execute(
        select(ReviewItem).where(ReviewItem.subject_ref == str(source.source_id))
    )
    assert result.scalar_one_or_none() is None


async def test_a_disagreement_parks_the_source_with_both_candidates(session, workspace):
    """The filename says runbook; the model says design-doc. A human decides."""
    source = await _submitted(session, filename="restart-worker-runbook.md")
    await ingestion.classify_source(
        session,
        source=source,
        call=_returns(label="eng.design-doc", confidence=0.99),
    )
    await session.commit()

    assert source.pipeline_state is PipelineState.pending_review
    last = (await pipeline.history(session, source.source_id))[-1]
    assert last.detail["reason"] == "lexical cross-check disagreed"
    assert set(last.detail["candidates"]) == {"eng.design-doc", "eng.runbook"}


async def test_a_model_failure_lands_on_error(session, workspace):
    """0.5's outcome: an exhausted Classifier call is representable."""
    source = await _submitted(session)
    state = await ingestion.classify_source(
        session, source=source, call=_raises(TimeoutError("upstream"))
    )
    await session.commit()

    assert state is PipelineState.error
    last = (await pipeline.history(session, source.source_id))[-1]
    assert last.detail == {"step": "classify", "error": "TimeoutError"}


async def test_the_summary_is_recorded_for_duplicate_detection(session, workspace):
    """03 §4 runs the summary as its near-match query, so it has to survive the step."""
    source = await _submitted(session)
    await ingestion.classify_source(
        session, source=source, call=_returns(summary="Restarts the worker.")
    )
    await session.commit()

    last = (await pipeline.history(session, source.source_id))[-1]
    assert last.detail["summary"] == "Restarts the worker."


async def test_history_records_every_step_of_the_attempt(session, workspace):
    source = await _submitted(session)
    await ingestion.classify_source(
        session, source=source, call=_returns()
    )
    await session.commit()

    states = [e.to_state for e in await pipeline.history(session, source.source_id)]
    assert states == [PipelineState.classifying, PipelineState.classified]
