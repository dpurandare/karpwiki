"""Phase 2 step 35 — 2b verify: closes out the Real Async Job Dispatch track.

Submits a document through the real gateway with nothing manually driving any pipeline
stage — no direct call to classify_source/curate_source/reindex, no admin action. The only
thing this test does after POST /sources is *drain the dispatch chain the real wiring
itself produced* (steps 30-33): each `.delay()` call recorded by the autouse `dispatched`
fixture is run through the real task body in turn, exactly what a real worker would do,
just executed inline so this runs in CI without a live broker or a real LLM call. The
mocked-LLM/no-broker combination is the fast, deterministic, committed counterpart to the
real end-to-end live check (real HTTP, real broker, real workers, real gpt-5-nano, real
wall-clock bound) — see spec/09-implementation-notes.md §38 for that run's results.
"""

import uuid

from sqlalchemy import select

from karpwiki import search, tasks
from karpwiki.classify import ClassificationResult
from karpwiki.curate import CuratedContent, CuratedPage
from karpwiki.models import IndexState, IndexStatus, IndexType, PipelineState, RawSource, WikiPage

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}


def _classifies_as(label, confidence=0.9, summary="A doc about restarting a worker."):
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


async def test_2b_pipeline_completes_purely_via_dispatch(client, session, workspace, dispatched, task_db):
    resp = await client.post(
        "/sources", headers=CONTRIBUTOR, data={"text": "Runbook: restarting the payments worker."}
    )
    source_id = resp.json()["source_id"]
    await session.commit()

    # 03 §2/step 32: submission alone enqueues classification — nothing else has run yet.
    assert dispatched["classify_source"] == [source_id]
    assert dispatched["curate_source"] == []
    assert dispatched["reindex"] == []

    # Drain exactly what the real wiring dispatched, running each task's real body in turn —
    # a stand-in for a worker process, not a reimplementation of one. `_classify` dispatching
    # `curate_source.delay(...)` inside this loop is what populates the next list below.
    while dispatched["classify_source"]:
        sid = dispatched["classify_source"].pop(0)
        await tasks._classify(uuid.UUID(sid), call=_classifies_as("eng.runbook"))
    await session.commit()

    assert dispatched["curate_source"] == [source_id]
    while dispatched["curate_source"]:
        sid = dispatched["curate_source"].pop(0)
        await tasks._curate(
            uuid.UUID(sid), call=_curates_as(title="Payments Worker Restart", body="Steps to restart.")
        )
    await session.commit()

    source = await session.get(RawSource, uuid.UUID(source_id))
    await session.refresh(source)
    assert source.pipeline_state is PipelineState.ingested
    assert source.workspace_id == workspace.workspace_id

    page = (
        await session.execute(select(WikiPage).where(WikiPage.path == "concepts/payments-worker-restart.md"))
    ).scalar_one()
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    assert status.state in (IndexState.pending, IndexState.stale)
    assert str(page.page_id) in dispatched["reindex"]

    while dispatched["reindex"]:
        pid = dispatched["reindex"].pop(0)
        await tasks._reindex(uuid.UUID(pid))
    await session.commit()

    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    await session.refresh(status)
    assert status.state is IndexState.indexed

    results = await search.search(session, query="payments worker", workspace_ids=[workspace.workspace_id])
    assert any(r.path == page.path for r in results)
