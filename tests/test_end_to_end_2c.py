"""Phase 2 step 42 — 2c verify: closes out the Maintenance Advisor track.

Seeds real stale, orphaned, superseded-source, existing-duplicate, and contradicting
content; runs each of the five detectors' real task bodies directly against the test DB
(`task_db`, mirroring what a real worker/beat-scheduled dispatch does — phase2-tasklist.md
step 41); confirms every resulting review item appears in the same `GET /review-items`
queue ingest-time items already use, with correct evidence in `detail`; resolves each one
through the same `POST /review-items/{id}/resolve` endpoint. Mocked LLM (a fake `call` for
the one detector that spends one at detection time, step 40) and no broker (the autouse
`dispatched` fixture) — the fast, deterministic, committed counterpart to a real live
check, matching every prior closing-verify file's convention (see
spec/09-implementation-notes.md §44 for the real live runs behind steps 40-41).
"""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta

from karpwiki import objectstore, search, tasks, versioning
from karpwiki.advisor import ContradictionJudgment
from karpwiki.models import (
    AccessPolicy,
    IndexState,
    IndexStatus,
    IndexType,
    PageStatus,
    PageType,
    PageVersion,
    QueryLog,
    RawSource,
    RawSourceStatus,
    Role,
)

ADMIN = {"X-Karpwiki-User": "avery"}

DUPLICATE_BODY = (
    "The payments worker drains its queue before restart. Operators run a rollout restart "
    "and verify that consumer lag returns to zero within five minutes."
)
# Scores ~0.5 against each other via search.find_similar — inside the [0.35, 0.60)
# contradiction candidate band (same pair tests/test_advisor.py's own unit tests use).
CONTRADICTION_BODY_A = (
    "Restart the payments worker daily using the automated recovery script during "
    "scheduled maintenance windows to clear the queue backlog."
)
CONTRADICTION_BODY_B = (
    "Restart the payments worker weekly using a manual failover checklist during "
    "unplanned incident response to clear the queue backlog."
)


async def _page(session, workspace, *, title, path=None, body="Body text.", indexed=False):
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=path or f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=PageType.concept,
        title=title,
        description=f"About {title}.",
        date=date(2026, 8, 19),
        tags=["a", "b"],
        body=body,
        author="system:curator",
        status=PageStatus.published,
    )
    if indexed:
        version = await session.get(PageVersion, page.current_version_id)
        await search.index_page(session, page=page, version=version)
    return page


async def _source_page(session, workspace, source):
    """Path must be `sources/{source_id}.md` exactly — `ingestion._write_source_page`'s
    convention, and the only path `find_pages_citing_superseded_sources` looks for."""
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"sources/{source.source_id}.md",
        page_type=PageType.source,
        title="A Source",
        description="About a source.",
        date=date(2026, 8, 19),
        tags=["source", "narrative"],
        body="Source body.",
        author="system:curator",
        status=PageStatus.published,
    )
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    status.state = IndexState.indexed
    await session.flush()
    return page


async def _fake_contradiction_check(**_kwargs):
    return ContradictionJudgment(
        contradicts=True, outdated_page="a", explanation="Conflicting restart cadence."
    )


