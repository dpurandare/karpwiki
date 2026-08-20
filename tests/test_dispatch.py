"""Dispatch wiring (phase2-tasklist.md step 32) — submission enqueues classification;
acceptance (fresh or admin-resolved) enqueues curation; a page write enqueues reindex.

These test the *wiring* in api.py — the right task gets `.delay()`d with the right id at
the right point — not the task bodies themselves (tests/test_tasks.py already covers
those). The autouse `dispatched` fixture in conftest.py intercepts every `.delay()` call so
nothing here touches the real broker.
"""

import uuid
from datetime import date

from sqlalchemy import select

from karpwiki import ingestion, objectstore, pipeline, review, search, versioning
from karpwiki.curate import MergedPage
from karpwiki.models import (
    AccessPolicy,
    PageStatus,
    PageType,
    PageVersion,
    PipelineState,
    RawSource,
    ReviewItem,
    ReviewKind,
    Role,
    VersionTrigger,
    WikiPage,
)

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
ADMIN = {"X-Karpwiki-User": "avery"}


async def _grant_admin(session, workspace, principal="avery"):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal=principal, role=Role.admin))
    await session.flush()


async def _page(session, workspace, *, title="Runbook", body="Body text."):
    return await versioning.create_page(
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


async def _classification_pending_review(session):
    """Mirrors test_review_resolution.py's helper — a source parked `pending_review` by
    low classification confidence (03 §3), ready for an admin to resolve."""
    source_id = uuid.uuid4()
    key = f"/_inbox/{source_id}/f.md"
    objectstore.write_bytes(key, b"content")
    source = RawSource(
        source_id=source_id,
        object_key=key,
        filename="f.md",
        content_hash="deadbeef",
        submitted_by="user:deepak",
        pipeline_state=PipelineState.submitted,
    )
    session.add(source)
    await session.flush()
    await pipeline.transition(
        session, source=source, to_state=PipelineState.classifying, actor="system:classifier"
    )
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.pending_review,
        actor="system:classifier",
        detail={"reason": "confidence below threshold", "candidates": []},
    )
    item = await review.create(session, kind=ReviewKind.classification, subject_ref=str(source.source_id))
    await session.commit()
    return source, item


async def _pii_pending_review(session):
    """A source parked `pending_review` by the PII scanner (07 §2, phase3-tasklist.md step
    71) — mirrors `_classification_pending_review`'s exact construction, a different reason
    for the same resting state."""
    source_id = uuid.uuid4()
    key = f"/_inbox/{source_id}/f.md"
    objectstore.write_bytes(key, b"password: hunter2")
    source = RawSource(
        source_id=source_id,
        object_key=key,
        filename="f.md",
        content_hash="deadbeef",
        submitted_by="user:deepak",
        pipeline_state=PipelineState.submitted,
    )
    session.add(source)
    await session.flush()
    await pipeline.transition(
        session, source=source, to_state=PipelineState.classifying, actor="system:classifier"
    )
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.pending_review,
        actor="system:pii_scanner",
        detail={"reason": "pii_detected", "categories": ["credential"]},
    )
    item = await review.create(
        session, kind=ReviewKind.pii_review, subject_ref=str(source.source_id),
        detail={"categories": ["credential"]},
    )
    await session.commit()
    return source, item


async def _near_duplicate_pending_review(session, workspace):
    """Mirrors test_review_resolution.py's helper — a `classified` source parked
    `pending_review` by a near-duplicate verdict against an existing page."""
    body = "The payments worker drains its queue before restart."
    target_page = await _page(session, workspace, title="Restarting Payments", body=body)
    version = await session.get(PageVersion, target_page.current_version_id)
    await search.index_page(session, page=target_page, version=version)

    source_id = uuid.uuid4()
    key = f"/{workspace.workspace_id}/sources/{source_id}/dup.md"
    objectstore.write_bytes(key, body.encode())
    source = RawSource(
        source_id=source_id,
        workspace_id=workspace.workspace_id,
        object_key=key,
        filename="dup.md",
        content_hash="deadbeef2",
        submitted_by="user:deepak",
        pipeline_state=PipelineState.classified,
    )
    session.add(source)
    await session.flush()
    state = await ingestion.check_duplicates(session, source=source, summary=body)
    assert state is PipelineState.pending_review
    item = (
        await session.execute(select(ReviewItem).where(ReviewItem.subject_ref == str(source.source_id)))
    ).scalar_one()
    await session.commit()
    return source, item, target_page


