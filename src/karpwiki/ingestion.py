"""Classification orchestration (03 §3, phase1-tasklist steps 9-10).

Performs the decisions in `classify.py` against the database and object store: runs the
pipeline transitions, relocates the stored object once a workspace is known, and parks the
source for review when the gate refuses.
"""

import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from . import classify, dedup, llm, objectstore, pipeline
from .models import PipelineState, RawSource, Workspace

logger = logging.getLogger(__name__)

# 09 §6's SCHEMA.md default, used when the workspace sets no threshold.
DEFAULT_MIN_CONFIDENCE = 0.75


class ClassifierCall(Protocol):
    """The one LLM call in this step, isolated so tests need no network."""

    async def __call__(
        self, *, model: str, text: str, filename: str, document_types: list[str]
    ) -> classify.ClassificationResult: ...


async def call_model(
    *, model: str, text: str, filename: str, document_types: list[str]
) -> classify.ClassificationResult:
    """Real Classifier call via Pydantic AI (08 §2)."""
    from pydantic_ai import Agent

    agent = Agent(
        model,
        output_type=classify.ClassificationResult,
        system_prompt=(
            "You classify documents for an enterprise wiki. Choose exactly one label from "
            "the taxonomy you are given. Report confidence honestly: low confidence is "
            "routed to a human, which is the desired outcome when the document is "
            "ambiguous.\n\nTaxonomy: " + ", ".join(document_types)
        ),
    )
    result = await agent.run(f"Filename: {filename}\n\n{text}")
    return result.output


async def classify_source(
    session: AsyncSession,
    *,
    source: RawSource,
    workspace: Workspace,
    call: ClassifierCall = call_model,
    min_confidence: float | None = None,
) -> PipelineState:
    """Take a `submitted` source through classification to its next resting state.

    Returns the state it ended in: `classified` when the gate accepts, `pending_review`
    when it refuses, `error` when the model call failed after the worker's retries.
    """
    await pipeline.transition(
        session, source=source, to_state=PipelineState.classifying, actor="system:classifier"
    )

    payload = objectstore.read_bytes(source.object_key)
    shape = classify.detect_content_shape(source.filename, payload)
    identity, version = classify.derive_artifact_identity(source.filename, payload, shape)
    source.content_shape = shape
    source.artifact_identity = identity
    source.source_version = version

    text = payload.decode("utf-8", errors="replace")
    lexical = classify.lexical_match(f"{source.filename}\n{text}", workspace.document_types)

    try:
        result = await call(
            model=llm.resolve_model("classifier"),
            text=text,
            filename=source.filename,
            document_types=list(workspace.document_types),
        )
    except Exception as exc:
        # 03 §1: transient failures are retried inside the worker; reaching here means
        # those are exhausted, and an exhausted step is what `error` represents.
        logger.exception("classifier failed for %s", source.source_id)
        await pipeline.transition(
            session,
            source=source,
            to_state=PipelineState.error,
            actor="system:classifier",
            detail={"step": "classify", "error": type(exc).__name__},
        )
        return PipelineState.error

    routing = classify.route(
        result,
        lexical,
        min_confidence=DEFAULT_MIN_CONFIDENCE if min_confidence is None else min_confidence,
        document_types=list(workspace.document_types),
    )
    detail = {
        "summary": result.summary,
        "model_label": result.document_type,
        "confidence": result.confidence,
        "lexical_label": lexical.label if lexical else None,
        "lexical_score": lexical.score if lexical else None,
        "reason": routing.reason,
    }

    if not routing.accepted:
        # Step 10: the gate refuses, so a human decides. The `classification` review item
        # itself is step 14; the pipeline state is what parks the source meanwhile.
        await pipeline.transition(
            session,
            source=source,
            to_state=PipelineState.pending_review,
            actor="system:classifier",
            detail={**detail, "candidates": list(routing.candidates)},
        )
        return PipelineState.pending_review

    source.workspace_id = workspace.workspace_id
    _relocate(source, workspace.workspace_id)
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.classified,
        actor="system:classifier",
        detail={**detail, "document_type": routing.document_type},
    )
    return PipelineState.classified


async def check_duplicates(
    session: AsyncSession,
    *,
    source: RawSource,
    summary: str,
    ingestion_policy: str = "auto",
    near_duplicate_score: float | None = None,
) -> PipelineState:
    """Run `duplicate_check` and route accordingly (03 §4, §7, step 11).

    A duplicate always blocks, whatever the workspace policy; the policy decides only the
    "no concerns found" path.
    """
    await pipeline.transition(
        session, source=source, to_state=PipelineState.duplicate_check, actor="system:ingestion"
    )

    finding = await dedup.check(
        session, source=source, summary=summary, near_duplicate_score=near_duplicate_score
    )

    if finding.blocks:
        await pipeline.transition(
            session,
            source=source,
            to_state=PipelineState.pending_review,
            actor="system:ingestion",
            detail={
                "reason": f"duplicate: {finding.verdict.value}",
                "severity": finding.severity,
                "proposed_action": finding.proposed_action,
                "duplicate_source_ids": [str(s) for s in finding.source_ids],
                "similar_pages": [
                    {"path": h.path, "score": round(h.score, 4)} for h in finding.page_hits
                ],
            },
        )
        return PipelineState.pending_review

    if ingestion_policy == "gated":
        await pipeline.transition(
            session,
            source=source,
            to_state=PipelineState.pending_review,
            actor="system:ingestion",
            detail={"reason": "workspace ingestion policy is gated"},
        )
        return PipelineState.pending_review

    await pipeline.transition(
        session, source=source, to_state=PipelineState.ingesting, actor="system:ingestion"
    )
    return PipelineState.ingesting


def _relocate(source: RawSource, workspace_id: str) -> None:
    """Move the object under its workspace prefix (readiness item 0.6).

    02 §2's per-workspace prefix is what drives retention lifecycle rules, access
    boundaries, and physical bucket separation, so a source cannot stay in `_inbox` once
    its workspace is known. Copy, then repoint, then delete: a crash between any two steps
    leaves an orphan object, never a lost source.
    """
    staged = source.object_key
    final = f"/{workspace_id}/sources/{source.source_id}/{source.filename}"
    if staged == final:
        return
    objectstore.write_bytes(final, objectstore.read_bytes(staged))
    source.object_key = final
    objectstore.delete(staged)
