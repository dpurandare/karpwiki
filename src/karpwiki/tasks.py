"""Async layer — Celery app with one queue per job type (01 §1, 06 §4, 08 §2).

The pools are separated here because they scale on different signals: classification
and curation are LLM-bound, indexing and the maintenance advisor are compute-bound.

Phase 2 step 30 fills the classification/curation/indexing queues by wrapping the pure
orchestration functions `ingestion.classify_source`/`curate_source` and `search.reindex`
(09 §21's deliberately deferred gap). Nothing enqueues these yet — that's step 32; this
step only makes the tasks real and independently runnable.
"""

import asyncio
import logging
import uuid

from celery import Celery

from . import ingestion, pipeline, search
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
        await ingestion.classify_source(session, source=source, call=call)


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
    """09 §21/step 32: acceptance runs dedup, then curate only if dedup clears — one task,
    since both are cheap/synchronous steps of the same "a classified source becomes pages"
    job and `tasks.py` has no dedicated dedup queue to dispatch a second hop into.

    `call` is the same test-only seam as `_classify`'s."""
    async with session_scope() as session:
        source = await session.get(RawSource, source_id)
        if source is None:
            logger.warning("curate task: no source %s", source_id)
            return
        summary = await _classification_summary(session, source_id)
        state = await ingestion.check_duplicates(session, source=source, summary=summary)
        if state is PipelineState.ingesting:
            workspace = await session.get(Workspace, source.workspace_id)
            await ingestion.curate_source(session, source=source, workspace=workspace, call=call)


async def _reindex(page_id: uuid.UUID) -> None:
    async with session_scope() as session:
        await search.reindex(session, page_id)


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
