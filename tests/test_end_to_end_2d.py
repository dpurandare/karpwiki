"""Phase 2 step 50 — 2d verify: closes out the Full API + MCP + Horizontal Scaling track.

Runs search, submit, and (as admin) resolve-a-review-item entirely through the MCP
protocol adapter (`mcp.client.client.Client`, real in-process MCP protocol machinery,
same as `test_mcp_server.py`) rather than the REST surface — the "not only through the
REST surface" half of this step's verify. Drains the real dispatch chain a real
`wiki_submit` call produces (mocked LLM/no broker, same convention as
`test_end_to_end_2b.py`) so `wiki_search` finds a genuinely curated-and-indexed page, not
a stub. The horizontal-scaling half (a second gateway instance behind a load balancer,
no session affinity) is an infra claim a committed test can't meaningfully assert — see
spec/09-implementation-notes.md §53 for that live check, run against the real `gateway`/
`nginx` docker-compose services step 49 built.
"""

import json
import uuid

from mcp.client.client import Client
from sqlalchemy import select

from karpwiki import mcp_server, tasks
from karpwiki.classify import ClassificationResult
from karpwiki.curate import CuratedContent, CuratedPage
from karpwiki.models import AccessPolicy, PipelineState, RawSource, Role, WikiPage


def _classifies_as(label, confidence=0.9, summary="A doc about horizontal scaling."):
    async def _call(**_kwargs):
        return ClassificationResult(summary=summary, document_type=label, confidence=confidence)

    return _call


def _curates_as(*, title, body):
    content = CuratedContent(
        source_title=title,
        source_description=f"About {title}.",
        source_summary=body,
        source_key_points=[body],
        pages=[CuratedPage(page_type="concept", title=title, tags=["ops", "scaling"], body=body)],
    )

    async def _call(**_kwargs):
        return content

    return _call


async def _call(client: Client, name: str, **kwargs):
    result = await client.call_tool(name, kwargs)
    text = result.content[0].text
    return result.is_error, (json.loads(text) if not result.is_error else text)


async def test_2d_mcp_client_search_submit_and_admin_resolve_end_to_end(
    client, session, workspace, dispatched, task_db, monkeypatch
):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await session.commit()

    monkeypatch.setenv("KARPWIKI_MCP_USER", "deepak")
    async with Client(mcp_server.create_mcp_server()) as mcp_client:
        is_error, submitted = await _call(
            mcp_client, "wiki_submit", text="Runbook: scaling the gateway behind a load balancer."
        )
    assert not is_error
    assert submitted["pipeline_state"] == "submitted"
    source_id = submitted["source_id"]
    await session.commit()

    # Drain exactly what the real wiring dispatched (03 §2/step 32), same as
    # test_end_to_end_2b.py — a stand-in for a real worker, not a reimplementation.
    assert dispatched["classify_source"] == [source_id]
    while dispatched["classify_source"]:
        sid = dispatched["classify_source"].pop(0)
        await tasks._classify(uuid.UUID(sid), call=_classifies_as("eng.runbook"))
    await session.commit()

    assert dispatched["curate_source"] == [source_id]
    while dispatched["curate_source"]:
        sid = dispatched["curate_source"].pop(0)
        await tasks._curate(
            uuid.UUID(sid),
            call=_curates_as(title="Gateway Horizontal Scaling", body="Steps to scale the gateway."),
        )
    await session.commit()

    source = await session.get(RawSource, uuid.UUID(source_id))
    await session.refresh(source)
    assert source.pipeline_state is PipelineState.ingested

    page = (
        await session.execute(
            select(WikiPage).where(WikiPage.path == "concepts/gateway-horizontal-scaling.md")
        )
    ).scalar_one()
    assert str(page.page_id) in dispatched["reindex"]
    while dispatched["reindex"]:
        pid = dispatched["reindex"].pop(0)
        await tasks._reindex(uuid.UUID(pid))
    await session.commit()

    # Search — real FTS, real curated content, all through the MCP tool.
    async with Client(mcp_server.create_mcp_server()) as mcp_client:
        is_error, found = await _call(mcp_client, "wiki_search", q="scaling the gateway")
    assert not is_error
    assert any(i["path"] == "concepts/gateway-horizontal-scaling.md" for i in found["items"])

    # Resolve the submission's own review item, as admin — through MCP, not REST.
    monkeypatch.setenv("KARPWIKI_MCP_USER", "avery")
    async with Client(mcp_server.create_mcp_server()) as mcp_client:
        is_error, items = await _call(
            mcp_client, "wiki_list_review_items", workspace_id=None, kind="submission"
        )
        assert not is_error
        [item] = [i for i in items["items"] if i["subject_ref"] == source_id]

        is_error, resolved = await _call(
            mcp_client, "wiki_resolve_review_item", review_id=item["review_id"], action="acknowledge"
        )
    assert not is_error
    assert resolved["status"] == "resolved"
