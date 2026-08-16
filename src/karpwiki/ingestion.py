"""Ingestion orchestration (03 §3, §4, §6, phase1-tasklist steps 9-12).

Performs the decisions in `classify.py`, `dedup.py`, and `curate.py` against the database
and object store: runs the pipeline transitions, relocates the stored object once a
workspace is known, and parks the source for review when a gate refuses.
"""

import logging
from datetime import date
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import classify, curate, dedup, llm, objectstore, pipeline, versioning
from .models import (
    PageStatus,
    PageType,
    PageVersion,
    PipelineState,
    RawSource,
    VersionTrigger,
    WikiPage,
    Workspace,
)

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
        await _refresh_log(session, workspace_id=workspace.workspace_id)
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
    body = curate.render_source_body(content, filename=source.filename)
    await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"sources/{source.source_id}.md",
        page_type=PageType.source,
        title=content.source_title,
        description=content.source_description,
        date=date.today(),
        tags=["source", str(source.content_shape.value if source.content_shape else "narrative")],
        body=body,
        author="system:curator",
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


async def _refresh_log(session: AsyncSession, *, workspace_id: str) -> None:
    entries = await pipeline.recent_ingested(
        session, workspace_id=workspace_id, limit=curate.LOG_RECENT_LIMIT
    )
    rows = []
    for entry in entries:
        source = await session.get(RawSource, entry.source_id)
        pages_touched = entry.detail.get("pages_touched") if isinstance(entry.detail, dict) else None
        rows.append((entry.created_at, source.filename if source else "?", pages_touched or 0))
    body = curate.render_log_body(rows)
    await _upsert_singleton(
        session,
        workspace_id=workspace_id,
        path="log.md",
        page_type=PageType.log,
        title=f"{workspace_id} Log",
        body=body,
    )


async def _upsert_singleton(
    session: AsyncSession, *, workspace_id: str, path: str, page_type: PageType, title: str, body: str
) -> None:
    result = await session.execute(
        select(WikiPage).where(WikiPage.workspace_id == workspace_id, WikiPage.path == path)
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        await versioning.create_page(
            session,
            workspace_id=workspace_id,
            path=path,
            page_type=page_type,
            title=title,
            description=f"Workspace {page_type.value} page.",
            date=date.today(),
            tags=[page_type.value, "workspace"],
            body=body,
            author="system:curator",
            status=PageStatus.published,
        )
        return

    await versioning.write_version(
        session,
        page=existing,
        body=body,
        author="system:curator",
        trigger=VersionTrigger.ingest,
        change_summary=f"Regenerated {path}.",
    )


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
