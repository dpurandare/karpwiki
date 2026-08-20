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
    WorkspaceStatus,
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


def test_maintenance_queue_registers_the_existing_duplicate_detector():
    """Step 38."""
    assert tasks.app.tasks["karpwiki.maintenance.detect_existing_duplicates"] is not None


def test_maintenance_queue_registers_the_orphan_detector():
    """Step 39."""
    assert tasks.app.tasks["karpwiki.maintenance.detect_orphans"] is not None


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


async def test_detect_existing_duplicates_task_raises_a_review_item(session, workspace, task_db):
    from datetime import date

    from karpwiki import search, versioning
    from karpwiki.models import PageStatus, PageType, PageVersion, ReviewItem, ReviewKind

    body = (
        "The payments worker drains its queue before restart. Operators run a rollout "
        "restart and verify that consumer lag returns to zero within five minutes."
    )
    for title in ("Restarting Payments", "Payments Restart Runbook"):
        page = await versioning.create_page(
            session,
            workspace_id=workspace.workspace_id,
            path=f"concepts/{title.lower().replace(' ', '-')}.md",
            page_type=PageType.concept,
            title=title,
            description=f"About {title}.",
            date=date(2026, 8, 17),
            tags=["a", "b"],
            body=body,
            author="system:curator",
            status=PageStatus.published,
        )
        version = await session.get(PageVersion, page.current_version_id)
        await search.index_page(session, page=page, version=version)
    await session.commit()

    await tasks._detect_existing_duplicates(workspace.workspace_id)

    item = (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace.workspace_id, ReviewItem.kind == ReviewKind.duplicate
            )
        )
    ).scalar_one()
    assert item.detail["raised_by"] == "advisor"


async def test_detect_orphans_task_raises_a_review_item(session, workspace, task_db):
    from datetime import date

    from karpwiki import versioning
    from karpwiki.models import PageStatus, PageType, ReviewItem, ReviewKind

    await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path="concepts/forgotten-page.md",
        page_type=PageType.concept,
        title="Forgotten Page",
        description="About Forgotten Page.",
        date=date(2026, 8, 17),
        tags=["a", "b"],
        body="Nobody links to or searches for this page.",
        author="system:curator",
        status=PageStatus.published,
    )
    await session.commit()

    await tasks._detect_orphans(workspace.workspace_id)

    item = (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace.workspace_id, ReviewItem.kind == ReviewKind.prune
            )
        )
    ).scalar_one()
    assert item.detail["reason"] == "orphaned"
    assert item.detail["page_count"] == 1


def test_maintenance_queue_registers_the_contradiction_detector():
    """Step 40's task, added alongside step 41's scheduling (the gap noted in
    `tasks._detect_contradictions`'s own docstring)."""
    assert tasks.app.tasks["karpwiki.maintenance.detect_contradictions"] is not None


def test_maintenance_queue_registers_the_tiered_staleness_and_dispatch_tasks():
    """Step 41."""
    assert tasks.app.tasks["karpwiki.maintenance.detect_staleness_tiered"] is not None
    assert tasks.app.tasks["karpwiki.maintenance.dispatch_daily_detectors"] is not None
    assert tasks.app.tasks["karpwiki.maintenance.dispatch_contradiction_detector"] is not None


def test_beat_schedule_wires_both_dispatch_tasks():
    schedule = tasks.app.conf.beat_schedule
    assert schedule["maintenance-daily-detectors"]["task"] == "karpwiki.maintenance.dispatch_daily_detectors"
    assert (
        schedule["maintenance-contradiction-detector"]["task"]
        == "karpwiki.maintenance.dispatch_contradiction_detector"
    )
    # Contradiction's interval must be >= the daily detectors' — it spends a real LLM
    # call per candidate (step 40), so it should never run *more* often than the free ones.
    assert schedule["maintenance-contradiction-detector"]["schedule"] >= schedule["maintenance-daily-detectors"]["schedule"]


def test_maintenance_queue_registers_the_stuck_pipeline_detector():
    """Step 64."""
    assert tasks.app.tasks["karpwiki.maintenance.detect_stuck_pipelines"] is not None


def test_beat_schedule_wires_the_stuck_pipeline_detector():
    """Step 64 — its own entry, not folded into `maintenance-daily-detectors`: that
    dispatcher fans out per workspace, which doesn't fit this detector's global sweep."""
    schedule = tasks.app.conf.beat_schedule
    assert (
        schedule["maintenance-stuck-pipeline-detector"]["task"]
        == "karpwiki.maintenance.detect_stuck_pipelines"
    )


