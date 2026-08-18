"""Phase 2 step 30 — real Celery tasks wrapping classify_source/curate_source/reindex.

Nothing is dispatched automatically yet (that's step 32); these tests drive each task's
async body directly, against the real test database via the `task_db` fixture, the same
way a real worker would run it against production's.
"""

import uuid

from sqlalchemy import select

from karpwiki import tasks
from karpwiki.classify import ClassificationResult
from karpwiki.curate import CuratedContent, CuratedPage
from karpwiki.models import (
    IndexState,
    IndexStatus,
    IndexType,
    PageType,
    PipelineState,
    RawSource,
    WikiPage,
)

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}


def _classifies_as(label, confidence=0.9, summary="A doc about ops."):
    async def _call(**_kwargs):
        return ClassificationResult(summary=summary, document_type=label, confidence=confidence)

    return _call


def _curates_as(*, title, body):
    content = CuratedContent(
        source_title=title,
        source_description=f"About {title}.",
        source_summary=body,
        source_key_points=[body],
        pages=[CuratedPage(page_type="concept", title=title, tags=["ops", "shared"], body=body)],
    )

    async def _call(**_kwargs):
        return content

    return _call


def test_queue_routing_registers_all_three_tasks():
    """Step 30's exact scope: classification/curation/indexing each get a real task, routed
    to the queue `tasks.py` already defined (no new queue, no broker round trip needed)."""
    assert tasks.app.tasks["karpwiki.classification.classify_source"] is not None
    assert tasks.app.tasks["karpwiki.curation.curate_source"] is not None
    assert tasks.app.tasks["karpwiki.indexing.reindex"] is not None

    routes = tasks.app.conf.task_routes
    assert routes["karpwiki.classification.*"]["queue"] == "classification"
    assert routes["karpwiki.curation.*"]["queue"] == "curation"
    assert routes["karpwiki.indexing.*"]["queue"] == "indexing"


def test_maintenance_queue_registers_the_staleness_detector():
    """Step 36 — the maintenance queue's first real task."""
    assert tasks.app.tasks["karpwiki.maintenance.detect_staleness"] is not None
    routes = tasks.app.conf.task_routes
    assert routes["karpwiki.maintenance.*"]["queue"] == "maintenance"


def test_maintenance_queue_registers_the_superseded_source_detector():
    """Step 37."""
    assert tasks.app.tasks["karpwiki.maintenance.detect_superseded_sources"] is not None


def test_acks_late_is_set_so_a_crashed_worker_redelivers_the_task():
    """Step 33: a worker process dying mid-task (OOM, SIGKILL) must not silently lose the
    job — acks_late + reject_on_worker_lost means the broker redelivers it instead."""
    assert tasks.app.conf.task_acks_late is True
    assert tasks.app.conf.task_reject_on_worker_lost is True
    # Live-verified (09 §36): acks_late alone is close to a no-op on the Redis transport
    # without also tuning this down from its 3600s default — a killed-mid-task worker's job
    # only came back once this was set to something in the minutes range, not left default.
    assert tasks.app.conf.broker_transport_options["visibility_timeout"] == 600


async def test_classify_task_routes_a_source(client, session, workspace, task_db):
    submitted = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "runbook text"})
    source_id = uuid.UUID(submitted.json()["source_id"])
    await session.commit()

    await tasks._classify(source_id, call=_classifies_as("eng.runbook"))

    source = await session.get(RawSource, source_id)
    await session.refresh(source)
    assert source.pipeline_state is PipelineState.classified
    assert source.workspace_id == workspace.workspace_id


async def test_curate_task_dedups_then_curates(client, session, workspace, task_db):
    submitted = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "runbook text"})
    source_id = uuid.UUID(submitted.json()["source_id"])
    await session.commit()

    await tasks._classify(source_id, call=_classifies_as("eng.runbook", summary="A runbook."))
    await session.commit()

    await tasks._curate(
        source_id, call=_curates_as(title="Runbook A", body="Steps to run the thing.")
    )

    source = await session.get(RawSource, source_id)
    await session.refresh(source)
    assert source.pipeline_state is PipelineState.ingested

    pages = (
        await session.execute(
            select(WikiPage).where(
                WikiPage.workspace_id == workspace.workspace_id,
                WikiPage.page_type == PageType.concept,
            )
        )
    ).scalars().all()
    assert any(p.path == "concepts/runbook-a.md" for p in pages)


