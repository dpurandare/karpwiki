"""Ingestion orchestration (03 §3, §4, §6, phase1-tasklist steps 9-12, 19).

Performs the decisions in `classify.py`, `dedup.py`, and `curate.py` against the database
and object store: runs the pipeline transitions, relocates the stored object once a
workspace is known, and parks the source for review when a gate refuses.

Also performs the pipeline-side effects of an admin resolving a review item (05 §1,
`resolve_review_item` and friends) — the counterpart to `review.py`'s generic bookkeeping,
kept separate to avoid a circular import (`review.py` cannot depend back on this module).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import classify, curate, dedup, document_types, llm, objectstore, pipeline, review, versioning
from .frontmatter import split_frontmatter
from .models import (
    AdminActionLog,
    PageStatus,
    PageType,
    PageVersion,
    PipelineState,
    RawSource,
    RawSourceStatus,
    ReviewItem,
    ReviewKind,
    VersionTrigger,
    WikiPage,
    Workspace,
)

logger = logging.getLogger(__name__)

# 09 §6's SCHEMA.md default, used when the workspace sets no threshold.
DEFAULT_MIN_CONFIDENCE = 0.75


class InvalidResolutionError(ValueError):
    """A review-item resolution request doesn't fit: wrong kind, wrong pipeline state, an
    action outside the kind's vocabulary, or missing evidence (e.g. `merge` with no
    matched page recorded)."""


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
    call: ClassifierCall = call_model,
    min_confidence: float | None = None,
) -> PipelineState:
    """Take a `submitted` source through classification to its next resting state.

    No `workspace` parameter: 03 §3 routes against the full central taxonomy (every
    `document_type` in every *active* workspace, phase2-tasklist.md step 24), resolving
    `document_type -> workspace_id` only once a label is chosen — not from a workspace the
    caller already picked, which was Phase 1's single-workspace simplification.

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
    types = [dt.type_code for dt in await document_types.list_active(session)]
    lexical = classify.lexical_match(f"{source.filename}\n{text}", types)

    try:
        result = await call(
            model=llm.resolve_model("classifier"),
            text=text,
            filename=source.filename,
            document_types=types,
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
        document_types=types,
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
        # Step 10: the gate refuses, so a human decides.
        await pipeline.transition(
            session,
            source=source,
            to_state=PipelineState.pending_review,
            actor="system:classifier",
            detail={**detail, "candidates": list(routing.candidates)},
        )
        # No workspace_id: classification never resolved one, and resolving this item —
        # an admin picking a document_type (03 §3) — may be exactly what does.
        # No proposed_action: 03 §5's table is explicit that classification has none —
        # the admin picks, there's nothing to pre-fill. The refusal reason and candidate
        # labels are already in the ingestion_log detail written above.
        await review.create(
            session, kind=ReviewKind.classification, subject_ref=str(source.source_id)
        )
        return PipelineState.pending_review

    # 03 §3 step 6: document_type -> workspace_id via the taxonomy's routing table. `types`
    # (just fetched from list_active) already constrained `routing.document_type` to a
    # registered, active-workspace code, so this can't miss within one request/transaction.
    workspace = await document_types.workspace_for_type(session, type_code=routing.document_type)
    if workspace is None:
        raise RuntimeError(
            f"routing accepted {routing.document_type!r} but its workspace is no longer active"
        )

    return await _accept_classification(
        session,
        source=source,
        workspace=workspace,
        document_type=routing.document_type,
        actor="system:classifier",
        detail=detail,
    )


async def _accept_classification(
    session: AsyncSession,
    *,
    source: RawSource,
    workspace: Workspace,
    document_type: str,
    actor: str,
    detail: dict,
) -> PipelineState:
    """Resolve a source's workspace once its `document_type` is settled (03 §3's accept
    path) — shared by the automatic gate above and an admin's manual resolution
    (`resolve_classification` below, 09 §22), since both do exactly the same work from
    that point on."""
    source.workspace_id = workspace.workspace_id
    relocate(source, workspace.workspace_id)
    await _create_placeholder_source_page(session, source=source, workspace=workspace)
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.classified,
        actor=actor,
        detail={**detail, "document_type": document_type},
    )
    return PipelineState.classified