async def test_submission_dispatches_classify_source(client, session, dispatched):
    resp = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "runbook text"})
    source_id = resp.json()["source_id"]
    assert dispatched["classify_source"] == [source_id]


async def test_resolving_classification_dispatches_curate_source(client, session, workspace, dispatched):
    await _grant_admin(session, workspace)
    source, item = await _classification_pending_review(session)

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "eng.runbook"}
    )
    assert resp.json()["pipeline_state"] == "classified"
    assert dispatched["curate_source"] == [str(source.source_id)]
    assert dispatched["reindex"] == []


async def test_resolving_pii_review_acknowledge_dispatches_classify_source(
    client, session, workspace, dispatched
):
    """Step 71 (07 §2) — `acknowledge` re-dispatches `classify_source` (checked by kind/
    action in api.py, not by the returned pipeline state, which stays `pending_review`)."""
    await _grant_admin(session, workspace)
    source, item = await _pii_pending_review(session)

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "acknowledge"}
    )
    assert resp.json()["pipeline_state"] == "pending_review"
    assert dispatched["classify_source"] == [str(source.source_id)]
    assert dispatched["curate_source"] == []


async def test_resolving_pii_review_reject_dispatches_nothing(client, session, workspace, dispatched):
    await _grant_admin(session, workspace)
    source, item = await _pii_pending_review(session)

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "reject"}
    )
    assert resp.json()["pipeline_state"] == "rejected"
    assert dispatched["classify_source"] == []
    assert dispatched["curate_source"] == []


async def test_resolving_duplicate_keep_both_dispatches_curate_source(client, session, workspace, dispatched):
    await _grant_admin(session, workspace)
    source, item, _ = await _near_duplicate_pending_review(session, workspace)

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "keep_both"}
    )
    assert resp.json()["pipeline_state"] == "ingesting"
    assert dispatched["curate_source"] == [str(source.source_id)]


async def test_resolving_duplicate_reject_dispatches_nothing(client, session, workspace, dispatched):
    await _grant_admin(session, workspace)
    source, item, _ = await _near_duplicate_pending_review(session, workspace)

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "reject"}
    )
    assert resp.json()["pipeline_state"] == "rejected"
    assert dispatched["curate_source"] == []
    assert dispatched["reindex"] == []


async def test_resolving_duplicate_reject_notifies_the_submitter(client, session, workspace, caplog):
    """Step 67 (07 §3): no injectable sink through the REST path — asserted via the real
    default `LogNotificationSink` (no webhook configured in tests), same as
    `KARPWIKI_NOTIFICATION_WEBHOOK_URL` being unset makes real everywhere else."""
    import logging

    await _grant_admin(session, workspace)
    source, item, _ = await _near_duplicate_pending_review(session, workspace)

    with caplog.at_level(logging.INFO):
        resp = await client.post(
            f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "reject"}
        )
    assert resp.json()["pipeline_state"] == "rejected"
    [record] = [r for r in caplog.records if "source rejected" in r.getMessage()]
    assert "user:deepak" in record.getMessage()
    assert str(source.source_id) in record.getMessage()


async def test_resolving_duplicate_merge_dispatches_reindex_for_the_target_page(
    client, session, workspace, dispatched, monkeypatch
):
    await _grant_admin(session, workspace)
    source, item, target_page = await _near_duplicate_pending_review(session, workspace)

    async def _merge_call(**_kwargs):
        return MergedPage(body="Merged body.", change_summary="Merged duplicate submission.")

    import karpwiki.ingestion as ingestion_module

    monkeypatch.setattr(ingestion_module, "call_merge_model", _merge_call)

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "merge"}
    )
    assert resp.json()["pipeline_state"] == "ingested"
    assert dispatched["reindex"] == [str(target_page.page_id)]
    assert dispatched["curate_source"] == []