async def test_curate_task_stops_at_pending_review_on_duplicate(client, session, workspace, task_db):
    """A second submission with the same content hash blocks at dedup (03 §4) — the curate
    task must not run the Curator when check_duplicates parks the source for review."""
    text = "identical runbook text"
    first = await client.post("/sources", headers=CONTRIBUTOR, data={"text": text})
    first_id = uuid.UUID(first.json()["source_id"])
    second = await client.post("/sources", headers=CONTRIBUTOR, data={"text": text})
    second_id = uuid.UUID(second.json()["source_id"])
    await session.commit()

    await tasks._classify(first_id, call=_classifies_as("eng.runbook"))
    await tasks._classify(second_id, call=_classifies_as("eng.runbook"))
    await session.commit()

    await tasks._curate(first_id, call=_curates_as(title="Runbook B", body=text))
    await tasks._curate(second_id, call=_curates_as(title="Runbook B", body=text))

    second_source = await session.get(RawSource, second_id)
    await session.refresh(second_source)
    assert second_source.pipeline_state is PipelineState.pending_review


async def test_reindex_task_indexes_a_pending_page(client, session, workspace, task_db):
    submitted = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "runbook text"})
    source_id = uuid.UUID(submitted.json()["source_id"])
    await session.commit()

    await tasks._classify(source_id, call=_classifies_as("eng.runbook"))
    await session.commit()
    await tasks._curate(source_id, call=_curates_as(title="Runbook C", body="Do the steps."))
    await session.commit()

    page = (
        await session.execute(
            select(WikiPage).where(
                WikiPage.workspace_id == workspace.workspace_id,
                WikiPage.page_type == PageType.concept,
            )
        )
    ).scalars().first()
    status_before = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    assert status_before.state in (IndexState.pending, IndexState.stale)

    await tasks._reindex(page.page_id)

    status_after = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    await session.refresh(status_after)
    assert status_after.state is IndexState.indexed
    assert status_after.last_indexed_at is not None


async def test_detect_staleness_task_raises_a_review_item(session, workspace, task_db):
    from datetime import UTC, date, datetime, timedelta

    from karpwiki import versioning
    from karpwiki.models import PageStatus, PageType, PageVersion, ReviewItem, ReviewKind

    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path="concepts/old-page.md",
        page_type=PageType.concept,
        title="Old Page",
        description="An old page.",
        date=date(2026, 8, 17),
        tags=["a", "b"],
        body="Body.",
        author="system:curator",
        status=PageStatus.published,
    )
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    status.state = IndexState.stale
    version = await session.get(PageVersion, page.current_version_id)
    version.created_at = datetime.now(UTC) - timedelta(days=100)
    await session.commit()

    await tasks._detect_staleness(workspace.workspace_id)

    item = (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace.workspace_id, ReviewItem.kind == ReviewKind.reindex
            )
        )
    ).scalar_one()
    assert item.detail["page_count"] == 1


async def test_detect_superseded_sources_task_raises_a_review_item(session, workspace, task_db):
    import hashlib
    from datetime import UTC, datetime, timedelta

    from karpwiki import objectstore
    from karpwiki.models import RawSource, RawSourceStatus, ReviewItem, ReviewKind

    source_id = uuid.uuid4()
    key = f"/{workspace.workspace_id}/sources/{source_id}/old.md"
    payload = b"old content"
    objectstore.write_bytes(key, payload)
    session.add(
        RawSource(
            source_id=source_id,
            workspace_id=workspace.workspace_id,
            object_key=key,
            filename="old.md",
            content_hash=hashlib.sha256(payload).hexdigest(),
            submitted_by="user:deepak",
            status=RawSourceStatus.superseded,
            superseded_at=datetime.now(UTC) - timedelta(days=200),
        )
    )
    await session.commit()

    await tasks._detect_superseded_sources(workspace.workspace_id)

    item = (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace.workspace_id, ReviewItem.kind == ReviewKind.prune
            )
        )
    ).scalar_one()
    assert item.detail["source_count"] == 1
