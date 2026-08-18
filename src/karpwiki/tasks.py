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

from celery import Celery

from . import advisor, ingestion, pipeline, search
from .config import CELERY_BROKER_URL
from .db import engine, session_scope
from .models import PipelineState, RawSource, Workspace

logger = logging.getLogger(__name__)

QUEUES = ("classification", "curation", "indexing", "maintenance")

app = Celery("karpwiki", broker=CELERY_BROKER_URL)
app.conf.task_default_queue = "curation"
app.conf.task_routes = {
    "karpwiki.classification.*": {"queue": "classification"},
    "karpwiki.curation.*": {"queue": "curation"},
    "karpwiki.indexing.*": {"queue": "indexing"},
    "karpwiki.maintenance.*": {"queue": "maintenance"},
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
app.conf.broker_transport_options = {"visibility_timeout": 600}


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