async def test_resolving_duplicate_merge_notifies_the_submitter(
    client, session, workspace, dispatched, monkeypatch, caplog
):
    """Step 67 (07 §3) — a distinct "merged" notification, not the generic "ingested" one
    (that one only ever fires from `tasks._curate`, never reached by a synchronous merge)."""
    import logging

    await _grant_admin(session, workspace)
    source, item, target_page = await _near_duplicate_pending_review(session, workspace)

    async def _merge_call(**_kwargs):
        return MergedPage(body="Merged body.", change_summary="Merged duplicate submission.")

    import karpwiki.ingestion as ingestion_module

    monkeypatch.setattr(ingestion_module, "call_merge_model", _merge_call)

    with caplog.at_level(logging.INFO):
        resp = await client.post(
            f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "merge"}
        )
    assert resp.json()["pipeline_state"] == "ingested"
    [record] = [r for r in caplog.records if "source merged" in r.getMessage()]
    assert "user:deepak" in record.getMessage()
    assert target_page.path in record.getMessage()


async def test_rollback_dispatches_reindex_for_the_page(client, session, workspace, dispatched):
    await _grant_admin(session, workspace)
    page = await _page(session, workspace, body="v1")
    v1 = page.current_version_id
    await versioning.write_version(
        session, page=page, body="v2", author="user:deepak", trigger=VersionTrigger.manual_edit
    )
    await session.commit()

    resp = await client.post(
        f"/pages/{page.page_id}/rollback", headers=ADMIN, json={"target_version_id": str(v1)}
    )
    assert resp.status_code == 200
    assert dispatched["reindex"] == [str(page.page_id)]


async def test_bulk_move_dispatches_reindex_per_moved_page(
    client, session, workspace, other_workspace, dispatched
):
    await _grant_admin(session, workspace)
    await _grant_admin(session, other_workspace)
    page = await _page(session, workspace, title="Move Me")
    await session.commit()

    resp = await client.post(
        f"/workspaces/{workspace.workspace_id}/bulk-move",
        headers=ADMIN,
        json={
            "target_workspace_id": other_workspace.workspace_id,
            "page_ids": [str(page.page_id)],
            "source_ids": [],
        },
    )
    assert resp.json()["completed"] is True
    assert dispatched["reindex"] == [str(page.page_id)]


async def test_resolving_reindex_now_dispatches_reindex_for_each_page(
    client, session, workspace, dispatched
):
    """Step 36: approving a Staleness Detector item dispatches reindex for exactly the
    pages it found (05 §3), read back from the review item's own `detail`."""
    from datetime import UTC, datetime, timedelta

    from karpwiki import advisor
    from karpwiki.models import IndexState, IndexStatus, IndexType, PageVersion

    await _grant_admin(session, workspace)
    page = await _page(session, workspace, title="Stale Page")
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    status.state = IndexState.stale
    version = await session.get(PageVersion, page.current_version_id)
    version.created_at = datetime.now(UTC) - timedelta(days=100)
    await session.flush()

    item = await advisor.run_staleness_detector(session, workspace_id=workspace.workspace_id, threshold_days=90)
    await session.commit()

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "reindex now"}
    )
    assert resp.status_code == 200
    assert dispatched["reindex"] == [str(page.page_id)]


async def test_resolving_reindex_dismiss_dispatches_nothing(client, session, workspace, dispatched):
    from datetime import UTC, datetime, timedelta

    from karpwiki import advisor
    from karpwiki.models import IndexState, IndexStatus, IndexType, PageVersion

    await _grant_admin(session, workspace)
    page = await _page(session, workspace, title="Stale Page")
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    status.state = IndexState.stale
    version = await session.get(PageVersion, page.current_version_id)
    version.created_at = datetime.now(UTC) - timedelta(days=100)
    await session.flush()

    item = await advisor.run_staleness_detector(session, workspace_id=workspace.workspace_id, threshold_days=90)
    await session.commit()

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "dismiss"}
    )
    assert resp.status_code == 200
    assert dispatched["reindex"] == []


