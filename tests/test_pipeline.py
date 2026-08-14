"""Ingestion pipeline state machine (03 §1, 09 §3) — phase1-tasklist step 8."""

import hashlib
import uuid

import pytest

from karpwiki import pipeline
from karpwiki.models import PipelineState, RawSource, RawSourceStatus


async def _source(session, workspace_id=None) -> RawSource:
    """A source as it exists at `submitted`: stored, but not yet routed anywhere."""
    body = f"payload-{uuid.uuid4()}".encode()
    source = RawSource(
        workspace_id=workspace_id,
        object_key=f"/inbox/{uuid.uuid4()}.md",
        filename="retry-policy.md",
        content_hash=hashlib.sha256(body).hexdigest(),
        submitted_by="user:deepak",
        status=RawSourceStatus.active,
        pipeline_state=PipelineState.submitted,
    )
    session.add(source)
    await session.flush()
    return source


async def test_a_source_exists_before_any_workspace_is_known(session):
    """03 §1: the raw_source row is created at `submitted`; `classifying` resolves the
    workspace. The column must therefore be nullable."""
    source = await _source(session)
    await session.commit()
    assert source.workspace_id is None
    assert source.pipeline_state is PipelineState.submitted


async def test_transition_moves_the_pointer_and_appends_history(session, workspace):
    source = await _source(session)
    entry = await pipeline.transition(
        session, source=source, to_state=PipelineState.classifying, actor="system:classifier"
    )
    await session.commit()

    assert source.pipeline_state is PipelineState.classifying
    assert entry.from_state is PipelineState.submitted
    assert entry.to_state is PipelineState.classifying
    assert entry.actor == "system:classifier"


async def test_full_happy_path_is_walkable(session, workspace):
    source = await _source(session)
    walk = [
        PipelineState.classifying,
        PipelineState.classified,
        PipelineState.duplicate_check,
        PipelineState.ingesting,
        PipelineState.ingested,
    ]
    for state in walk:
        await pipeline.transition(session, source=source, to_state=state, actor="system:curator")
    await session.commit()

    assert source.pipeline_state is PipelineState.ingested
    recorded = [e.to_state for e in await pipeline.history(session, source.source_id)]
    assert recorded == walk


async def test_history_order_is_stable_within_one_transaction(session, workspace):
    """Several transitions commonly share a transaction; now() would tie their timestamps."""
    source = await _source(session)
    for state in (PipelineState.classifying, PipelineState.classified):
        await pipeline.transition(session, source=source, to_state=state, actor="system:classifier")
    await session.commit()

    recorded = await pipeline.history(session, source.source_id)
    assert [e.to_state for e in recorded] == [PipelineState.classifying, PipelineState.classified]
    assert recorded[0].created_at < recorded[1].created_at


async def test_illegal_transition_is_refused(session, workspace):
    source = await _source(session)
    with pytest.raises(pipeline.IllegalTransition, match="submitted -> ingesting"):
        await pipeline.transition(
            session, source=source, to_state=PipelineState.ingesting, actor="user:deepak"
        )
    assert source.pipeline_state is PipelineState.submitted


async def test_terminal_states_admit_nothing(session, workspace):
    for terminal in pipeline.TERMINAL:
        assert pipeline.TRANSITIONS[terminal] == frozenset()

    source = await _source(session)
    for state in (
        PipelineState.classifying,
        PipelineState.pending_review,
        PipelineState.rejected,
    ):
        await pipeline.transition(session, source=source, to_state=state, actor="user:admin")
    with pytest.raises(pipeline.IllegalTransition, match="terminal"):
        await pipeline.transition(
            session, source=source, to_state=PipelineState.ingesting, actor="user:admin"
        )


async def test_review_and_error_paths(session, workspace):
    """Low confidence parks a source for review; an ingest failure routes back to review."""
    source = await _source(session)
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.classifying,
        actor="system:classifier",
    )
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.pending_review,
        actor="system:classifier",
        detail={"reason": "confidence below threshold"},
    )
    await pipeline.transition(
        session, source=source, to_state=PipelineState.ingesting, actor="user:admin"
    )
    await pipeline.transition(
        session, source=source, to_state=PipelineState.error, actor="system:curator"
    )
    await pipeline.transition(
        session, source=source, to_state=PipelineState.pending_review, actor="system:curator"
    )
    await session.commit()

    assert source.pipeline_state is PipelineState.pending_review
    assert len(await pipeline.history(session, source.source_id)) == 5


async def test_every_working_state_can_fail(session, workspace):
    """03 §1: `error` means "a step failed", so it is reachable from every state that runs
    automatic work — a Classifier failure included."""
    for state in pipeline.FAILABLE:
        assert PipelineState.error in pipeline.TRANSITIONS[state], state

    source = await _source(session)
    await pipeline.transition(
        session, source=source, to_state=PipelineState.classifying, actor="system:classifier"
    )
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.error,
        actor="system:classifier",
        detail={"step": "classify", "error": "APITimeoutError", "attempts": 3},
    )
    await session.commit()
    assert source.pipeline_state is PipelineState.error


async def test_nothing_fails_while_waiting_on_a_human(session, workspace):
    """`pending_review` runs no work, so it cannot fail; terminals cannot either."""
    assert PipelineState.pending_review not in pipeline.FAILABLE
    for terminal in pipeline.TERMINAL:
        assert PipelineState.error not in pipeline.TRANSITIONS[terminal]


async def test_review_resumes_at_the_point_the_source_left(session, workspace):
    """A classification review resolves to `classified`, not straight to `ingesting`:
    the admin supplied the document_type, so classification is what just completed."""
    source = await _source(session)
    await pipeline.transition(
        session, source=source, to_state=PipelineState.classifying, actor="system:classifier"
    )
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.pending_review,
        actor="system:classifier",
        detail={"reason": "lexical cross-check disagreed"},
    )
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.classified,
        actor="user:admin",
        detail={"document_type": "eng.runbook"},
    )
    await pipeline.transition(
        session, source=source, to_state=PipelineState.duplicate_check, actor="system:ingestion"
    )
    await session.commit()
    assert source.pipeline_state is PipelineState.duplicate_check


async def test_a_failed_step_can_be_retried_from_review(session, workspace):
    """error -> pending_review -> the state that failed (03 §1's resume table)."""
    source = await _source(session)
    for state in (
        PipelineState.classifying,
        PipelineState.error,
        PipelineState.pending_review,
        PipelineState.classifying,
        PipelineState.classified,
    ):
        await pipeline.transition(session, source=source, to_state=state, actor="user:admin")
    await session.commit()
    assert source.pipeline_state is PipelineState.classified