PLACEHOLDER_SOURCE_BODY = "This document has been submitted and is being processed."


async def _create_placeholder_source_page(
    session: AsyncSession, *, source: RawSource, workspace: Workspace
) -> None:
    """03 §1's placeholder `source` page — created once the workspace is known, not
    literally at `submitted`. A page needs a workspace (partition key) and required
    frontmatter including `workspace_id` (01 §6), neither of which exist before
    classification resolves the workspace; that earlier window has no title or workspace
    to show, so there is nothing meaningful to display anyway.

    `status=draft` per 03 §1's note: this label is UI presentation, not the frontmatter
    `status` field, which only becomes `published` when the Curator finalizes the page.
    """
    await _upsert_singleton(
        session,
        workspace_id=workspace.workspace_id,
        path=f"sources/{source.source_id}.md",
        page_type=PageType.source,
        title=f"Processing: {source.filename}",
        description="Submission awaiting curation.",
        tags=["source", "processing"],
        body=PLACEHOLDER_SOURCE_BODY,
        status=PageStatus.draft,
    )


async def resolve_classification(
    session: AsyncSession,
    *,
    item: ReviewItem,
    document_type: str,
    actor: str,
) -> PipelineState:
    """Admin resolution of a `classification` review item (03 §3, §5): picking a
    `document_type` resolves the workspace the same way `classify_source` itself does now
    (phase2-tasklist.md step 24) — via the taxonomy's routing table, not an admin-supplied
    `workspace_id`. Runs the same accept path `classify_source` takes when it succeeds on
    its own."""
    if item.kind is not ReviewKind.classification:
        raise InvalidResolutionError(f"review item {item.review_id} is not a classification item")

    source = await session.get(RawSource, uuid.UUID(item.subject_ref))
    if source is None or source.pipeline_state is not PipelineState.pending_review:
        raise InvalidResolutionError(
            f"source for review item {item.review_id} is not awaiting classification"
        )
    workspace = await document_types.workspace_for_type(session, type_code=document_type)
    if workspace is None:
        raise InvalidResolutionError(
            f"{document_type!r} is not a registered document type in an active workspace"
        )

    state = await _accept_classification(
        session,
        source=source,
        workspace=workspace,
        document_type=document_type,
        actor=actor,
        detail={"resolution": "admin_assigned"},
    )
    # Backfill (09 §19's pattern, applied to review_item too): resolving is what settles
    # the workspace this item never had, so from here on it belongs in that workspace's
    # queue view and log.md the same as any other resolved item.
    item.workspace_id = workspace.workspace_id
    await review.resolve(session, item=item, action=document_type, actor=actor)
    return state


