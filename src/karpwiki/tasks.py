"""Async layer — Celery app with one queue per job type (01 §1, 06 §4, 08 §2).

The pools are separated here because they scale on different signals: classification
and curation are LLM-bound, indexing and the maintenance advisor are compute-bound.

Phase 2 step 30 fills the classification/curation/indexing queues by wrapping the pure
orchestration functions `ingestion.classify_source`/`curate_source` and `search.reindex`
(09 §21's deliberately deferred gap). Step 32 wires the dispatch: `api.py` enqueues
`classify_source` on submission and `curate_source`/`reindex` after an admin resolution or
a page write it drove directly (rollback, bulk-move); the chained stages below —
classification's acceptance enqueuing curation, curation's page writes enqueuing reindex —
dispatch themselves, once their own transaction has committed.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from celery import Celery
from sqlalchemy import select

from . import advisor, connector_polling, ingestion, pipeline, search
from . import connectors_git  # noqa: F401 — registers "git" into connector_polling.ADAPTERS (step 54)
from .config import (
    CELERY_BROKER_URL,
    CELERY_VISIBILITY_TIMEOUT_SECONDS,
    CONNECTOR_DISPATCH_INTERVAL_MINUTES,
    MAINTENANCE_CONTRADICTION_INTERVAL_HOURS,
    MAINTENANCE_INTERVAL_HOURS,
)
from .db import engine, session_scope
from .models import Connector, ConnectorState, PipelineState, RawSource, Workspace, WorkspaceStatus

logger = logging.getLogger(__name__)

QUEUES = ("classification", "curation", "indexing", "maintenance", "connector_polling")

app = Celery("karpwiki", broker=CELERY_BROKER_URL)
app.conf.task_default_queue = "curation"
app.conf.task_routes = {
    "karpwiki.classification.*": {"queue": "classification"},
    "karpwiki.curation.*": {"queue": "curation"},
    "karpwiki.indexing.*": {"queue": "indexing"},
    "karpwiki.maintenance.*": {"queue": "maintenance"},
    "karpwiki.connector_polling.*": {"queue": "connector_polling"},
}
# Step 33's other half of "retried inside the worker" (03 §1): a worker process that dies
# mid-task (OOM, SIGKILL, container restart) must not silently lose the job — acks_late
# means the broker only drops a task once it's actually finished, so a crash redelivers it
# to another worker instead. No blanket `autoretry_for` on top of this: an ordinary
# exception (an `IllegalTransition`, an `InvalidResolutionError`) is a real bug or a race,
# not a transient failure, and retrying it would just fail identically every time — see 09
# §36 for why the transition table's own guard, not a broker-level retry, is what makes a
# redelivered/duplicate execution safe rather than corrupting.
app.conf.task_acks_late = True
app.conf.task_reject_on_worker_lost = True
# acks_late is close to a no-op without this: Celery's Redis transport doesn't notice a
# dropped consumer and requeue immediately (unlike RabbitMQ) — it tracks unacked messages
# and only restores one to the queue after `visibility_timeout` elapses, which defaults to
# **3600 seconds**. Found live (killed a worker mid-task, restarted it, and the task simply
# never came back at the default) — 09 §36 has the full story. 600s comfortably covers the
# slowest real path today (curate_source: several sequential LLM-touched page writes) with
# margin, while keeping a genuine crash's recovery bounded to minutes, not up to an hour.
app.conf.broker_transport_options = {"visibility_timeout": CELERY_VISIBILITY_TIMEOUT_SECONDS}

# Phase 2 step 41 (05 §2's "scheduling philosophy") — the two dispatcher tasks defined
# below enumerate active workspaces at fire time (beat's own static schedule can't know
# about a workspace created after the process started) and re-enqueue each detector's
# existing per-workspace task. Contradiction Detection gets its own, less frequent
# interval since it spends a real LLM call per candidate (step 40); the other four cost
# nothing beyond a few DB queries, so they share the faster one. Both intervals are
# env-overridable (`config.py`) — deployment-wide operational tuning, not per-workspace
# content thresholds, which stay Python defaults elsewhere in `advisor.py`.
app.conf.beat_schedule = {
    "maintenance-daily-detectors": {
        "task": "karpwiki.maintenance.dispatch_daily_detectors",
        "schedule": MAINTENANCE_INTERVAL_HOURS * 3600,
    },
    "maintenance-contradiction-detector": {
        "task": "karpwiki.maintenance.dispatch_contradiction_detector",
        "schedule": MAINTENANCE_CONTRADICTION_INTERVAL_HOURS * 3600,
    },
    # Phase 2 step 52 (09 §4) — same "enumerate at fire time" shape as the two detector
    # dispatchers above, just checking each *enabled* connector's own `schedule.
    # interval_minutes` against `last_run_at` rather than a single deployment-wide cadence.
    "connector-polling-dispatch": {
        "task": "karpwiki.connector_polling.dispatch_connector_polls",
        "schedule": CONNECTOR_DISPATCH_INTERVAL_MINUTES * 60,
    },
}


@app.task(name="karpwiki.curation.ping")
def ping() -> str:
    """Smoke-test task — proves the broker round-trips before 1b adds real work."""
    return "pong"


async def _classify(
    source_id: uuid.UUID, *, call: ingestion.ClassifierCall = ingestion.call_model
) -> None:
    # `call` is a test-only seam (never passed by the `@app.task` wrapper below, so
    # production always takes the real model) — same injectable-call pattern `ingestion.py`
    # already uses throughout, one layer up.
    async with session_scope() as session:
        source = await session.get(RawSource, source_id)
        if source is None:
            logger.warning("classify task: no source %s", source_id)
            return
        state = await ingestion.classify_source(session, source=source, call=call)
    # Dispatched only after the `async with` block above commits (`session_scope`'s
    # __aexit__) — the curation task opens its own session and must see this source as
    # `classified`, not whatever it was mid-transaction.
    if state is PipelineState.classified:
        curate_source.delay(str(source_id))


async def _classification_summary(session, source_id: uuid.UUID) -> str:
    """The Classifier's summary (03 §4's near-match query text), read back off
    `ingestion_log` rather than threading a new return value through `classify_source` —
    same pattern `ingestion._duplicate_evidence` already uses for duplicate detail.
    Empty when classification was admin-assigned rather than run by the model (no
    classifier call, so nothing to read back)."""
    for entry in reversed(await pipeline.history(session, source_id)):
        if entry.to_state is PipelineState.classified:
            return entry.detail.get("summary", "")
    return ""


async def _curate(
    source_id: uuid.UUID, *, call: ingestion.CuratorCall = ingestion.call_curator_model
) -> None:
    """09 §21/§33/step 32: acceptance runs dedup, then curate only if dedup clears — one
    task, since both are cheap/synchronous steps of the same "a classified source becomes
    pages" job and `tasks.py` has no dedicated dedup queue to dispatch a second hop into.

    Entered at `classified` (the normal path, from `_classify`'s dispatch above) or at
    `ingesting` directly — an admin already resolved dedup themselves (`resolve_duplicate`'s
    `keep_both`/`supersede`, 09 §22) before dispatching here, so re-running
    `check_duplicates` would both re-litigate that decision and hit an illegal
    `ingesting -> duplicate_check` transition (03 §1's edges don't allow it back out of
    `ingesting`) — curate runs directly instead.

    `call` is the same test-only seam as `_classify`'s."""
    page_ids: list[uuid.UUID] = []
    async with session_scope() as session:
        source = await session.get(RawSource, source_id)
        if source is None:
            logger.warning("curate task: no source %s", source_id)
            return
        if source.pipeline_state is PipelineState.classified:
            summary = await _classification_summary(session, source_id)
            state = await ingestion.check_duplicates(session, source=source, summary=summary)
        elif source.pipeline_state is PipelineState.ingesting:
            state = PipelineState.ingesting
        else:
            logger.warning(
                "curate task: source %s in unexpected state %s",
                source_id,
                source.pipeline_state,
            )
            return
        if state is PipelineState.ingesting:
            workspace = await session.get(Workspace, source.workspace_id)
            await ingestion.curate_source(session, source=source, workspace=workspace, call=call)
            # 02 §7's "a page write enqueues reindex," scoped to this workspace rather than
            # the exact pages just written — cheap, and every id returned here is
            # currently pending/stale within this same transaction, so `reindex` (which
            # requires that state) has something real to do for each one.
            page_ids = await search.pending_pages(session, workspace_id=source.workspace_id)
    for page_id in page_ids:
        reindex.delay(str(page_id))


async def _reindex(page_id: uuid.UUID) -> None:
    async with session_scope() as session:
        await search.reindex(session, page_id)


async def _detect_staleness(workspace_id: str) -> None:
    """Phase 2 step 36 — the maintenance queue's first real task. Nothing schedules this
    yet (step 41's Celery beat); dispatched manually per workspace until then."""
    async with session_scope() as session:
        await advisor.run_staleness_detector(session, workspace_id=workspace_id)


async def _detect_superseded_sources(workspace_id: str) -> None:
    """Phase 2 step 37 — same manual-dispatch position as `_detect_staleness` above."""
    async with session_scope() as session:
        await advisor.run_superseded_source_detector(session, workspace_id=workspace_id)


async def _detect_existing_duplicates(workspace_id: str) -> None:
    """Phase 2 step 38 — same manual-dispatch position as the two detectors above."""
    async with session_scope() as session:
        await advisor.run_existing_content_duplicate_detector(session, workspace_id=workspace_id)


async def _detect_orphans(workspace_id: str) -> None:
    """Phase 2 step 39 — same manual-dispatch position as the three detectors above."""
    async with session_scope() as session:
        await advisor.run_orphan_detector(session, workspace_id=workspace_id)


async def _detect_contradictions(
    workspace_id: str, *, call: advisor.ContradictionCheckCall | None = None
) -> None:
    """Phase 2 step 40 — same manual-dispatch position as the four detectors above. Added
    alongside step 41's scheduling rather than with step 40 itself, since step 40's own
    scope was the detector/resolution logic in `advisor.py`, not its task wrapper — a gap
    caught while wiring up the beat dispatcher below, which needs every detector to have
    one. `call` is the same test-only seam `_classify`/`_curate` already use — never
    passed by the real `@app.task` wrapper below, so production always takes the real
    model; unlike the other four detectors' tasks, this one needs the seam because
    step 40's detection itself spends a real LLM call, not just its resolution."""
    async with session_scope() as session:
        await advisor.run_contradiction_detector(session, workspace_id=workspace_id, call=call)


async def _detect_staleness_tiered(workspace_id: str) -> None:
    """Phase 2 step 41 — the popularity-tiered variant of `_detect_staleness` above (05 §2),
    used only by the beat-scheduled dispatcher below. A separate task rather than a
    `tiered` kwarg on `detect_staleness` itself, matching this module's existing
    one-task-per-detector shape, and leaving manual/test dispatch of `detect_staleness`
    exactly as it behaved before this step."""
    async with session_scope() as session:
        await advisor.run_staleness_detector(session, workspace_id=workspace_id, tiered=True)


async def _active_workspace_ids(session) -> list[str]:
    return (
        (
            await session.execute(
                select(Workspace.workspace_id).where(Workspace.status == WorkspaceStatus.active)
            )
        )
        .scalars()
        .all()
    )


async def _dispatch_daily_detectors() -> None:
    """Fired by `KARPWIKI_MAINTENANCE_INTERVAL_HOURS` (default 24h, `config.py`) — the four
    detectors with no LLM cost at detection time. Enumerates active workspaces at fire
    time (see the `beat_schedule` comment above for why) and re-enqueues each one's
    existing per-workspace task, so a large workspace count fans out across
    `worker-maintenance` replicas rather than being processed serially inside this task."""
    async with session_scope() as session:
        workspace_ids = await _active_workspace_ids(session)
    for workspace_id in workspace_ids:
        detect_staleness_tiered.delay(workspace_id)
        detect_superseded_sources.delay(workspace_id)
        detect_existing_duplicates.delay(workspace_id)
        detect_orphans.delay(workspace_id)


async def _dispatch_contradiction_detector() -> None:
    """Fired by `KARPWIKI_MAINTENANCE_CONTRADICTION_INTERVAL_HOURS` (default 168h/weekly,
    `config.py`) — separated from the four above since this one spends a real LLM call per
    candidate (step 40)."""
    async with session_scope() as session:
        workspace_ids = await _active_workspace_ids(session)
    for workspace_id in workspace_ids:
        detect_contradictions.delay(workspace_id)


async def _poll_connector(connector_id: uuid.UUID) -> None:
    """Phase 2 step 52 (09 §4). Dispatches classification for whatever `raw_source`s this
    run created, after the transaction that created them has committed — same "dispatch
    only after commit" discipline as `_classify`/`_curate` above."""
    async with session_scope() as session:
        connector = await session.get(Connector, connector_id)
        if connector is None:
            logger.warning("poll_connector task: no connector %s", connector_id)
            return
        source_ids = await connector_polling.poll_connector(session, connector=connector)
    for source_id in source_ids:
        classify_source.delay(str(source_id))


def _connector_is_due(connector: Connector, *, now: datetime) -> bool:
    """A connector with no `interval_minutes` in its `schedule` is webhook-only or simply
    unconfigured (09 §4: polling is the default, webhook is additive) — never dispatched
    automatically. `last_run_at is None` means "never run," always due."""
    interval_minutes = connector.schedule.get("interval_minutes")
    if not isinstance(interval_minutes, (int, float)) or interval_minutes <= 0:
        return False
    if connector.last_run_at is None:
        return True
    return now - connector.last_run_at >= timedelta(minutes=interval_minutes)


async def _dispatch_connector_polls() -> None:
    """Fired by `KARPWIKI_CONNECTOR_DISPATCH_INTERVAL_MINUTES` (default 5m, `config.py`).
    Enumerates *enabled* connectors at fire time (same reasoning as the two detector
    dispatchers above — a connector created after beat started must still get picked up)
    and re-enqueues `poll_connector` for whichever are due per their own `schedule`.
    `disabled`/`disabled_auth` connectors are never dispatched — an admin re-enabling one
    (09 §13) is what puts it back in this set."""
    now = datetime.now(UTC)
    async with session_scope() as session:
        connectors = (
            (
                await session.execute(
                    select(Connector).where(Connector.state == ConnectorState.enabled)
                )
            )
            .scalars()
            .all()
        )
    for connector in connectors:
        if _connector_is_due(connector, now=now):
            poll_connector.delay(str(connector.connector_id))


async def _run_and_release(coro) -> None:
    """`db.engine`'s connection pool is a process-level singleton, but each `@app.task`
    below gets its own `asyncio.run()` — a fresh event loop per call — and an asyncpg
    connection can't outlive the loop it was opened on (09 §21's OpenSearch-client lesson,
    same failure mode: a live dispatch through a real worker hit `RuntimeError: ... attached
    to a different loop` on a second task in the same worker process). Disposing the pool at
    the end of every call, not just after fork, means the next task always finds it empty
    and reconnects fresh under its own loop — see 09 §34 for the full story and the
    persistent-loop alternative it costs against."""
    try:
        await coro
    finally:
        await engine.dispose()


@app.task(name="karpwiki.classification.classify_source")
def classify_source(source_id: str) -> None:
    asyncio.run(_run_and_release(_classify(uuid.UUID(source_id))))


@app.task(name="karpwiki.curation.curate_source")
def curate_source(source_id: str) -> None:
    asyncio.run(_run_and_release(_curate(uuid.UUID(source_id))))


@app.task(name="karpwiki.indexing.reindex")
def reindex(page_id: str) -> None:
    asyncio.run(_run_and_release(_reindex(uuid.UUID(page_id))))


@app.task(name="karpwiki.maintenance.detect_staleness")
def detect_staleness(workspace_id: str) -> None:
    asyncio.run(_run_and_release(_detect_staleness(workspace_id)))


@app.task(name="karpwiki.maintenance.detect_superseded_sources")
def detect_superseded_sources(workspace_id: str) -> None:
    asyncio.run(_run_and_release(_detect_superseded_sources(workspace_id)))


@app.task(name="karpwiki.maintenance.detect_existing_duplicates")
def detect_existing_duplicates(workspace_id: str) -> None:
    asyncio.run(_run_and_release(_detect_existing_duplicates(workspace_id)))


@app.task(name="karpwiki.maintenance.detect_orphans")
def detect_orphans(workspace_id: str) -> None:
    asyncio.run(_run_and_release(_detect_orphans(workspace_id)))


@app.task(name="karpwiki.maintenance.detect_contradictions")
def detect_contradictions(workspace_id: str) -> None:
    asyncio.run(_run_and_release(_detect_contradictions(workspace_id)))


@app.task(name="karpwiki.maintenance.detect_staleness_tiered")
def detect_staleness_tiered(workspace_id: str) -> None:
    asyncio.run(_run_and_release(_detect_staleness_tiered(workspace_id)))


@app.task(name="karpwiki.maintenance.dispatch_daily_detectors")
def dispatch_daily_detectors() -> None:
    asyncio.run(_run_and_release(_dispatch_daily_detectors()))


@app.task(name="karpwiki.maintenance.dispatch_contradiction_detector")
def dispatch_contradiction_detector() -> None:
    asyncio.run(_run_and_release(_dispatch_contradiction_detector()))


@app.task(name="karpwiki.connector_polling.poll_connector")
def poll_connector(connector_id: str) -> None:
    asyncio.run(_run_and_release(_poll_connector(uuid.UUID(connector_id))))


@app.task(name="karpwiki.connector_polling.dispatch_connector_polls")
def dispatch_connector_polls() -> None:
    asyncio.run(_run_and_release(_dispatch_connector_polls()))