async def test_detect_contradictions_task_raises_a_review_item(session, workspace, task_db):
    from datetime import date

    from karpwiki import search, versioning
    from karpwiki.advisor import ContradictionJudgment
    from karpwiki.models import PageStatus, PageVersion, ReviewItem, ReviewKind

    # Same scoring shape as tests/test_advisor.py's CONTRADICTION_BODY_A/B (~0.5
    # containment against each other, tuned against the real full-text index).
    body_a = (
        "Restart the payments worker daily using the automated recovery script during "
        "scheduled maintenance windows to clear the queue backlog."
    )
    body_b = (
        "Restart the payments worker weekly using a manual failover checklist during "
        "unplanned incident response to clear the queue backlog."
    )
    for title, body in (("Daily Restart", body_a), ("Weekly Restart", body_b)):
        page = await versioning.create_page(
            session,
            workspace_id=workspace.workspace_id,
            path=f"concepts/{title.lower().replace(' ', '-')}.md",
            page_type=PageType.concept,
            title=title,
            description=f"About {title}.",
            date=date(2026, 8, 19),
            tags=["a", "b"],
            body=body,
            author="system:curator",
            status=PageStatus.published,
        )
        version = await session.get(PageVersion, page.current_version_id)
        await search.index_page(session, page=page, version=version)
    await session.commit()

    async def _fake_call(**_kwargs):
        return ContradictionJudgment(contradicts=True, outdated_page="a", explanation="Conflicting cadence.")

    await tasks._detect_contradictions(workspace.workspace_id, call=_fake_call)

    item = (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace.workspace_id, ReviewItem.kind == ReviewKind.prune
            )
        )
    ).scalar_one()
    assert item.detail["reason"] == "contradicted_by"


async def test_detect_stuck_pipelines_task_raises_a_workspace_less_review_item(
    session, workspace, task_db
):
    """Step 64 — global sweep, no `workspace_id` argument (unlike every task above): a
    `submitted`-stuck source has none of its own yet (03 §1)."""
    import hashlib
    from datetime import UTC, datetime, timedelta

    from karpwiki import objectstore
    from karpwiki.models import IngestionLog, ReviewItem, ReviewKind

    source_id = uuid.uuid4()
    key = f"/_inbox/{source_id}/lost.md"
    objectstore.write_bytes(key, b"content")
    source = RawSource(
        source_id=source_id,
        object_key=key,
        filename="lost.md",
        content_hash=hashlib.sha256(b"content").hexdigest(),
        submitted_by="user:deepak",
        pipeline_state=PipelineState.submitted,
    )
    session.add(source)
    await session.flush()
    session.add(
        IngestionLog(
            source_id=source_id,
            from_state=None,
            to_state=PipelineState.submitted,
            actor="test",
            created_at=datetime.now(UTC) - timedelta(hours=2),
        )
    )
    await session.commit()

    await tasks._detect_stuck_pipelines()

    item = (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id.is_(None), ReviewItem.kind == ReviewKind.stuck
            )
        )
    ).scalar_one()
    assert item.detail["sources"][0]["source_id"] == str(source_id)


async def test_detect_staleness_tiered_task_flags_a_high_traffic_page_at_the_short_bar(
    session, workspace, task_db
):
    from datetime import UTC, date, datetime, timedelta

    from karpwiki import versioning
    from karpwiki.models import PageStatus, PageVersion, QueryLog, ReviewItem, ReviewKind

    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path="concepts/popular-old-page.md",
        page_type=PageType.concept,
        title="Popular Old Page",
        description="A popular old page.",
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
    session.add(
        QueryLog(
            principal="user:deepak",
            query_text="popular old page",
            resolved_workspaces=[workspace.workspace_id],
            results=[{"page_id": str(page.page_id), "score": 0.9}],
        )
    )
    await session.commit()

    await tasks._detect_staleness_tiered(workspace.workspace_id)

    item = (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id == workspace.workspace_id, ReviewItem.kind == ReviewKind.reindex
            )
        )
    ).scalar_one()
    assert item.detail["page_count"] == 1


async def test_dispatch_daily_detectors_enqueues_each_detector_per_active_workspace(
    session, workspace, other_workspace, task_db, dispatched
):
    await session.commit()

    await tasks._dispatch_daily_detectors()

    for name in (
        "detect_staleness_tiered",
        "detect_superseded_sources",
        "detect_existing_duplicates",
        "detect_orphans",
    ):
        assert set(dispatched[name]) == {workspace.workspace_id, other_workspace.workspace_id}