async def reject_source(
    session: AsyncSession, *, source: RawSource, reason: str, actor: str = "user:admin"
) -> PipelineState:
    """Admin declines a `pending_review` source (03 §1's `rejected` row).

    The one transition where both status axes move together: `pipeline_state` to
    `rejected` and `raw_source.status` to `rejected` (02 §3's retention axis) — every
    other transition leaves `raw_source.status` at `active` (09 §3).
    """
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.rejected,
        actor=actor,
        detail={"reason": reason},
    )
    source.status = RawSourceStatus.rejected

    if source.workspace_id is not None:
        # A source rejected before classification ever resolved a workspace (e.g. still
        # `pending_review` from low confidence) has no placeholder page to finalize.
        await _upsert_singleton(
            session,
            workspace_id=source.workspace_id,
            path=f"sources/{source.source_id}.md",
            page_type=PageType.source,
            title=f"Rejected: {source.filename}",
            description="This submission was rejected.",
            tags=["source", "rejected"],
            body=f"This submission was rejected.\n\n**Reason:** {reason}",
            status=PageStatus.draft,
        )
    return PipelineState.rejected


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
        await review.create(
            session,
            kind=ReviewKind.duplicate,
            subject_ref=str(source.source_id),
            workspace_id=source.workspace_id,
            severity=finding.severity,
            proposed_action=finding.proposed_action,
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


async def resolve_submission(session: AsyncSession, *, item: ReviewItem, actor: str) -> ReviewItem:
    """03 §5: a `submission` item's only resolution is `acknowledge` — informational, no
    pipeline effect. (An admin reassigning the workspace or halting processing from here
    is a roadmap capability, not built in Phase 1.)"""
    if item.kind is not ReviewKind.submission:
        raise InvalidResolutionError(f"review item {item.review_id} is not a submission item")
    return await review.resolve(session, item=item, action="acknowledge", actor=actor)


async def _duplicate_evidence(session: AsyncSession, source_id: uuid.UUID) -> dict:
    """The detail `check_duplicates` recorded when it parked this source (03 §4) — the
    durable record of which prior source(s)/page(s) it matched. `ReviewItem` itself carries
    no structured detail column (09 §22), so resolution reads it back off `ingestion_log`
    rather than re-running `dedup.check` against what may now be a changed DB state."""
    for entry in reversed(await pipeline.history(session, source_id)):
        if entry.to_state is PipelineState.pending_review and "duplicate_source_ids" in entry.detail:
            return entry.detail
    return {}


async def resolve_duplicate(
    session: AsyncSession,
    *,
    item: ReviewItem,
    source: RawSource,
    action: str,
    actor: str,
    note: str | None = None,
    call: MergeCall | None = None,
) -> PipelineState:
    """Admin resolution of a `duplicate` review item (03 §4): `reject`, `keep_both`,
    `supersede`, or `merge`."""
    if item.kind is not ReviewKind.duplicate:
        raise InvalidResolutionError(f"review item {item.review_id} is not a duplicate item")
    if source.pipeline_state is not PipelineState.pending_review:
        raise InvalidResolutionError(f"source {source.source_id} is not pending_review")

    if action == "reject":
        state = await reject_source(
            session, source=source, reason=note or "duplicate", actor=actor
        )
    elif action == "keep_both":
        # 03 §4: "proceeds to normal ingestion as a distinct source" — the same edge
        # check_duplicates' own "no concerns" path takes.
        await pipeline.transition(
            session,
            source=source,
            to_state=PipelineState.ingesting,
            actor=actor,
            detail={"resolution": "keep_both"},
        )
        state = PipelineState.ingesting
    elif action == "supersede":
        state = await _resolve_supersede(session, source=source, actor=actor)
    elif action == "merge":
        state = await _resolve_merge(
            session, source=source, actor=actor, call=call or call_merge_model
        )
    else:
        raise InvalidResolutionError(f"{action!r} is not a valid duplicate resolution")

    if state is not PipelineState.error:
        # A failed merge (the one branch that can still land on `error`, 09 §22) leaves
        # the item open rather than "resolved" with an action that didn't stick — an
        # admin needs to be able to retry it.
        await review.resolve(
            session, item=item, action=action, actor=actor, detail={"note": note} if note else None
        )
    return state


async def _resolve_supersede(
    session: AsyncSession, *, source: RawSource, actor: str
) -> PipelineState:
    """03 §4: 'existing source/page marked superseded, new one becomes canonical.' Marking
    the prior source(s) `superseded` and letting this one proceed through the normal
    `ingesting` path is sufficient — `curate_source`'s existing title-match upsert
    (`_write_curated_page`) is what updates the corresponding page in place; no new
    curation logic is needed for the page side of this resolution."""
    evidence = await _duplicate_evidence(session, source.source_id)
    old_ids = evidence.get("duplicate_source_ids") or []
    if not old_ids:
        raise InvalidResolutionError(
            f"source {source.source_id} has no recorded prior source(s) to supersede"
        )

    for old_id in old_ids:
        old_source = await session.get(RawSource, uuid.UUID(old_id))
        if old_source is not None and old_source.status is RawSourceStatus.active:
            old_source.status = RawSourceStatus.superseded

    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.ingesting,
        actor=actor,
        detail={"resolution": "supersede", "superseded_source_ids": old_ids},
    )
    return PipelineState.ingesting