async def test_2c_end_to_end(client, session, workspace, task_db, dispatched):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))

    # --- seed: staleness (Signal 1) ---
    stale_page = await _page(session, workspace, title="Old Runbook")
    stale_status = await session.get(IndexStatus, (stale_page.page_id, IndexType.fts))
    stale_status.state = IndexState.stale
    stale_version = await session.get(PageVersion, stale_page.current_version_id)
    stale_version.created_at = datetime.now(UTC) - timedelta(days=100)

    # --- seed: orphan (zero inbound page_link rows, zero query_log appearances) ---
    orphan_page = await _page(session, workspace, title="Forgotten Page")

    # --- seed: superseded source past the 180-day retention window ---
    source_id = uuid.uuid4()
    key = f"/{workspace.workspace_id}/sources/{source_id}/old.md"
    payload = b"old content"
    objectstore.write_bytes(key, payload)
    source = RawSource(
        source_id=source_id,
        workspace_id=workspace.workspace_id,
        object_key=key,
        filename="old.md",
        content_hash=hashlib.sha256(payload).hexdigest(),
        submitted_by="user:deepak",
        status=RawSourceStatus.superseded,
        superseded_at=datetime.now(UTC) - timedelta(days=200),
    )
    session.add(source)
    await session.flush()
    source_page = await _source_page(session, workspace, source)

    # --- seed: existing-content duplicate (two published pages, near-identical bodies) ---
    dup_older = await _page(session, workspace, title="Restarting Payments", body=DUPLICATE_BODY, indexed=True)
    dup_newer = await _page(
        session, workspace, title="Payments Restart Runbook", body=DUPLICATE_BODY, indexed=True
    )

    # --- seed: contradiction candidate pair ---
    contra_a = await _page(session, workspace, title="Daily Restart", body=CONTRADICTION_BODY_A, indexed=True)
    contra_b = await _page(session, workspace, title="Weekly Restart", body=CONTRADICTION_BODY_B, indexed=True)

    # Every other concept page above has zero inbound links too, so without a query_log
    # entry the Orphan Detector would (correctly!) also flag them — a real overlap between
    # detectors, not a bug, but it would blur this file's per-detector evidence
    # assertions. Give each a recent query hit so only `orphan_page` stays a genuine
    # orphan, keeping each detector's seed data cleanly attributable to its own item.
    for page in (stale_page, dup_older, dup_newer, contra_a, contra_b):
        session.add(
            QueryLog(
                principal="user:deepak",
                query_text=page.path,
                resolved_workspaces=[workspace.workspace_id],
                results=[{"page_id": str(page.page_id), "score": 0.9}],
            )
        )

    await session.commit()

    # --- run the advisor: all five detectors, exactly what a real beat-scheduled
    # dispatch fires per workspace (phase2-tasklist.md step 41) ---
    await tasks._detect_staleness(workspace.workspace_id)
    await tasks._detect_superseded_sources(workspace.workspace_id)
    await tasks._detect_existing_duplicates(workspace.workspace_id)
    await tasks._detect_orphans(workspace.workspace_id)
    await tasks._detect_contradictions(workspace.workspace_id, call=_fake_contradiction_check)

    # --- confirm every finding surfaced in the same admin queue ingest-time items use ---
    listed = await client.get(
        "/review-items", headers=ADMIN, params={"workspace_id": workspace.workspace_id}
    )
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 5

    by_key = {}
    for item in items:
        detail = item["detail"] or {}
        by_key[(item["kind"], detail.get("reason"))] = item

    reindex_item = by_key[("reindex", None)]
    # Signal 1 (`stale_page`) and Signal 2 (`source_page`, indexed and citing the
    # superseded source seeded below) both land in the same batched reindex item — the
    # same detector, two conditions, per 05 §2/advisor.py's `run_staleness_detector`.
    assert {p["page_id"] for p in reindex_item["detail"]["pages"]} == {
        str(stale_page.page_id),
        str(source_page.page_id),
    }

    superseded_item = by_key[("prune", "superseded_source_retention")]
    assert superseded_item["detail"]["sources"][0]["source_id"] == str(source_id)

    orphan_item = by_key[("prune", "orphaned")]
    assert {p["page_id"] for p in orphan_item["detail"]["pages"]} == {str(orphan_page.page_id)}

    contradiction_item = by_key[("prune", "contradicted_by")]
    assert contradiction_item["detail"]["page_id"] == str(contra_a.page_id)
    assert contradiction_item["detail"]["contradicting_page_id"] == str(contra_b.page_id)

    duplicate_item = by_key[("duplicate", None)]
    assert duplicate_item["detail"]["raised_by"] == "advisor"
    assert duplicate_item["detail"]["primary_page_id"] == str(dup_older.page_id)
    assert duplicate_item["detail"]["duplicate_page_id"] == str(dup_newer.page_id)

    # --- resolve every item through the real resolve endpoint ---
    reindex_resp = await client.post(
        f"/review-items/{reindex_item['review_id']}/resolve", headers=ADMIN, json={"action": "reindex now"}
    )
    assert reindex_resp.status_code == 200
    assert set(dispatched["reindex"]) == {str(stale_page.page_id), str(source_page.page_id)}
    while dispatched["reindex"]:
        pid = dispatched["reindex"].pop(0)
        await tasks._reindex(uuid.UUID(pid))
    await session.commit()
    for page_id in (stale_page.page_id, source_page.page_id):
        refreshed_status = await session.get(IndexStatus, (page_id, IndexType.fts))
        await session.refresh(refreshed_status)
        assert refreshed_status.state is IndexState.indexed

    superseded_resp = await client.post(
        f"/review-items/{superseded_item['review_id']}/resolve",
        headers=ADMIN,
        json={"action": "delete superseded source"},
    )
    assert superseded_resp.status_code == 200
    refreshed_source = await session.get(RawSource, source_id)
    await session.refresh(refreshed_source)
    assert refreshed_source.status is RawSourceStatus.archived

    orphan_resp = await client.post(
        f"/review-items/{orphan_item['review_id']}/resolve", headers=ADMIN, json={"action": "archive page"}
    )
    assert orphan_resp.status_code == 200
    refreshed_orphan = await session.get(type(orphan_page), orphan_page.page_id)
    await session.refresh(refreshed_orphan)
    assert refreshed_orphan.status is PageStatus.archived

    contradiction_resp = await client.post(
        f"/review-items/{contradiction_item['review_id']}/resolve",
        headers=ADMIN,
        json={"action": "archive page"},
    )
    assert contradiction_resp.status_code == 200
    refreshed_flagged = await session.get(type(contra_a), contra_a.page_id)
    await session.refresh(refreshed_flagged)
    assert refreshed_flagged.status is PageStatus.archived
    refreshed_other = await session.get(type(contra_b), contra_b.page_id)
    await session.refresh(refreshed_other)
    assert refreshed_other.status is PageStatus.published

    # "supersede" rather than "merge": keeps this closing verify LLM-free like every
    # other resolution here — advisor-raised `merge` is already covered by step 38's own
    # unit test and live check, doesn't need re-proving in this file.
    duplicate_resp = await client.post(
        f"/review-items/{duplicate_item['review_id']}/resolve", headers=ADMIN, json={"action": "supersede"}
    )
    assert duplicate_resp.status_code == 200
    refreshed_dup = await session.get(type(dup_newer), dup_newer.page_id)
    await session.refresh(refreshed_dup)
    assert refreshed_dup.status is PageStatus.archived
    refreshed_primary = await session.get(type(dup_older), dup_older.page_id)
    await session.refresh(refreshed_primary)
    assert refreshed_primary.status is PageStatus.published

    # Every item resolved, none left open.
    remaining = await client.get(
        "/review-items", headers=ADMIN, params={"workspace_id": workspace.workspace_id}
    )
    assert remaining.json()["items"] == []