async def test_resolving_delete_superseded_source_archives_it_no_dispatch(
    client, session, workspace, dispatched
):
    """Step 37: "delete superseded source" is a synchronous status flip (05 §4 delegates
    physical erasure to an external object-store lifecycle policy) — no task dispatch."""
    import hashlib
    from datetime import UTC, datetime, timedelta

    from karpwiki import advisor, objectstore
    from karpwiki.models import RawSource, RawSourceStatus

    await _grant_admin(session, workspace)
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
    await session.flush()

    item = await advisor.run_superseded_source_detector(
        session, workspace_id=workspace.workspace_id, retention_days=180
    )
    await session.commit()

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve",
        headers=ADMIN,
        json={"action": "delete superseded source"},
    )
    assert resp.status_code == 200
    assert dispatched["reindex"] == []
    assert dispatched["curate_source"] == []

    source = await session.get(RawSource, source_id)
    await session.refresh(source)
    assert source.status is RawSourceStatus.archived


# --- Stuck-Pipeline Sweep (phase3-tasklist.md step 64) -----------------------------------


async def _stuck_source(session, *, workspace=None, filename="doc.md", pipeline_state, hours_ago=2):
    import hashlib
    from datetime import UTC, datetime, timedelta

    from karpwiki import objectstore
    from karpwiki.models import IngestionLog

    source_id = uuid.uuid4()
    workspace_id = workspace.workspace_id if workspace is not None else None
    key = f"/{workspace_id or '_inbox'}/{source_id}/{filename}"
    payload = b"content"
    objectstore.write_bytes(key, payload)
    source = RawSource(
        source_id=source_id,
        workspace_id=workspace_id,
        object_key=key,
        filename=filename,
        content_hash=hashlib.sha256(payload).hexdigest(),
        submitted_by="user:deepak",
        pipeline_state=pipeline_state,
    )
    session.add(source)
    await session.flush()
    session.add(
        IngestionLog(
            source_id=source_id,
            workspace_id=workspace_id,
            from_state=None,
            to_state=pipeline_state,
            actor="test",
            created_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        )
    )
    await session.flush()
    return source


async def test_resolving_stuck_retry_dispatches_classify_source_for_a_submitted_source(
    client, session, workspace, dispatched
):
    """A `submitted`-stuck source is the state a lost first dispatch would still be sitting
    in — re-dispatching `classify_source` directly is safe, no transition needed."""
    from karpwiki import advisor

    await _grant_admin(session, workspace)
    stuck = await _stuck_source(session, pipeline_state=PipelineState.submitted, hours_ago=2)
    item = await advisor.run_stuck_pipeline_detector(session, threshold_hours=1)
    await session.commit()

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "retry"}
    )
    assert resp.status_code == 200
    assert dispatched["classify_source"] == [str(stuck.source_id)]
    assert dispatched["curate_source"] == []


async def test_resolving_stuck_retry_dispatches_curate_source_for_a_classified_source(
    client, session, workspace, dispatched
):
    """`classified`/`ingesting` are both already re-entrant by design (`tasks._curate`'s
    own if/elif branching on `pipeline_state`) — re-dispatching `curate_source` directly is
    safe, no transition needed."""
    from karpwiki import advisor

    await _grant_admin(session, workspace)
    stuck = await _stuck_source(
        session, workspace=workspace, pipeline_state=PipelineState.classified, hours_ago=2
    )
    item = await advisor.run_stuck_pipeline_detector(session, threshold_hours=1)
    await session.commit()

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "retry"}
    )
    assert resp.status_code == 200
    assert dispatched["curate_source"] == [str(stuck.source_id)]
    assert dispatched["classify_source"] == []


async def test_resolving_stuck_abort_rejects_the_source_no_dispatch(
    client, session, workspace, dispatched
):
    from karpwiki import advisor
    from karpwiki.models import RawSourceStatus

    await _grant_admin(session, workspace)
    stuck = await _stuck_source(
        session, workspace=workspace, pipeline_state=PipelineState.ingesting, hours_ago=2
    )
    item = await advisor.run_stuck_pipeline_detector(session, threshold_hours=1)
    await session.commit()

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "abort"}
    )
    assert resp.status_code == 200
    assert dispatched["classify_source"] == []
    assert dispatched["curate_source"] == []
    assert dispatched["reindex"] == []

    source = await session.get(RawSource, stuck.source_id)
    await session.refresh(source)
    assert source.pipeline_state is PipelineState.rejected
    assert source.status is RawSourceStatus.rejected