async def test_dispatch_daily_detectors_skips_archived_workspaces(
    session, workspace, other_workspace, task_db, dispatched
):
    other_workspace.status = WorkspaceStatus.archived
    await session.commit()

    await tasks._dispatch_daily_detectors()

    assert dispatched["detect_staleness_tiered"] == [workspace.workspace_id]


async def test_dispatch_contradiction_detector_enqueues_per_active_workspace(
    session, workspace, other_workspace, task_db, dispatched
):
    await session.commit()

    await tasks._dispatch_contradiction_detector()

    assert set(dispatched["detect_contradictions"]) == {workspace.workspace_id, other_workspace.workspace_id}


# --- connector polling (phase2-tasklist.md step 52) -----------------------------------------


def test_connector_is_due_when_never_run():
    from datetime import UTC, datetime

    from karpwiki.models import Connector

    connector = Connector(schedule={"interval_minutes": 10}, last_run_at=None)
    assert tasks._connector_is_due(connector, now=datetime.now(UTC))


def test_connector_is_due_respects_interval():
    from datetime import UTC, datetime, timedelta

    from karpwiki.models import Connector

    now = datetime.now(UTC)
    connector = Connector(schedule={"interval_minutes": 10}, last_run_at=now - timedelta(minutes=5))
    assert not tasks._connector_is_due(connector, now=now)

    connector.last_run_at = now - timedelta(minutes=15)
    assert tasks._connector_is_due(connector, now=now)


def test_connector_is_due_false_with_no_interval_configured():
    from datetime import UTC, datetime

    from karpwiki.models import Connector

    connector = Connector(schedule={}, last_run_at=None)
    assert not tasks._connector_is_due(connector, now=datetime.now(UTC))


async def test_poll_connector_task_dispatches_classification_for_created_sources(
    session, workspace, task_db, dispatched, monkeypatch
):
    from karpwiki import connector_polling, connectors
    from karpwiki.connector_polling import DiscoveredItem

    class _StubAdapter:
        async def poll(self, connector, credential_ref):
            return [DiscoveredItem(filename="a.md", content=b"A")], {"sha": "abc"}

    monkeypatch.setitem(connector_polling.ADAPTERS, "stub", _StubAdapter())
    connector = await connectors.create(session, workspace_id=workspace.workspace_id, type="stub")
    await session.commit()

    await tasks._poll_connector(connector.connector_id)

    assert len(dispatched["classify_source"]) == 1


async def test_dispatch_connector_polls_enqueues_due_connectors_only(
    session, workspace, other_workspace, task_db, dispatched
):
    from datetime import UTC, datetime, timedelta

    from karpwiki import connectors

    due = await connectors.create(
        session, workspace_id=workspace.workspace_id, type="stub", schedule={"interval_minutes": 10}
    )
    not_due = await connectors.create(
        session, workspace_id=other_workspace.workspace_id, type="stub", schedule={"interval_minutes": 10}
    )
    not_due.last_run_at = datetime.now(UTC) - timedelta(minutes=1)
    unconfigured = await connectors.create(
        session, workspace_id=workspace.workspace_id, type="stub"
    )  # no interval_minutes at all
    await session.commit()

    await tasks._dispatch_connector_polls()

    assert dispatched["poll_connector"] == [str(due.connector_id)]
    assert str(not_due.connector_id) not in dispatched["poll_connector"]
    assert str(unconfigured.connector_id) not in dispatched["poll_connector"]


async def test_dispatch_connector_polls_skips_disabled_connectors(session, workspace, task_db, dispatched):
    from karpwiki import connectors
    from karpwiki.models import ConnectorState

    connector = await connectors.create(
        session, workspace_id=workspace.workspace_id, type="stub", schedule={"interval_minutes": 10}
    )
    connector.state = ConnectorState.disabled
    await session.commit()

    await tasks._dispatch_connector_polls()

    assert dispatched["poll_connector"] == []


# --- Real Notification Service delivery (phase3-tasklist.md step 67) ---------------------


class _FakeSink:
    def __init__(self):
        self.calls = []

    async def notify_review_sla_breach(self, **kwargs):
        self.calls.append(("review_sla_breach", kwargs))

    async def notify_search_latency_sla_breach(self, **kwargs):
        self.calls.append(("search_latency_sla_breach", kwargs))

    async def notify_source_ingested(self, **kwargs):
        self.calls.append(("source_ingested", kwargs))

    async def notify_source_rejected(self, **kwargs):
        self.calls.append(("source_rejected", kwargs))

    async def notify_source_merged(self, **kwargs):
        self.calls.append(("source_merged", kwargs))

    async def notify_connector_auth_failure(self, connector, message):
        self.calls.append(("connector_auth_failure", {"message": message}))


