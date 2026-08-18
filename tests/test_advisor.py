"""Maintenance Advisor — Staleness Detector (05 §2-3) — phase2-tasklist.md step 36."""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from karpwiki import advisor, objectstore, review, search, versioning
from karpwiki.models import (
    IndexState,
    IndexStatus,
    IndexType,
    PageStatus,
    PageType,
    PageVersion,
    RawSource,
    RawSourceStatus,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
)


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


async def _make_stale(session, page, *, days_ago: int):
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    status.state = IndexState.stale
    version = await session.get(PageVersion, page.current_version_id)
    version.created_at = datetime.now(UTC) - timedelta(days=days_ago)
    await session.flush()


async def _superseded_source(session, workspace, *, filename="notes.md"):
    source_id = uuid.uuid4()
    key = f"/{workspace.workspace_id}/sources/{source_id}/{filename}"
    payload = b"content"
    objectstore.write_bytes(key, payload)
    source = RawSource(
        source_id=source_id,
        workspace_id=workspace.workspace_id,
        object_key=key,
        filename=filename,
        content_hash=hashlib.sha256(payload).hexdigest(),
        submitted_by="user:deepak",
        status=RawSourceStatus.superseded,
    )
    session.add(source)
    await session.flush()
    return source


async def _source_page(session, workspace, source):
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"sources/{source.source_id}.md",
        page_type=PageType.source,
        title="A Source",
        description="About a source.",
        date=date(2026, 8, 17),
        tags=["source", "narrative"],
        body="Source body.",
        author="system:curator",
        status=PageStatus.published,
    )
    # `find_pages_citing_superseded_sources` only cares about pages that were actually
    # indexed at some point — simulate that directly rather than driving a real reindex.
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    status.state = IndexState.indexed
    await session.flush()
    return page


# --- find_stale_pages -------------------------------------------------------------------


async def test_find_stale_pages_only_past_threshold(session, workspace):
    old_page = await _page(session, workspace, title="Old Page")
    await _make_stale(session, old_page, days_ago=100)

    recent_page = await _page(session, workspace, title="Recent Page")
    await _make_stale(session, recent_page, days_ago=5)

    findings = await advisor.find_stale_pages(session, workspace_id=workspace.workspace_id, threshold_days=90)

    assert [f.page_id for f in findings] == [old_page.page_id]
    assert findings[0].reason == "stale_content"


async def test_find_stale_pages_ignores_indexed_pages(session, workspace):
    page = await _page(session, workspace, title="Fine Page")
    # Not marked stale — index_status stays `pending` from versioning.create_page.
    findings = await advisor.find_stale_pages(session, workspace_id=workspace.workspace_id, threshold_days=0)
    assert findings == []


# --- find_pages_citing_superseded_sources -----------------------------------------------


async def test_find_pages_citing_superseded_sources(session, workspace):
    source = await _superseded_source(session, workspace)
    page = await _source_page(session, workspace, source)

    findings = await advisor.find_pages_citing_superseded_sources(session, workspace_id=workspace.workspace_id)

    assert [f.page_id for f in findings] == [page.page_id]
    assert findings[0].reason == "source_updated"


async def test_find_pages_citing_superseded_sources_skips_already_stale(session, workspace):
    source = await _superseded_source(session, workspace)
    page = await _source_page(session, workspace, source)
    await search.mark_stale(session, page.page_id)

    findings = await advisor.find_pages_citing_superseded_sources(session, workspace_id=workspace.workspace_id)
    assert findings == []


async def test_find_pages_citing_superseded_sources_ignores_active_sources(session, workspace):
    source_id = uuid.uuid4()
    key = f"/{workspace.workspace_id}/sources/{source_id}/f.md"
    objectstore.write_bytes(key, b"x")
    active_source = RawSource(
        source_id=source_id,
        workspace_id=workspace.workspace_id,
        object_key=key,
        filename="f.md",
        content_hash="deadbeef",
        submitted_by="user:deepak",
        status=RawSourceStatus.active,
    )
    session.add(active_source)
    await session.flush()
    await _source_page(session, workspace, active_source)

    findings = await advisor.find_pages_citing_superseded_sources(session, workspace_id=workspace.workspace_id)
    assert findings == []


