"""Curator ingest orchestration (03 §6) — database and object store, no network."""

import hashlib
import uuid
from datetime import date

import pytest
from sqlalchemy import select

from karpwiki import curate, ingestion, objectstore, pipeline, versioning
from karpwiki.curate import CuratedContent, CuratedPage
from karpwiki.models import (
    ContentShape,
    PageStatus,
    PageType,
    PageVersion,
    PipelineState,
    RawSource,
    WikiPage,
)

BODY = (
    b"# Restarting the payments worker\n\nDrain the queue, then roll the deployment. "
    b"Verify consumer lag returns to zero."
)


async def _classified(session, workspace, *, filename="restart-payments.md", payload=BODY):
    source_id = uuid.uuid4()
    key = f"/{workspace.workspace_id}/sources/{source_id}/{filename}"
    objectstore.write_bytes(key, payload)
    source = RawSource(
        source_id=source_id,
        workspace_id=workspace.workspace_id,
        object_key=key,
        filename=filename,
        content_hash=hashlib.sha256(payload).hexdigest(),
        content_shape=ContentShape.narrative,
        submitted_by="user:deepak",
        pipeline_state=PipelineState.ingesting,
    )
    session.add(source)
    await session.flush()
    return source


def _content(pages=(), title="Restarting Payments", description="Restart runbook."):
    return CuratedContent(
        source_title=title,
        source_description=description,
        source_summary="Drain the queue before restarting the worker.",
        source_key_points=["Drain the queue.", "Verify lag returns to zero."],
        pages=list(pages),
    )


def _returns(content: CuratedContent):
    async def _call(**_kwargs):
        return content

    return _call


def _raises(exc):
    async def _call(**_kwargs):
        raise exc

    return _call


async def test_curation_writes_a_published_source_page(session, workspace):
    source = await _classified(session, workspace)
    state = await ingestion.curate_source(
        session, source=source, workspace=workspace, call=_returns(_content())
    )
    await session.commit()

    assert state is PipelineState.ingested
    assert source.pipeline_state is PipelineState.ingested

    result = await session.execute(
        select(WikiPage).where(WikiPage.page_type == PageType.source)
    )
    page = result.scalar_one()
    assert page.status is PageStatus.published
    version = await session.get(PageVersion, page.current_version_id)
    assert version.frontmatter["title"] == "Restarting Payments"
    assert "[^1]: restart-payments.md" in version.content


async def test_proposed_pages_are_created_when_no_match_exists(session, workspace):
    source = await _classified(session, workspace)
    content = _content(
        pages=[
            CuratedPage(
                page_type="concept", title="Backoff", tags=["reliability", "ops"], body="What backoff is."
            )
        ]
    )
    await ingestion.curate_source(session, source=source, workspace=workspace, call=_returns(content))
    await session.commit()

    result = await session.execute(
        select(WikiPage).where(WikiPage.page_type == PageType.concept)
    )
    page = result.scalar_one()
    assert page.path == "concepts/backoff.md"
    assert page.status is PageStatus.published


async def test_entity_pages_land_under_entities_not_entitys(session, workspace):
    source = await _classified(session, workspace)
    content = _content(
        pages=[CuratedPage(page_type="entity", title="Payments Worker", tags=["x", "y"], body="b")]
    )
    await ingestion.curate_source(session, source=source, workspace=workspace, call=_returns(content))
    result = await session.execute(select(WikiPage).where(WikiPage.page_type == PageType.entity))
    assert result.scalar_one().path == "entities/payments-worker.md"


async def test_a_page_matching_an_existing_title_is_updated_not_duplicated(session, workspace):
    existing = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path="concepts/backoff.md",
        page_type=PageType.concept,
        title="Backoff",
        description="d",
        date=date(2026, 1, 1),
        tags=["a", "b"],
        body="Old content.",
        author="system:curator",
        status=PageStatus.published,
    )
    original_version = existing.current_version_id
    source = await _classified(session, workspace)

    content = _content(
        pages=[
            CuratedPage(page_type="concept", title="Backoff", tags=["a", "b"], body="New content.")
        ]
    )
    await ingestion.curate_source(session, source=source, workspace=workspace, call=_returns(content))
    await session.commit()

    result = await session.execute(
        select(WikiPage).where(WikiPage.page_type == PageType.concept)
    )
    pages = result.scalars().all()
    assert len(pages) == 1  # updated, not duplicated
    assert pages[0].current_version_id != original_version
    new_version = await session.get(PageVersion, pages[0].current_version_id)
    assert "New content." in new_version.content