async def _resolve_merge(
    session: AsyncSession, *, source: RawSource, actor: str, call: MergeCall
) -> PipelineState:
    """03 §4: 'Curator folds the new source's content into the existing page(s) as an
    update.' Scoped to the near-duplicate verdict's evidence (`similar_pages`) — the only
    verdict that names an actual matched *page* rather than a prior *source*; an
    exact/newer-version duplicate has no page-level match recorded to merge into (09 §22)."""
    evidence = await _duplicate_evidence(session, source.source_id)
    similar = evidence.get("similar_pages") or []
    if not similar:
        raise InvalidResolutionError(
            f"source {source.source_id} has no matched wiki page to merge into "
            "(merge needs near-duplicate evidence)"
        )
    target_path = similar[0]["path"]
    target = (
        await session.execute(
            select(WikiPage).where(
                WikiPage.workspace_id == source.workspace_id, WikiPage.path == target_path
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise InvalidResolutionError(f"matched page {target_path!r} no longer exists")

    # `pending_review -> ingested` is not a legal edge (03 §1) even though a merge *is* a
    # completed ingest — go through `ingesting` first, same as every other path to
    # `ingested`, rather than widening pipeline.py's transition table for this one case.
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.ingesting,
        actor=actor,
        detail={"resolution": "merge", "target_page_id": str(target.page_id)},
    )

    try:
        payload = objectstore.read_bytes(source.object_key)
        new_text = payload.decode("utf-8", errors="replace")
        current_version = await session.get(PageVersion, target.current_version_id)
        _, existing_body = split_frontmatter(current_version.content)
        merged = await call(
            model=llm.resolve_model("curator"),
            existing_body=existing_body,
            new_source_text=new_text,
            filename=source.filename,
        )
        await versioning.write_version(
            session,
            page=target,
            body=merged.body,
            author="system:curator",
            trigger=VersionTrigger.ingest,
            change_summary=merged.change_summary,
        )
    except Exception as exc:
        logger.exception("merge failed for %s -> %s", source.source_id, target.page_id)
        await pipeline.transition(
            session,
            source=source,
            to_state=PipelineState.error,
            actor=actor,
            detail={"step": "merge", "error": type(exc).__name__},
        )
        return PipelineState.error

    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.ingested,
        actor=actor,
        detail={"resolution": "merge", "target_page_id": str(target.page_id)},
    )
    return PipelineState.ingested


async def resolve_review_item(
    session: AsyncSession,
    *,
    item: ReviewItem,
    action: str,
    actor: str,
    note: str | None = None,
    merge_call: MergeCall | None = None,
) -> PipelineState | None:
    """Single entry point the gateway's resolve endpoint calls (05 §1) — dispatches to the
    kind-specific function above by `item.kind`, then leaves the generic bookkeeping
    (`review.resolve`) to whichever one it called. Returns the resulting pipeline state, or
    `None` for `submission` (nothing pipeline-side happens).

    No `workspace` parameter: since phase2-tasklist.md step 24, `resolve_classification`
    derives the workspace from `action` (the chosen `document_type`) itself.
    """
    if item.kind is ReviewKind.submission:
        if action != "acknowledge":
            raise InvalidResolutionError("submission items only accept action='acknowledge'")
        await resolve_submission(session, item=item, actor=actor)
        return None

    source = await session.get(RawSource, uuid.UUID(item.subject_ref))
    if source is None:
        raise InvalidResolutionError(f"no source for review item {item.review_id}")

    if item.kind is ReviewKind.classification:
        return await resolve_classification(
            session, item=item, document_type=action, actor=actor
        )

    if item.kind is ReviewKind.duplicate:
        return await resolve_duplicate(
            session,
            item=item,
            source=source,
            action=action,
            actor=actor,
            note=note,
            call=merge_call or call_merge_model,
        )

    raise InvalidResolutionError(f"resolution for {item.kind.value} items is not implemented")


class CuratorCall(Protocol):
    async def __call__(
        self, *, model: str, source_text: str, filename: str, existing_titles: list[str]
    ) -> curate.CuratedContent: ...


async def call_curator_model(
    *, model: str, source_text: str, filename: str, existing_titles: list[str]
) -> curate.CuratedContent:
    """Real Curator call via Pydantic AI (08 §2)."""
    from pydantic_ai import Agent

    catalog = "\n".join(f"- {t}" for t in existing_titles) or "(none yet)"
    agent = Agent(
        model,
        output_type=curate.CuratedContent,
        system_prompt=(
            "You curate an enterprise wiki from raw source documents, in the style of a "
            "small, well-maintained knowledge base. Write a source page (title, one-sentence "
            "description, 2-5 sentence summary, key points) and propose the concept and "
            "entity pages this source warrants — typically a handful, not dozens. A page's "
            "title must match an existing page's title EXACTLY to update it rather than "
            "create a duplicate.\n\n"
            f"Existing concept/entity pages in this workspace:\n{catalog}"
        ),
    )
    result = await agent.run(f"Filename: {filename}\n\n{source_text}")
    return result.output


class MergeCall(Protocol):
    """The LLM call `_resolve_merge` above makes, isolated the same way `CuratorCall` is."""

    async def __call__(
        self, *, model: str, existing_body: str, new_source_text: str, filename: str
    ) -> curate.MergedPage: ...


async def call_merge_model(
    *, model: str, existing_body: str, new_source_text: str, filename: str
) -> curate.MergedPage:
    """Real merge call via Pydantic AI — 03 §4's `merge` duplicate resolution."""
    from pydantic_ai import Agent

    agent = Agent(
        model,
        output_type=curate.MergedPage,
        system_prompt=(
            "You maintain a page in an enterprise wiki. An admin has decided a newly "
            "submitted document duplicates this page's subject and should be folded into "
            "it rather than becoming a separate page. Rewrite the page's full body to "
            "incorporate anything new, corrected, or updated from the submitted document, "
            "preserving what still holds from the existing page. Then write a one-sentence "
            "change summary noting that this update came from a merge."
        ),
    )
    result = await agent.run(
        f"Existing page body:\n\n{existing_body}\n\n---\n\n"
        f"Newly submitted document ({filename}):\n\n{new_source_text}"
    )
    return result.output


async def curate_source(
    session: AsyncSession,
    *,
    source: RawSource,
    workspace: Workspace,
    call: CuratorCall = call_curator_model,
) -> PipelineState:
    """Curator ingest (03 §6, step 12): raw source -> source/concept/entity pages,
    overview.md and log.md refreshed, index_status implicitly `pending`/`stale` via the
    versioning primitives that already do this on every write (02 §7)."""
    try:
        payload = objectstore.read_bytes(source.object_key)
        text = payload.decode("utf-8", errors="replace")

        existing = await _existing_concept_entity_pages(session, workspace.workspace_id)
        content = await call(
            model=llm.resolve_model("curator"),
            source_text=text,
            filename=source.filename,
            existing_titles=[p.title for p in existing],
        )

        await _write_source_page(session, source=source, workspace=workspace, content=content)
        pages_touched = 1
        for page in content.pages:
            await _write_curated_page(
                session, workspace_id=workspace.workspace_id, page=page, existing=existing
            )
            pages_touched += 1
    except Exception as exc:
        # 03 §6: "on failure at any step" — this covers steps 1-3; the state transitions
        # in steps 4-7 below cannot themselves fail into `error` (see the note there).
        logger.exception("curator failed for %s", source.source_id)
        await pipeline.transition(
            session,
            source=source,
            to_state=PipelineState.error,
            actor="system:curator",
            detail={"step": "curate", "error": type(exc).__name__},
        )
        return PipelineState.error

    # 03 §6 orders "append to ingestion_log" (step 5) before "set pipeline state" (step 7),
    # but 09 §3 fuses them into one atomic write — pipeline.transition does both. That
    # means this transition must happen *before* log.md is regenerated, or log.md would
    # never show the ingest that triggered it. It also means `ingested` is now terminal
    # (pipeline.TRANSITIONS has no ingested -> error edge), so a failure regenerating the
    # bookkeeping pages below must not attempt one — the ingest itself already succeeded.
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.ingested,
        actor="system:curator",
        detail={"pages_touched": pages_touched},
    )

    try:
        await _refresh_overview(session, workspace_id=workspace.workspace_id)
        await refresh_log(session, workspace_id=workspace.workspace_id)
    except Exception:
        logger.exception("failed to refresh overview.md/log.md for %s", source.source_id)

    return PipelineState.ingested