async def test_resolving_stuck_dismiss_leaves_the_source_untouched_no_dispatch(
    client, session, workspace, dispatched
):
    from karpwiki import advisor

    await _grant_admin(session, workspace)
    stuck = await _stuck_source(session, pipeline_state=PipelineState.submitted, hours_ago=2)
    item = await advisor.run_stuck_pipeline_detector(session, threshold_hours=1)
    await session.commit()

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "dismiss"}
    )
    assert resp.status_code == 200
    assert dispatched["classify_source"] == []
    assert dispatched["curate_source"] == []

    source = await session.get(RawSource, stuck.source_id)
    await session.refresh(source)
    assert source.pipeline_state is PipelineState.submitted


async def test_resolving_advisor_duplicate_merge_dispatches_reindex_for_the_primary_page(
    client, session, workspace, dispatched, monkeypatch
):
    """Step 38: merging an advisor-raised duplicate writes a new version on the primary
    page (advisor.resolve_existing_duplicate) — dispatched from `item.detail` directly,
    same as the ingest-time merge case dispatches from `ingestion_log`."""
    from datetime import date

    from karpwiki import advisor, search, versioning
    from karpwiki.curate import MergedPage
    from karpwiki.models import PageStatus, PageType, PageVersion

    await _grant_admin(session, workspace)

    body = (
        "The payments worker drains its queue before restart. Operators run a rollout "
        "restart and verify that consumer lag returns to zero within five minutes."
    )
    pages = []
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
        pages.append(page)
    older = pages[0]

    [item] = await advisor.run_existing_content_duplicate_detector(
        session, workspace_id=workspace.workspace_id
    )
    await session.commit()

    async def _fake_merge(**_kwargs):
        return MergedPage(body="Merged body.", change_summary="Merged a duplicate.")

    monkeypatch.setattr(advisor, "call_page_merge_model", _fake_merge)

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "merge"}
    )
    assert resp.status_code == 200
    assert dispatched["reindex"] == [str(older.page_id)]


async def test_resolving_archive_page_archives_it_no_dispatch(client, session, workspace, dispatched):
    """Step 39: "archive page" is a synchronous status flip, same shape as step 37's
    "delete superseded source" — an archived page just stops matching search's own
    published-only filter, no reindex/dispatch needed."""
    from datetime import date

    from karpwiki import advisor, versioning
    from karpwiki.models import PageStatus, PageType, WikiPage

    await _grant_admin(session, workspace)
    page = await versioning.create_page(
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
    await session.flush()

    item = await advisor.run_orphan_detector(session, workspace_id=workspace.workspace_id)
    await session.commit()

    resp = await client.post(
        f"/review-items/{item.review_id}/resolve", headers=ADMIN, json={"action": "archive page"}
    )
    assert resp.status_code == 200
    assert dispatched["reindex"] == []
    assert dispatched["curate_source"] == []

    refreshed = await session.get(WikiPage, page.page_id)
    await session.refresh(refreshed)
    assert refreshed.status is PageStatus.archived


async def test_curate_task_dispatches_reindex_for_pages_it_wrote(session, workspace, task_db, dispatched):
    """Extends tests/test_tasks.py's coverage: `_curate` itself dispatches reindex for
    whatever it left pending/stale in the source's workspace (step 32), not just the
    api.py-level call sites above."""
    from karpwiki import tasks
    from karpwiki.classify import ClassificationResult
    from karpwiki.curate import CuratedContent, CuratedPage

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
    assert dispatched["curate_source"] == [str(source_id)]

    content = CuratedContent(
        source_title="Runbook D",
        source_description="About Runbook D.",
        source_summary="Steps.",
        source_key_points=["Steps."],
        pages=[CuratedPage(page_type="concept", title="Runbook D", tags=["a", "b"], body="Steps.")],
    )

    async def _curate_call(**_kwargs):
        return content

    await tasks._curate(source_id, call=_curate_call)

    page = (
        await session.execute(
            select(WikiPage).where(WikiPage.path == "concepts/runbook-d.md")
        )
    ).scalar_one()
    assert str(page.page_id) in dispatched["reindex"]