async def test_the_existing_catalog_is_passed_to_the_call(session, workspace):
    """The curator can only avoid duplicating a page if it's told what already exists."""
    await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path="concepts/backoff.md",
        page_type=PageType.concept,
        title="Backoff",
        description="d",
        date=date(2026, 1, 1),
        tags=["a", "b"],
        body="x",
        author="system:curator",
        status=PageStatus.published,
    )
    source = await _classified(session, workspace)

    seen = {}

    async def _call(**kwargs):
        seen.update(kwargs)
        return _content()

    await ingestion.curate_source(session, source=source, workspace=workspace, call=_call)
    assert seen["existing_titles"] == ["Backoff"]


async def test_overview_reflects_current_counts_and_the_new_source(session, workspace):
    source = await _classified(session, workspace)
    await ingestion.curate_source(session, source=source, workspace=workspace, call=_returns(_content()))
    await session.commit()

    result = await session.execute(
        select(WikiPage)
        .where(WikiPage.workspace_id == workspace.workspace_id, WikiPage.path == "overview.md")
    )
    overview = result.scalar_one()
    version = await session.get(PageVersion, overview.current_version_id)
    assert "Sources ingested: 1" in version.content
    assert "Restarting Payments" in version.content


async def test_overview_is_regenerated_not_appended(session, workspace):
    """Running curation twice must not double the recent-updates list — each refresh is
    computed fresh from the database, not from the prior overview.md text."""
    for i in range(2):
        source = await _classified(session, workspace, filename=f"doc-{i}.md", payload=f"body {i}".encode())
        await ingestion.curate_source(
            session, source=source, workspace=workspace, call=_returns(_content(title=f"Doc {i}"))
        )
    await session.commit()

    result = await session.execute(
        select(WikiPage)
        .where(WikiPage.workspace_id == workspace.workspace_id, WikiPage.path == "overview.md")
    )
    overview = result.scalar_one()
    version = await session.get(PageVersion, overview.current_version_id)
    assert "Sources ingested: 2" in version.content
    assert version.content.count("Sources ingested:") == 1


async def test_log_reflects_the_ingest_that_triggered_it(session, workspace):
    """log.md materializes ingestion_log, and the ingested transition is what creates the
    entry it needs to show — a naive ordering would refresh the log one ingest too early."""
    source = await _classified(session, workspace)
    await ingestion.curate_source(session, source=source, workspace=workspace, call=_returns(_content()))
    await session.commit()

    result = await session.execute(
        select(WikiPage)
        .where(WikiPage.workspace_id == workspace.workspace_id, WikiPage.path == "log.md")
    )
    log_page = result.scalar_one()
    version = await session.get(PageVersion, log_page.current_version_id)
    assert "restart-payments.md" in version.content
    assert "1 page(s) touched" in version.content


async def test_pages_touched_counts_source_plus_every_curated_page(session, workspace):
    source = await _classified(session, workspace)
    content = _content(
        pages=[
            CuratedPage(page_type="concept", title="A", tags=["x", "y"], body="a"),
            CuratedPage(page_type="entity", title="B", tags=["x", "y"], body="b"),
        ]
    )
    await ingestion.curate_source(session, source=source, workspace=workspace, call=_returns(content))
    await session.commit()

    last = (await pipeline.history(session, source.source_id))[-1]
    assert last.detail["pages_touched"] == 3


async def test_a_curator_failure_lands_on_error(session, workspace):
    source = await _classified(session, workspace)
    state = await ingestion.curate_source(
        session, source=source, workspace=workspace, call=_raises(TimeoutError("upstream"))
    )
    await session.commit()

    assert state is PipelineState.error
    last = (await pipeline.history(session, source.source_id))[-1]
    assert last.detail == {"step": "curate", "error": "TimeoutError"}


async def test_a_failure_after_ingested_does_not_retry_the_transition(session, workspace, monkeypatch):
    """ingested has no outgoing edge; a bookkeeping failure after that point must not try
    to move to error, or it would raise IllegalTransition and mask the real ingest result."""

    async def _blow_up(*_args, **_kwargs):
        raise RuntimeError("overview refresh exploded")

    monkeypatch.setattr(ingestion, "_refresh_overview", _blow_up)

    source = await _classified(session, workspace)
    state = await ingestion.curate_source(
        session, source=source, workspace=workspace, call=_returns(_content())
    )
    await session.commit()

    assert state is PipelineState.ingested
    assert source.pipeline_state is PipelineState.ingested