async def _existing_concept_entity_pages(
    session: AsyncSession, workspace_id: str
) -> list[curate.ExistingPage]:
    result = await session.execute(
        select(WikiPage, PageVersion)
        .join(PageVersion, WikiPage.current_version_id == PageVersion.version_id)
        .where(
            WikiPage.workspace_id == workspace_id,
            WikiPage.page_type.in_([PageType.concept, PageType.entity]),
            WikiPage.status == PageStatus.published,
        )
    )
    return [
        curate.ExistingPage(page_id=wp.page_id, title=str(pv.frontmatter.get("title", "")), path=wp.path)
        for wp, pv in result.all()
    ]


async def _write_source_page(
    session: AsyncSession, *, source: RawSource, workspace: Workspace, content: curate.CuratedContent
) -> None:
    """Finalize the source page (03 §6 step 2): if step 13's placeholder already exists
    (the normal case — classify_source creates it before curate_source ever runs), this
    updates that same page rather than creating a duplicate."""
    body = curate.render_source_body(content, filename=source.filename)
    await _upsert_singleton(
        session,
        workspace_id=workspace.workspace_id,
        path=f"sources/{source.source_id}.md",
        page_type=PageType.source,
        title=content.source_title,
        description=content.source_description,
        tags=["source", str(source.content_shape.value if source.content_shape else "narrative")],
        body=body,
        status=PageStatus.published,
    )


