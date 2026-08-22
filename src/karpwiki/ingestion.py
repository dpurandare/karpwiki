"""Ingestion orchestration (03 §3, §4, §6, phase1-tasklist steps 9-12, 19).

Performs the decisions in `classify.py`, `dedup.py`, and `curate.py` against the database
and object store: runs the pipeline transitions, relocates the stored object once a
workspace is known, and parks the source for review when a gate refuses.

Also performs the pipeline-side effects of an admin resolving a review item (05 §1,
`resolve_review_item` and friends) — the counterpart to `review.py`'s generic bookkeeping,
kept separate to avoid a circular import (`review.py` cannot depend back on this module).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, date, datetime
from typing import Protocol

from sqlalchemy import func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from . import (
    advisor,
    classify,
    curate,
    dedup,
    doc_extract,
    document_types,
    llm,
    objectstore,
    pii,
    pipeline,
    review,
    schema,
    versioning,
)
from .frontmatter import split_frontmatter
from .models import (
    AdminActionLog,
    Connector,
    ContentShape,
    LintLog,
    PageStatus,
    PageType,
    PageVersion,
    PipelineState,
    RawSource,
    RawSourceStatus,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    VersionTrigger,
    WikiPage,
    Workspace,
)
from .pagination import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, decode_cursor, encode_cursor

logger = logging.getLogger(__name__)

# 09 §6's SCHEMA.md default, used when the workspace sets no threshold.
DEFAULT_MIN_CONFIDENCE = 0.75


class InvalidResolutionError(ValueError):
    """A review-item resolution request doesn't fit: wrong kind, wrong pipeline state, an
    action outside the kind's vocabulary, or missing evidence (e.g. `merge` with no
    matched page recorded)."""


class UnsupportedContentError(ValueError):
    """The submitted content is neither decodable text nor a recognized binary format
    (`doc_extract.py`) — found live during Phase 3 step 62 prep, not part of either
    completeness audit. `connectors_git.py` already skips a file it can't decode rather
    than submitting it; this closes the same gap for every other entry point by rejecting
    at `store()`, before a `raw_source` (and the garbled-text classification/curation that
    would otherwise follow) ever exists."""


async def store(
    session: AsyncSession,
    payload: bytes,
    filename: str,
    *,
    submitted_by: str,
    extra_detail: dict | None = None,
) -> RawSource:
    """Write the object, create the raw_source row, and open its ingestion_log history —
    the one entry point every submission source goes through (03 §2): REST `POST
    /sources`, the MCP `wiki_submit` tool, and the connector poller (phase2-tasklist.md
    step 52) all call this directly rather than each writing their own copy. Moved here
    from `api.py` in step 52 — `connector_polling.py` needs it too, and importing `api`
    from there would cycle back (`api` -> `tasks` -> `connector_polling` -> `api`).

    `extra_detail` merges into that first log entry's `detail` — currently only the MCP
    `wiki_submit` tool's on-behalf-of path (09 §5, phase2-tasklist.md step 46) uses it, to
    record the calling agent's own identity for audit without a new core field ("no new
    core field required" is 09 §5's own wording)."""
    if doc_extract.extract_text(filename, payload) is None:
        raise UnsupportedContentError(
            f"{filename!r} could not be read as text, PDF, or DOCX content"
        )

    source_id = uuid.uuid4()
    # Staged outside any workspace prefix: 02 §2's /{workspace_id}/sources/... scheme
    # cannot apply yet because 03 §2 accepts the source before the workspace is known.
    # See readiness item 0.6 — classification has to settle where it ends up.
    object_key = f"/_inbox/{source_id}/{filename}"
    objectstore.write_bytes(object_key, payload)

    source = RawSource(
        source_id=source_id,
        object_key=object_key,
        filename=filename,
        content_hash=hashlib.sha256(payload).hexdigest(),
        submitted_by=submitted_by,
        pipeline_state=PipelineState.submitted,
    )
    session.add(source)
    await session.flush()

    session.add(
        pipeline.IngestionLog(
            source_id=source.source_id,
            from_state=None,
            to_state=PipelineState.submitted,
            actor=submitted_by,
            detail={"object_key": object_key, **(extra_detail or {})},
        )
    )
    # 03 §5: every submission gets an always-on informational review item, unconditionally
    # and regardless of what happens downstream. No workspace yet — none is resolved until
    # classification succeeds.
    await review.create(session, kind=ReviewKind.submission, subject_ref=str(source.source_id))
    await session.flush()
    return source