# --- run_staleness_detector --------------------------------------------------------------


async def test_run_staleness_detector_batches_into_one_item(session, workspace):
    old_page = await _page(session, workspace, title="Old Page")
    await _make_stale(session, old_page, days_ago=100)
    source = await _superseded_source(session, workspace)
    source_page = await _source_page(session, workspace, source)

    item = await advisor.run_staleness_detector(session, workspace_id=workspace.workspace_id, threshold_days=90)
    await session.commit()

    assert item is not None
    assert item.kind is ReviewKind.reindex
    assert item.workspace_id == workspace.workspace_id
    assert item.subject_ref == workspace.workspace_id
    assert item.detail["raised_by"] == "advisor"
    assert item.detail["page_count"] == 2
    found_ids = {p["page_id"] for p in item.detail["pages"]}
    assert found_ids == {str(old_page.page_id), str(source_page.page_id)}

    # Signal 2's page is now marked stale, ready for the same reindex dispatch path any
    # other stale page uses.
    status = await session.get(IndexStatus, (source_page.page_id, IndexType.fts))
    assert status.state is IndexState.stale


async def test_run_staleness_detector_no_findings_returns_none(session, workspace):
    await _page(session, workspace, title="Fine Page")
    item = await advisor.run_staleness_detector(session, workspace_id=workspace.workspace_id)
    assert item is None


async def test_run_staleness_detector_skips_if_already_open(session, workspace):
    old_page = await _page(session, workspace, title="Old Page")
    await _make_stale(session, old_page, days_ago=100)

    first = await advisor.run_staleness_detector(session, workspace_id=workspace.workspace_id, threshold_days=90)
    await session.commit()
    assert first is not None

    another_page = await _page(session, workspace, title="Another Old Page")
    await _make_stale(session, another_page, days_ago=100)
    second = await advisor.run_staleness_detector(session, workspace_id=workspace.workspace_id, threshold_days=90)
    assert second is None


# --- resolve_reindex ----------------------------------------------------------------------


async def test_resolve_reindex_now(session, workspace):
    page = await _page(session, workspace, title="Old Page")
    await _make_stale(session, page, days_ago=100)
    item = await advisor.run_staleness_detector(session, workspace_id=workspace.workspace_id, threshold_days=90)
    await session.commit()

    resolved = await advisor.resolve_reindex(session, item=item, action="reindex now", actor="user:admin")
    await session.commit()

    assert resolved.status is ReviewStatus.resolved
    assert resolved.resolved_action == "reindex now"


async def test_resolve_reindex_dismiss(session, workspace):
    page = await _page(session, workspace, title="Old Page")
    await _make_stale(session, page, days_ago=100)
    item = await advisor.run_staleness_detector(session, workspace_id=workspace.workspace_id, threshold_days=90)
    await session.commit()

    resolved = await advisor.resolve_reindex(session, item=item, action="dismiss", actor="user:admin")
    assert resolved.status is ReviewStatus.resolved


async def test_resolve_reindex_rejects_off_peak_scheduling(session, workspace):
    page = await _page(session, workspace, title="Old Page")
    await _make_stale(session, page, days_ago=100)
    item = await advisor.run_staleness_detector(session, workspace_id=workspace.workspace_id, threshold_days=90)
    await session.commit()

    with pytest.raises(advisor.InvalidResolutionError):
        await advisor.resolve_reindex(
            session, item=item, action="schedule for off-peak", actor="user:admin"
        )


async def test_resolve_reindex_rejects_the_wrong_kind(session, workspace):
    item = await review.create(session, kind=ReviewKind.duplicate, subject_ref="x")
    with pytest.raises(advisor.InvalidResolutionError):
        await advisor.resolve_reindex(session, item=item, action="dismiss", actor="user:admin")