async def _write_curated_page(
    session: AsyncSession,
    *,
    workspace_id: str,
    page: curate.CuratedPage,
    existing: list[curate.ExistingPage],
) -> None:
    match = curate.match_existing(page.title, existing)
    page_type = PageType.concept if page.page_type == "concept" else PageType.entity

    if match is not None:
        wiki_page = await session.get(WikiPage, match.page_id)
        await versioning.write_version(
            session,
            page=wiki_page,
            body=page.body,
            author="system:curator",
            trigger=VersionTrigger.ingest,
            change_summary="Updated during source ingest.",
        )
        return

    await versioning.create_page(
        session,
        workspace_id=workspace_id,
        path=f"{curate.PAGE_DIRECTORY[page.page_type]}/{curate.slugify(page.title)}.md",
        page_type=page_type,
        title=page.title,
        description=page.tags[0] if page.tags else page.title,
        date=date.today(),
        tags=page.tags,
        body=page.body,
        author="system:curator",
        status=PageStatus.published,
    )


async def _refresh_overview(session: AsyncSession, *, workspace_id: str) -> None:
    source_count = await _count(
        session, PageType.source, workspace_id=workspace_id
    )
    page_count = await _count_all_pages(session, workspace_id=workspace_id)

    result = await session.execute(
        select(PageVersion.frontmatter, WikiPage.path, PageVersion.created_at)
        .join(WikiPage, WikiPage.current_version_id == PageVersion.version_id)
        .where(WikiPage.workspace_id == workspace_id, WikiPage.page_type == PageType.source)
        .order_by(PageVersion.created_at.desc())
        .limit(curate.OVERVIEW_RECENT_LIMIT)
    )
    recent = [
        (str(fm.get("title", "")), str(fm.get("description", "")), path) for fm, path, _ in result.all()
    ]
    body = curate.render_overview_body(source_count=source_count, page_count=page_count, recent=recent)
    await _upsert_singleton(
        session,
        workspace_id=workspace_id,
        path="overview.md",
        page_type=PageType.overview,
        title=f"{workspace_id} Overview",
        body=body,
    )