class ClassifierCall(Protocol):
    """The one LLM call in this step, isolated so tests need no network."""

    async def __call__(
        self, *, model: str, text: str, filename: str, document_types: list[str]
    ) -> classify.ClassificationResult: ...


async def call_model(
    *, model: str, text: str, filename: str, document_types: list[str]
) -> classify.ClassificationResult:
    """Real Classifier call via Pydantic AI (08 §2). Transient failures retried with
    backoff (03 §1, `llm.retry_transient`) — only an exhausted run propagates."""
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
    result = await llm.retry_transient(lambda: agent.run(f"Filename: {filename}\n\n{text}"))
    return result.output


async def _pii_already_acknowledged(session: AsyncSession, *, source_id: uuid.UUID) -> bool:
    """07 §2, phase3-tasklist.md step 71 — has an admin already resolved a `pii_review`
    item for this exact source with `acknowledge`? The resolved item is itself the audit
    record of that decision, so this reuses it rather than tracking new state."""
    result = await session.execute(
        select(ReviewItem.review_id).where(
            ReviewItem.subject_ref == str(source_id),
            ReviewItem.kind == ReviewKind.pii_review,
            ReviewItem.status == ReviewStatus.resolved,
            ReviewItem.resolved_action == "acknowledge",
        )
    )
    return result.scalar_one_or_none() is not None


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

    # `store()` already rejected anything doc_extract.extract_text can't read before this
    # raw_source ever existed — `or ""` is a defensive fallback, not the real gate.
    text = doc_extract.extract_text(source.filename, payload) or ""

    # PII detection (07 §2, phase3-tasklist.md step 71) — the pre-step's own deterministic
    # position (03 §3), before the Classifier's LLM call, and deliberately blocking here
    # rather than proceeding: never send PII/credential content to a third-party model,
    # and never spend the call on a source about to be blocked anyway.
    #
    # Skipped once this exact source already has a resolved `acknowledge` on record — found
    # live by a test that actually re-ran classification after acknowledging, not assumed:
    # without this, a genuinely PII-containing source could never actually get past the
    # gate at all — every re-dispatch re-detects the same content and re-blocks, an
    # unbreakable acknowledge-reclassify-reblock loop. The resolved `ReviewItem` itself is
    # the audit record of the admin decision to accept the risk, so this reuses it rather
    # than adding new state to track "already acknowledged."
    pii_categories = pii.detect_pii(text)
    if pii_categories and not await _pii_already_acknowledged(session, source_id=source.source_id):
        await pipeline.transition(
            session,
            source=source,
            to_state=PipelineState.pending_review,
            actor="system:pii_scanner",
            detail={"reason": "pii_detected", "categories": pii_categories},
        )
        await review.create(
            session,
            kind=ReviewKind.pii_review,
            subject_ref=str(source.source_id),
            detail={"categories": pii_categories},
        )
        return PipelineState.pending_review

    types = [dt.type_code for dt in await document_types.list_active(session)]
    lexical = classify.lexical_match(f"{source.filename}\n{text}", types)

    try:
        # No per-workspace override here, unlike every other resolve_model call site: which
        # workspace this source belongs to is exactly what classification is about to
        # determine (03 §3) — there is no workspace, and so no SCHEMA.md, to read yet.
        # Always the platform default.
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
            detail=llm.failure_detail("classify", exc),
        )
        return PipelineState.error

    # 09 §27 flagged this ordering as needing revisiting once SCHEMA.md thresholds are real
    # (phase3-tasklist.md step 59): `result.document_type` — the model's chosen label — is
    # already known here, before the gate runs, so its owning workspace can be resolved now
    # to read *that* workspace's own `min_confidence` rather than always the platform
    # default. `routing_workspace` is reused below on the accept path (routing.document_type
    # is always `result.document_type` whenever `routing.accepted`, per `classify.route`'s
    # own logic) rather than looked up a second time.
    routing_workspace = await document_types.workspace_for_type(
        session, type_code=result.document_type
    )
    resolved_min_confidence = min_confidence
    if resolved_min_confidence is None:
        resolved_min_confidence = DEFAULT_MIN_CONFIDENCE
        if routing_workspace is not None:
            workspace_schema = await schema.load(session, workspace_id=routing_workspace.workspace_id)
            if (
                workspace_schema is not None
                and workspace_schema.thresholds.classification.min_confidence is not None
            ):
                resolved_min_confidence = workspace_schema.thresholds.classification.min_confidence

    routing = classify.route(
        result,
        lexical,
        min_confidence=resolved_min_confidence,
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

    # `routing_workspace` was already resolved above (from `result.document_type`, to read
    # its threshold for the gate) — reused here rather than looked up a second time, valid
    # since `routing.document_type is result.document_type` whenever `routing.accepted`.
    workspace = routing_workspace
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


async def resolve_pii_review(
    session: AsyncSession, *, item: ReviewItem, source: RawSource, action: str, actor: str, note: str | None = None
) -> PipelineState:
    """Admin resolution of a `pii_review` item (07 §2, phase3-tasklist.md step 71):
    `acknowledge` (the flagged content is expected/acceptable — proceed) or `reject`
    (decline the source outright, reusing `reject_source` unchanged) — mirroring
    `classification`/`duplicate`'s own action shape rather than inventing a new resolution
    model, per this step's own text.

    `acknowledge` makes no pipeline transition of its own — the source stays at
    `pending_review`, exactly the resting state `pending_review -> classifying` (03 §1's
    "admin retries a failed classification" edge) resumes from once `classify_source` is
    re-dispatched (`api.run_resolve_review_item`, after commit, checking `item.kind`/
    `action` directly rather than this function's return value). Transitioning to
    `classifying` *here* was tried and found to be a real bug: `classify_source`'s own
    first line unconditionally transitions `-> classifying` too, and `classifying ->
    classifying` is not a legal self-edge — caught by a test that actually re-ran
    classification after acknowledging, not assumed."""
    if item.kind is not ReviewKind.pii_review:
        raise InvalidResolutionError(f"review item {item.review_id} is not a pii_review item")
    if source.pipeline_state is not PipelineState.pending_review:
        raise InvalidResolutionError(f"source {source.source_id} is not pending_review")

    if action == "acknowledge":
        state = PipelineState.pending_review
    elif action == "reject":
        state = await reject_source(session, source=source, reason=note or "pii_review", actor=actor)
    else:
        raise InvalidResolutionError(f"{action!r} is not a valid pii_review resolution (acknowledge | reject)")

    await review.resolve(
        session, item=item, action=action, actor=actor, detail={"note": note} if note else None
    )
    return state


async def resolve_ingestion_policy(
    session: AsyncSession, *, source: RawSource, workspace_schema: schema.WorkspaceSchema | None
) -> str:
    """The effective `auto`/`gated` policy for `source` (03 §7, 09 §6's SCHEMA.md
    `ingestion_policy` field, phase3-tasklist.md step 59) — the workspace's own
    schema-configured policy (`"auto"` if none is set), tightened to `"gated"` if the
    source's connector (`submitted_by="connector:<id>"`) is itself configured `gated`.

    09 §13: a connector's own `ingestion_policy` "may only tighten... never relax" its
    workspace's policy — this is that comparison, real now that a workspace's own policy is
    real content instead of only ever a SCHEMA.md template field. The tasklist's own text
    names `connector_polling.poll_connector` as where to wire this, but that function only
    ever creates a `raw_source` unconditionally (03 §2: "indistinguishable from any other
    submission") — it has no gating decision to make. The actual `auto`/`gated` decision
    happens here, at curate time (`check_duplicates` below), which is where the workspace's
    policy already governs the "no concerns found" path.
    """
    workspace_policy = workspace_schema.ingestion_policy if workspace_schema else "auto"
    if source.submitted_by.startswith("connector:"):
        connector_id = uuid.UUID(source.submitted_by.removeprefix("connector:"))
        connector = await session.get(Connector, connector_id)
        if connector is not None and connector.ingestion_policy == "gated":
            return "gated"
    return workspace_policy


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
            old_source.superseded_at = datetime.now(UTC)

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
        new_text = doc_extract.extract_text(source.filename, payload) or ""
        current_version = await session.get(PageVersion, target.current_version_id)
        _, existing_body = split_frontmatter(current_version.content)
        workspace_schema = await schema.load(session, workspace_id=source.workspace_id)
        merged = await call(
            model=llm.resolve_model("curator", schema.as_dict(workspace_schema)),
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
            detail=llm.failure_detail("merge", exc),
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

    if item.kind in (ReviewKind.reindex, ReviewKind.prune):
        # `subject_ref` is never a source_id here — a workspace_id for the batched
        # reasons (phase2-tasklist.md steps 36-37, 39), a page_id for the pair-specific
        # `contradicted_by` reason (step 40, same shape as duplicate's page_id below) —
        # resolved entirely in `advisor.py`, before the RawSource lookup below, which
        # doesn't apply to either kind regardless.
        resolver = advisor.resolve_reindex if item.kind is ReviewKind.reindex else advisor.resolve_prune
        try:
            await resolver(session, item=item, action=action, actor=actor)
        except advisor.InvalidResolutionError as exc:
            raise InvalidResolutionError(str(exc)) from exc
        return None

    if item.kind is ReviewKind.stuck:
        # Same "not a source_id" shape as reindex/prune above: `subject_ref` here is a
        # fixed sentinel, not a RawSource id, so this must also branch before the lookup
        # below. `resolve_stuck` itself stays bookkeeping-only (advisor.py, same reason as
        # resolve_reindex/resolve_prune); `abort`'s actual per-source `rejected` transition
        # happens right here, since it's the one piece of kind-specific pipeline work this
        # resolution needs and `advisor.py` can't reach `reject_source` without a circular
        # import. `retry` needs no pipeline-side change at all here — re-dispatching the
        # correct Celery task per source is `api.py`'s job, done after commit
        # (`run_resolve_review_item`), the same way reindex's own dispatch is.
        try:
            await advisor.resolve_stuck(session, item=item, action=action, actor=actor)
        except advisor.InvalidResolutionError as exc:
            raise InvalidResolutionError(str(exc)) from exc
        if action == "abort":
            for entry in (item.detail or {}).get("sources", []):
                stuck_source = await session.get(RawSource, uuid.UUID(entry["source_id"]))
                if stuck_source is not None and stuck_source.pipeline_state in pipeline.ABORTABLE_IF_STUCK:
                    await reject_source(
                        session,
                        source=stuck_source,
                        reason="stuck pipeline sweep: aborted by admin",
                        actor=actor,
                    )
        return None

    if item.kind is ReviewKind.duplicate and (item.detail or {}).get("raised_by") == "advisor":
        # `subject_ref` is a page_id here (phase2-tasklist.md step 38), not a source_id —
        # there is no RawSource behind an existing-content duplicate finding at all, so
        # this must also branch before the RawSource lookup below. `resolve_duplicate`
        # itself stays completely untouched — this is a different function for a different
        # subject shape, not a variant of it.
        try:
            await advisor.resolve_existing_duplicate(session, item=item, action=action, actor=actor)
        except advisor.InvalidResolutionError as exc:
            raise InvalidResolutionError(str(exc)) from exc
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

    if item.kind is ReviewKind.pii_review:
        return await resolve_pii_review(
            session, item=item, source=source, action=action, actor=actor, note=note
        )

    raise InvalidResolutionError(f"resolution for {item.kind.value} items is not implemented")


class CuratorCall(Protocol):
    async def __call__(
        self, *, model: str, source_text: str, filename: str, existing_titles: list[str]
    ) -> curate.CuratedContent: ...


async def call_curator_model(
    *, model: str, source_text: str, filename: str, existing_titles: list[str]
) -> curate.CuratedContent:
    """Real Curator call via Pydantic AI (08 §2). Transient failures retried with backoff
    (03 §1, `llm.retry_transient`) — only an exhausted run propagates."""
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
    result = await llm.retry_transient(lambda: agent.run(f"Filename: {filename}\n\n{source_text}"))
    return result.output


class StructuredCuratorCall(Protocol):
    """The `structured_data` counterpart to `CuratorCall` above (07 §1.3,
    phase3-tasklist.md step 61) — same shape, different output type."""

    async def __call__(
        self, *, model: str, source_text: str, filename: str, existing_titles: list[str]
    ) -> curate.StructuredCuratedContent: ...


async def call_structured_curator_model(
    *, model: str, source_text: str, filename: str, existing_titles: list[str]
) -> curate.StructuredCuratedContent:
    """Real structured-data Curator call via Pydantic AI (07 §1.3, 08 §2). Transient
    failures retried with backoff (03 §1, `llm.retry_transient`), same as
    `call_curator_model`."""
    from pydantic_ai import Agent

    catalog = "\n".join(f"- {t}" for t in existing_titles) or "(none yet)"
    agent = Agent(
        model,
        output_type=curate.StructuredCuratedContent,
        system_prompt=(
            "You curate an enterprise wiki from a structured data artifact — a schema, "
            "config file, API spec, or data dictionary, not prose. Extract its fields, "
            "columns, parameters, or endpoints as a structure table; write a one-sentence "
            "intent statement (what this artifact is for, what system or process it "
            "supports, who owns/produces/consumes it — inferred from context, not "
            "invented); and propose an entity page for each major table, resource, or "
            "config section it defines, when significant enough to be referenced "
            "elsewhere. A page's title must match an existing page's title EXACTLY to "
            "update it rather than create a duplicate.\n\n"
            f"Existing concept/entity pages in this workspace:\n{catalog}"
        ),
    )
    result = await llm.retry_transient(lambda: agent.run(f"Filename: {filename}\n\n{source_text}"))
    return result.output


class MergeCall(Protocol):
    """The LLM call `_resolve_merge` above makes, isolated the same way `CuratorCall` is."""

    async def __call__(
        self, *, model: str, existing_body: str, new_source_text: str, filename: str
    ) -> curate.MergedPage: ...


async def call_merge_model(
    *, model: str, existing_body: str, new_source_text: str, filename: str
) -> curate.MergedPage:
    """Real merge call via Pydantic AI — 03 §4's `merge` duplicate resolution. Transient
    failures retried with backoff (03 §1, `llm.retry_transient`) — only an exhausted run
    propagates."""
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
    result = await llm.retry_transient(
        lambda: agent.run(
            f"Existing page body:\n\n{existing_body}\n\n---\n\n"
            f"Newly submitted document ({filename}):\n\n{new_source_text}"
        )
    )
    return result.output


async def curate_source(
    session: AsyncSession,
    *,
    source: RawSource,
    workspace: Workspace,
    call: CuratorCall = call_curator_model,
    structured_call: StructuredCuratorCall = call_structured_curator_model,
) -> PipelineState:
    """Curator ingest (03 §6, step 12): raw source -> source/concept/entity pages,
    overview.md/log.md/index.md refreshed, index_status implicitly `pending`/`stale` via
    the versioning primitives that already do this on every write (02 §7).

    Branches on `content_shape` (07 §1.1's two treatments, phase3-tasklist.md step 61): a
    `structured_data` source gets the metadata-first structure-table+intent-statement
    treatment (`structured_call`/`_write_structured_source_page`) instead of the narrative
    summary+citations one — `content_shape` is a `raw_source` attribute, not a page-type
    distinction (07 §1.1), so both paths still produce an ordinary `page_type: source`
    page and feed the same `pages` create-or-update loop below.
    """
    try:
        payload = objectstore.read_bytes(source.object_key)
        text = doc_extract.extract_text(source.filename, payload) or ""

        existing = await _existing_concept_entity_pages(session, workspace.workspace_id)
        workspace_schema = await schema.load(session, workspace_id=workspace.workspace_id)
        model = llm.resolve_model("curator", schema.as_dict(workspace_schema))

        if source.content_shape is ContentShape.structured_data:
            structured_content = await structured_call(
                model=model,
                source_text=text,
                filename=source.filename,
                existing_titles=[p.title for p in existing],
            )
            await _write_structured_source_page(
                session, source=source, workspace=workspace, content=structured_content
            )
            pages = structured_content.pages
        else:
            content = await call(
                model=model,
                source_text=text,
                filename=source.filename,
                existing_titles=[p.title for p in existing],
            )
            await _write_source_page(session, source=source, workspace=workspace, content=content)
            pages = content.pages

        pages_touched = 1
        for page in pages:
            wiki_page = await _write_curated_page(
                session, workspace_id=workspace.workspace_id, page=page, existing=existing
            )
            await _score_and_log_quality(session, page=wiki_page, body=page.body)
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
            detail=llm.failure_detail("curate", exc),
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
        await refresh_index(session, workspace_id=workspace.workspace_id)
    except Exception:
        logger.exception("failed to refresh overview.md/log.md/index.md for %s", source.source_id)

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


async def _write_structured_source_page(
    session: AsyncSession,
    *,
    source: RawSource,
    workspace: Workspace,
    content: curate.StructuredCuratedContent,
) -> None:
    """Finalize a `structured_data` source's page (07 §1.3) — same upsert-by-path shape as
    `_write_source_page`, different render and `description` source (the intent statement,
    not a prose summary)."""
    body = curate.render_structured_source_body(
        content,
        filename=source.filename,
        artifact_identity=source.artifact_identity,
        source_version=source.source_version,
    )
    await _upsert_singleton(
        session,
        workspace_id=workspace.workspace_id,
        path=f"sources/{source.source_id}.md",
        page_type=PageType.source,
        title=content.source_title,
        description=content.intent_statement,
        tags=["source", "structured_data"],
        body=body,
        status=PageStatus.published,
    )


async def _write_curated_page(
    session: AsyncSession,
    *,
    workspace_id: str,
    page: curate.CuratedPage,
    existing: list[curate.ExistingPage],
) -> WikiPage:
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
        return wiki_page

    return await versioning.create_page(
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


async def _score_and_log_quality(session: AsyncSession, *, page: WikiPage, body: str) -> None:
    """Content quality scoring (07 §4, phase3-tasklist.md step 69) — mechanical, computed
    from the body just written (`curate.score_content_quality`), never an LLM judgment.
    `page.quality_score` is the sortable Admin Console value; the full breakdown goes into
    a new `lint_log` entry — the first real writer of that stream (`models.LintLog`'s own
    docstring explains why it sat unbuilt since Phase 1). Scoped to concept/entity pages
    only (`curate_source`'s own loop, the only caller) — a source/overview/index/log page
    is provenance/bookkeeping, not the knowledge content these dimensions describe."""
    score = curate.score_content_quality(body)
    page.quality_score = score.combined
    session.add(
        LintLog(
            page_id=page.page_id,
            workspace_id=page.workspace_id,
            kind="quality_score",
            detail={
                "citation_density": score.citation_density,
                "cross_reference_completeness": score.cross_reference_completeness,
                "combined": score.combined,
            },
        )
    )
    await session.flush()


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
    """log.md merges `ingestion_log`, `admin_action_log`, and `lint_log` (02 §5, 09 §23,
    §74) — `lint_log` sat named-but-unbuilt since Phase 1 until content quality scoring
    (phase3-tasklist.md step 69) became its first real writer.

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

    for entry in await _recent_lint_entries(
        session, workspace_id=workspace_id, limit=curate.LOG_RECENT_LIMIT
    ):
        page = await session.get(WikiPage, entry.page_id)
        path = page.path if page is not None else "?"
        combined = entry.detail.get("combined") if isinstance(entry.detail, dict) else None
        rows.append((entry.created_at, f"Lint: `{path}` quality score {combined}"))

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


async def refresh_index(session: AsyncSession, *, workspace_id: str) -> None:
    """index.md: catalog of all pages, one-line summaries by category (01 §4,
    phase3-tasklist.md step 60) — real content now, replacing the never-materialized page
    `search.py`'s own tsvector weight-tier comment has flagged as an approximation since
    Phase 1. Same refresh points as `refresh_log` (curate_source, rollback, bulk-move) —
    any of those can change a page's title/description/workspace, all of which the catalog
    must reflect."""
    concepts = await _catalog_entries(session, PageType.concept, workspace_id=workspace_id)
    entities = await _catalog_entries(session, PageType.entity, workspace_id=workspace_id)
    sources = await _catalog_entries(session, PageType.source, workspace_id=workspace_id)
    comparisons = await _catalog_entries(session, PageType.comparison, workspace_id=workspace_id)
    body = curate.render_index_body(
        concepts=concepts, entities=entities, sources=sources, comparisons=comparisons
    )
    await _upsert_singleton(
        session,
        workspace_id=workspace_id,
        path="index.md",
        page_type=PageType.index,
        title=f"{workspace_id} Index",
        body=body,
    )


async def _catalog_entries(
    session: AsyncSession, page_type: PageType, *, workspace_id: str
) -> list[tuple[str, str, str]]:
    """(title, description, path) for every published page of `page_type`, alphabetical by
    title — index.md's own catalog order (01 §4: "organized by category"). Raw `->>` text
    extraction, matching `versioning.list_pages`/`search.search()`'s own established
    convention for reading `frontmatter` fields, rather than the ORM-level JSONB comparator
    no other query in this codebase uses."""
    stmt = text(
        "SELECT COALESCE(pv.frontmatter ->> 'title', '') AS title, "
        "       COALESCE(pv.frontmatter ->> 'description', '') AS description, "
        "       p.path AS path "
        "FROM wiki_page p "
        "JOIN page_version pv ON pv.version_id = p.current_version_id "
        "WHERE p.workspace_id = :workspace_id AND p.page_type = :page_type "
        "      AND p.status = :status "
        "ORDER BY title"
    )
    rows = await session.execute(
        stmt,
        {
            "workspace_id": workspace_id,
            "page_type": page_type.value,
            "status": PageStatus.published.value,
        },
    )
    return [(r.title, r.description, r.path) for r in rows]


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


async def _recent_lint_entries(session: AsyncSession, *, workspace_id: str, limit: int) -> list[LintLog]:
    result = await session.execute(
        select(LintLog)
        .where(LintLog.workspace_id == workspace_id)
        .order_by(LintLog.created_at.desc(), LintLog.entry_id.desc())
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


async def list_sources(
    session: AsyncSession,
    *,
    workspace_id: str,
    status: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> tuple[list[RawSource], str | None]:
    """05 §7's Raw Source Browser: every source in a workspace, newest-first, cursor-
    paginated per 09 §14's shared convention (`RawSource.created_at`, added
    phase2-tasklist.md step 43 specifically for this).

    Each row carries its own `supersedes` pointer; a client reconstructs a full chain by
    following that pointer through this same list rather than this function resolving and
    returning a pre-walked chain per row — no recursive query needed for what's explicitly
    scoped as a browse *view*, not a chain-resolution endpoint."""
    limit = min(limit, MAX_LIST_LIMIT)
    stmt = select(RawSource).where(RawSource.workspace_id == workspace_id)
    if status is not None:
        stmt = stmt.where(RawSource.status == RawSourceStatus(status))
    if cursor is not None:
        created_at, source_id = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(RawSource.created_at, RawSource.source_id) < tuple_(created_at, source_id)
        )
    stmt = stmt.order_by(RawSource.created_at.desc(), RawSource.source_id.desc()).limit(limit + 1)
    sources = list((await session.execute(stmt)).scalars())

    next_cursor = None
    if len(sources) > limit:
        sources = sources[:limit]
        last = sources[-1]
        next_cursor = encode_cursor(last.created_at, last.source_id)
    return sources, next_cursor