def test_maintenance_queue_registers_the_sla_breach_notifier():
    assert tasks.app.tasks["karpwiki.maintenance.notify_sla_breaches"] is not None


def test_beat_schedule_wires_the_sla_breach_notifier():
    schedule = tasks.app.conf.beat_schedule
    assert (
        schedule["notification-sla-sweep"]["task"] == "karpwiki.maintenance.notify_sla_breaches"
    )


async def test_notify_sla_breaches_task_fires_a_review_sla_breach(session, workspace, task_db):
    from datetime import UTC, datetime, timedelta

    from karpwiki import review
    from karpwiki.models import ReviewKind

    item = await review.create(
        session, kind=ReviewKind.duplicate, subject_ref="x", workspace_id=workspace.workspace_id
    )
    item.created_at = datetime.now(UTC) - timedelta(hours=10)
    await session.commit()

    sink = _FakeSink()
    await tasks._notify_sla_breaches(notification_sink=sink)

    [call] = [c for c in sink.calls if c[0] == "review_sla_breach"]
    _, kwargs = call
    assert kwargs["workspace_id"] == workspace.workspace_id
    assert kwargs["kind"] == "duplicate"
    assert kwargs["count"] == 1
    assert kwargs["oldest_age_hours"] >= 10


async def test_notify_sla_breaches_task_fires_nothing_when_nothing_breaches(session, workspace, task_db):
    sink = _FakeSink()
    await tasks._notify_sla_breaches(notification_sink=sink)
    assert sink.calls == []


def test_maintenance_queue_registers_the_storage_snapshot_recorder():
    assert tasks.app.tasks["karpwiki.maintenance.record_storage_snapshots"] is not None


def test_beat_schedule_wires_the_storage_snapshot_recorder():
    schedule = tasks.app.conf.beat_schedule
    assert (
        schedule["storage-snapshot-recording"]["task"]
        == "karpwiki.maintenance.record_storage_snapshots"
    )


async def test_record_storage_snapshots_task_records_one_row_per_active_workspace(
    session, workspace, other_workspace, task_db
):
    from sqlalchemy import select

    from karpwiki.models import StorageSnapshot

    await session.commit()

    await tasks._record_storage_snapshots()

    rows = (await session.execute(select(StorageSnapshot))).scalars().all()
    assert {r.workspace_id for r in rows} == {workspace.workspace_id, other_workspace.workspace_id}


async def test_record_storage_snapshots_task_purges_expired_rows(session, workspace, task_db):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from karpwiki import monitoring
    from karpwiki.models import StorageSnapshot

    await session.commit()

    stale = await monitoring.record_storage_snapshot(session, workspace_id=workspace.workspace_id)
    stale.created_at = datetime.now(UTC) - timedelta(days=200)
    await session.commit()

    await tasks._record_storage_snapshots()

    rows = (await session.execute(select(StorageSnapshot))).scalars().all()
    assert stale.snapshot_id not in {r.snapshot_id for r in rows}


async def test_curate_task_notifies_the_submitter_on_ingested(session, workspace, task_db):
    from karpwiki import objectstore

    source_id = uuid.uuid4()
    key = f"/_inbox/{source_id}/f.md"
    objectstore.write_bytes(key, b"runbook text")
    source = RawSource(
        source_id=source_id,
        object_key=key,
        filename="f.md",
        content_hash="deadbeefcafe",
        submitted_by="user:deepak",
        pipeline_state=PipelineState.submitted,
    )
    session.add(source)
    await session.flush()
    await session.commit()

    async def _classify_call(**_kwargs):
        return ClassificationResult(summary="A runbook.", document_type="eng.runbook", confidence=0.9)

    await tasks._classify(source_id, call=_classify_call)
    await session.commit()

    content = CuratedContent(
        source_title="Runbook D",
        source_description="About Runbook D.",
        source_summary="Steps.",
        source_key_points=["Steps."],
        pages=[CuratedPage(page_type="concept", title="Runbook D", tags=["a", "b"], body="Steps.")],
    )

    async def _curate_call(**_kwargs):
        return content

    sink = _FakeSink()
    await tasks._curate(source_id, call=_curate_call, notification_sink=sink)

    [call] = [c for c in sink.calls if c[0] == "source_ingested"]
    _, kwargs = call
    assert kwargs["submitted_by"] == "user:deepak"
    assert kwargs["filename"] == "f.md"
    assert kwargs["source_id"] == str(source_id)