async def refresh_log(session: AsyncSession, *, workspace_id: str) -> None:
    """log.md merges `ingestion_log` and `admin_action_log` (02 §5, 09 §23) — `lint_log`
    doesn't exist in Phase 1, no lint pass is built.

    Public: `curate_source` below calls it after an ingest, and `api.py`'s rollback
    endpoint calls it after a rollback — `versioning.rollback` can't call it itself
    without a circular import (`ingestion.py` already imports `versioning.py`).
    """
    rows: list[tuple] = []

    for entry in await pipeline.recent_ingested(
        session, workspace_id=workspace_id, limit=curate.LOG_RECENT_LIMIT
    ):
        source = await session.get(RawSource, entry.source_id)
        pages_touched = entry.detail.get("pages_touched") if isinstance(entry.detail, dict) else None
        filename = source.filename if source else "?"
        rows.append(
            (entry.created_at, f"Ingested `{filename}` → {pages_touched or 0} page(s) touched")
        )

    for entry in await _recent_admin_actions(
        session, workspace_id=workspace_id, limit=curate.LOG_RECENT_LIMIT
    ):
        rows.append((entry.created_at, f"{entry.actor}: {entry.action} ({entry.subject_ref})"))

    rows.sort(key=lambda row: row[0], reverse=True)
    body = curate.render_log_body(rows)
    await _upsert_singleton(
        session,
        workspace_id=workspace_id,
        path="log.md",
        page_type=PageType.log,
        title=f"{workspace_id} Log",
        body=body,
    )


async def _recent_admin_actions(
    session: AsyncSession, *, workspace_id: str, limit: int
) -> list[AdminActionLog]:
    result = await session.execute(
        select(AdminActionLog)
        .where(AdminActionLog.workspace_id == workspace_id)
        .order_by(AdminActionLog.created_at.desc(), AdminActionLog.entry_id.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def _upsert_singleton(
    session: AsyncSession,
    *,
    workspace_id: str,
    path: str,
    page_type: PageType,
    title: str,
    body: str,
    description: str | None = None,
    tags: list[str] | None = None,
    status: PageStatus = PageStatus.published,
) -> WikiPage:
    """Find-or-create a page by (workspace_id, path); update in place if it exists.

    Used for overview.md/log.md (same path, refreshed every ingest) and for the source
    placeholder (step 13): created once classification resolves a workspace, finalized by
    the Curator. Must be idempotent rather than create-once, because 03 §1 allows a failed
    classification to be retried (`pending_review -> classifying`), which would otherwise
    hit this path a second time for the same source.
    """
    resolved_description = description or f"Workspace {page_type.value} page."
    resolved_tags = tags or [page_type.value, "workspace"]

    result = await session.execute(
        select(WikiPage).where(WikiPage.workspace_id == workspace_id, WikiPage.path == path)
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        return await versioning.create_page(
            session,
            workspace_id=workspace_id,
            path=path,
            page_type=page_type,
            title=title,
            description=resolved_description,
            date=date.today(),
            tags=resolved_tags,
            body=body,
            author="system:curator",
            status=status,
        )

    await versioning.write_version(
        session,
        page=existing,
        body=body,
        author="system:curator",
        trigger=VersionTrigger.ingest,
        change_summary=f"Regenerated {path}.",
        frontmatter_updates={
            "title": title,
            "description": resolved_description,
            "tags": resolved_tags,
        },
        status=status,
    )
    return existing


async def _count(session: AsyncSession, page_type: PageType, *, workspace_id: str) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(WikiPage)
        .where(
            WikiPage.workspace_id == workspace_id,
            WikiPage.page_type == page_type,
            WikiPage.status == PageStatus.published,
        )
    )
    return result.scalar_one()


async def _count_all_pages(session: AsyncSession, *, workspace_id: str) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(WikiPage)
        .where(WikiPage.workspace_id == workspace_id, WikiPage.status == PageStatus.published)
    )
    return result.scalar_one()


def relocate(source: RawSource, workspace_id: str) -> None:
    """Move the object under its workspace prefix (readiness item 0.6).

    02 §2's per-workspace prefix is what drives retention lifecycle rules, access
    boundaries, and physical bucket separation, so a source cannot stay in `_inbox` once
    its workspace is known. Copy, then repoint, then delete: a crash between any two steps
    leaves an orphan object, never a lost source.

    Public (not module-private): `bulk_move.py` (phase2-tasklist.md step 27, 09 §11) reuses
    this exact copy-repoint-delete sequence when re-homing a source to a different
    workspace after ingestion, not just at classification time.
    """
    staged = source.object_key
    final = f"/{workspace_id}/sources/{source.source_id}/{source.filename}"
    if staged == final:
        return
    objectstore.write_bytes(final, objectstore.read_bytes(staged))
    source.object_key = final
    objectstore.delete(staged)
