"""Maintenance Advisor detectors (05 §2-5) — phase2-tasklist.md steps 36-40."""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from karpwiki import advisor, objectstore, review, search, versioning
from karpwiki.curate import MergedPage
from karpwiki.models import (
    IndexState,
    IndexStatus,
    IndexType,
    LinkType,
    PageLink,
    PageStatus,
    PageType,
    PageVersion,
    QueryLog,
    RawSource,
    RawSourceStatus,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
)

DUPLICATE_BODY = (
    "The payments worker drains its queue before restart. Operators run a rollout restart "
    "and verify that consumer lag returns to zero within five minutes."
)


async def _indexed_page(session, workspace, *, title, body=DUPLICATE_BODY):
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=PageType.concept,
        title=title,
        description=f"About {title}.",
        date=date(2026, 8, 14),
        tags=["a", "b"],
        body=body,
        author="system:curator",
        status=PageStatus.published,
    )
    version = await session.get(PageVersion, page.current_version_id)
    await search.index_page(session, page=page, version=version)
    return page


async def _page(session, workspace, *, title="Runbook", body="Body text.", page_type=PageType.concept):
    return await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"{'sources' if page_type is PageType.source else 'concepts'}/{title.lower().replace(' ', '-')}.md",
        page_type=page_type,
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


async def _superseded_source(session, workspace, *, filename="notes.md", superseded_days_ago=None):
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
        superseded_at=(
            datetime.now(UTC) - timedelta(days=superseded_days_ago)
            if superseded_days_ago is not None
            else None
        ),
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


# --- find_stale_pages_tiered (05 §2's popularity-tiered refresh) — step 41 ---------------


async def test_find_stale_pages_tiered_flags_a_high_traffic_page_at_the_short_threshold(session, workspace):
    """Stale 100 days, queried recently -> high traffic -> the 90-day bar is enough, even
    though it hasn't crossed the 365-day low-traffic bar."""
    page = await _page(session, workspace, title="Popular Old Page")
    await _make_stale(session, page, days_ago=100)
    session.add(
        QueryLog(
            principal="user:deepak",
            query_text="popular old page",
            resolved_workspaces=[workspace.workspace_id],
            results=[{"page_id": str(page.page_id), "score": 0.9}],
        )
    )
    await session.flush()

    findings = await advisor.find_stale_pages_tiered(
        session, workspace_id=workspace.workspace_id, high_traffic_days=90, low_traffic_days=365
    )
    assert [f.page_id for f in findings] == [page.page_id]


async def test_find_stale_pages_tiered_excludes_a_low_traffic_page_under_the_long_threshold(
    session, workspace
):
    """Stale 100 days, never queried -> low traffic -> needs the 365-day bar, which 100
    days doesn't clear."""
    page = await _page(session, workspace, title="Unpopular Old Page")
    await _make_stale(session, page, days_ago=100)

    findings = await advisor.find_stale_pages_tiered(
        session, workspace_id=workspace.workspace_id, high_traffic_days=90, low_traffic_days=365
    )
    assert findings == []


async def test_find_stale_pages_tiered_flags_a_low_traffic_page_past_the_long_threshold(session, workspace):
    """Stale 400 days, never queried -> low traffic, but 400 > 365 clears the stricter
    bar on its own merits."""
    page = await _page(session, workspace, title="Ancient Unpopular Page")
    await _make_stale(session, page, days_ago=400)

    findings = await advisor.find_stale_pages_tiered(
        session, workspace_id=workspace.workspace_id, high_traffic_days=90, low_traffic_days=365
    )
    assert [f.page_id for f in findings] == [page.page_id]


async def test_find_stale_pages_tiered_uses_config_defaults_when_omitted(session, workspace):
    page = await _page(session, workspace, title="Default Tier Page")
    await _make_stale(session, page, days_ago=100)

    findings = await advisor.find_stale_pages_tiered(session, workspace_id=workspace.workspace_id)
    # Never queried, 100 days < config.STALENESS_LOW_TRAFFIC_DAYS (365) -> excluded,
    # proving the config.py defaults (not some other value) were actually applied.
    assert findings == []


async def test_run_staleness_detector_tiered_true_uses_tiered_thresholds(session, workspace):
    high_traffic = await _page(session, workspace, title="High Traffic Stale Page")
    await _make_stale(session, high_traffic, days_ago=100)
    session.add(
        QueryLog(
            principal="user:deepak",
            query_text="high traffic stale page",
            resolved_workspaces=[workspace.workspace_id],
            results=[{"page_id": str(high_traffic.page_id), "score": 0.9}],
        )
    )
    low_traffic = await _page(session, workspace, title="Low Traffic Stale Page")
    await _make_stale(session, low_traffic, days_ago=100)
    await session.flush()

    item = await advisor.run_staleness_detector(
        session,
        workspace_id=workspace.workspace_id,
        tiered=True,
        high_traffic_days=90,
        low_traffic_days=365,
    )
    await session.commit()

    assert item is not None
    found_ids = {p["page_id"] for p in item.detail["pages"]}
    assert found_ids == {str(high_traffic.page_id)}


async def test_run_staleness_detector_default_is_not_tiered(session, workspace):
    """Backward compatibility: omitting `tiered` keeps the flat, pre-step-41 behavior —
    a page stale past `threshold_days` is flagged regardless of query traffic."""
    page = await _page(session, workspace, title="Flat Mode Page")
    await _make_stale(session, page, days_ago=100)

    item = await advisor.run_staleness_detector(session, workspace_id=workspace.workspace_id, threshold_days=90)
    await session.commit()

    assert item is not None
    assert {p["page_id"] for p in item.detail["pages"]} == {str(page.page_id)}


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


# --- Superseded-Source Detector (05 §4) — step 37 ----------------------------------------


async def test_find_superseded_sources_only_past_retention(session, workspace):
    old = await _superseded_source(session, workspace, filename="old.md", superseded_days_ago=200)
    recent = await _superseded_source(session, workspace, filename="recent.md", superseded_days_ago=30)

    findings = await advisor.find_superseded_sources_past_retention(
        session, workspace_id=workspace.workspace_id, retention_days=180
    )

    assert [f.source_id for f in findings] == [old.source_id]
    assert findings[0].filename == "old.md"


async def test_find_superseded_sources_skips_active_sources(session, workspace):
    source_id = uuid.uuid4()
    key = f"/{workspace.workspace_id}/sources/{source_id}/f.md"
    objectstore.write_bytes(key, b"x")
    active = RawSource(
        source_id=source_id,
        workspace_id=workspace.workspace_id,
        object_key=key,
        filename="f.md",
        content_hash="deadbeef",
        submitted_by="user:deepak",
        status=RawSourceStatus.active,
    )
    session.add(active)
    await session.flush()

    findings = await advisor.find_superseded_sources_past_retention(
        session, workspace_id=workspace.workspace_id, retention_days=0
    )
    assert findings == []


async def test_find_superseded_sources_skips_no_timestamp(session, workspace):
    """A source superseded before `superseded_at` existed (or by any path that predates
    this column) has nothing to check against — skipped, not assumed either way."""
    await _superseded_source(session, workspace, superseded_days_ago=None)
    findings = await advisor.find_superseded_sources_past_retention(
        session, workspace_id=workspace.workspace_id, retention_days=0
    )
    assert findings == []


async def test_run_superseded_source_detector_creates_prune_item(session, workspace):
    old = await _superseded_source(session, workspace, filename="old.md", superseded_days_ago=200)

    item = await advisor.run_superseded_source_detector(
        session, workspace_id=workspace.workspace_id, retention_days=180
    )
    await session.commit()

    assert item is not None
    assert item.kind is ReviewKind.prune
    assert item.workspace_id == workspace.workspace_id
    assert item.proposed_action == "delete superseded source"
    assert item.detail["raised_by"] == "advisor"
    assert item.detail["reason"] == "superseded_source_retention"
    assert item.detail["source_count"] == 1
    assert item.detail["sources"][0]["source_id"] == str(old.source_id)


async def test_run_superseded_source_detector_no_findings_returns_none(session, workspace):
    await _superseded_source(session, workspace, superseded_days_ago=30)
    item = await advisor.run_superseded_source_detector(session, workspace_id=workspace.workspace_id)
    assert item is None


async def test_run_superseded_source_detector_skips_if_already_open(session, workspace):
    await _superseded_source(session, workspace, filename="a.md", superseded_days_ago=200)
    first = await advisor.run_superseded_source_detector(
        session, workspace_id=workspace.workspace_id, retention_days=180
    )
    await session.commit()
    assert first is not None

    await _superseded_source(session, workspace, filename="b.md", superseded_days_ago=200)
    second = await advisor.run_superseded_source_detector(
        session, workspace_id=workspace.workspace_id, retention_days=180
    )
    assert second is None


async def test_resolve_prune_delete_superseded_source_archives_it(session, workspace):
    old = await _superseded_source(session, workspace, filename="old.md", superseded_days_ago=200)
    item = await advisor.run_superseded_source_detector(
        session, workspace_id=workspace.workspace_id, retention_days=180
    )
    await session.commit()

    resolved = await advisor.resolve_prune(
        session, item=item, action="delete superseded source", actor="user:admin"
    )
    await session.commit()

    assert resolved.status is ReviewStatus.resolved
    source = await session.get(RawSource, old.source_id)
    await session.refresh(source)
    assert source.status is RawSourceStatus.archived


async def test_resolve_prune_dismiss_leaves_source_untouched(session, workspace):
    old = await _superseded_source(session, workspace, filename="old.md", superseded_days_ago=200)
    item = await advisor.run_superseded_source_detector(
        session, workspace_id=workspace.workspace_id, retention_days=180
    )
    await session.commit()

    await advisor.resolve_prune(session, item=item, action="dismiss", actor="user:admin")

    source = await session.get(RawSource, old.source_id)
    await session.refresh(source)
    assert source.status is RawSourceStatus.superseded


async def test_resolve_prune_rejects_the_wrong_kind(session, workspace):
    item = await review.create(session, kind=ReviewKind.duplicate, subject_ref="x")
    with pytest.raises(advisor.InvalidResolutionError):
        await advisor.resolve_prune(session, item=item, action="dismiss", actor="user:admin")


async def test_resolve_prune_rejects_an_unbuilt_reason(session, workspace):
    """Every reason `05` §4 names is built as of step 40 — this checks the fallback
    `else` branch itself still rejects whatever isn't one of them."""
    item = await review.create(
        session,
        kind=ReviewKind.prune,
        subject_ref=workspace.workspace_id,
        workspace_id=workspace.workspace_id,
        detail={"reason": "some_future_reason"},
    )
    with pytest.raises(advisor.InvalidResolutionError):
        await advisor.resolve_prune(session, item=item, action="archive page", actor="user:admin")


# --- Existing-Content Duplicate Detector (05 §5) — step 38 -------------------------------


async def test_find_similar_page_pairs_finds_identical_pages(session, workspace):
    older = await _indexed_page(session, workspace, title="Restarting Payments")
    newer = await _indexed_page(session, workspace, title="Payments Restart Runbook")

    findings = await advisor.find_similar_page_pairs(session, workspace_id=workspace.workspace_id)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.score == 1.0
    # Older page (created first) is primary.
    assert finding.primary_page_id == older.page_id
    assert finding.duplicate_page_id == newer.page_id


async def test_find_similar_page_pairs_ignores_unrelated_pages(session, workspace):
    await _indexed_page(session, workspace, title="Restarting Payments")
    await _indexed_page(session, workspace, title="Holiday Policy", body="Staff accrue leave.")

    findings = await advisor.find_similar_page_pairs(session, workspace_id=workspace.workspace_id)
    assert findings == []


async def test_find_similar_page_pairs_ignores_archived_pages(session, workspace):
    older = await _indexed_page(session, workspace, title="Restarting Payments")
    newer = await _indexed_page(session, workspace, title="Payments Restart Runbook")
    newer.status = PageStatus.archived
    await session.flush()

    findings = await advisor.find_similar_page_pairs(session, workspace_id=workspace.workspace_id)
    assert findings == []


async def test_run_existing_content_duplicate_detector_creates_one_item(session, workspace):
    older = await _indexed_page(session, workspace, title="Restarting Payments")
    newer = await _indexed_page(session, workspace, title="Payments Restart Runbook")

    items = await advisor.run_existing_content_duplicate_detector(
        session, workspace_id=workspace.workspace_id
    )
    await session.commit()

    assert len(items) == 1
    item = items[0]
    assert item.kind is ReviewKind.duplicate
    assert item.subject_ref == str(older.page_id)
    assert item.detail["raised_by"] == "advisor"
    assert item.detail["primary_page_id"] == str(older.page_id)
    assert item.detail["duplicate_page_id"] == str(newer.page_id)
    assert item.detail["score"] == 1.0


async def test_run_existing_content_duplicate_detector_skips_an_open_pair(session, workspace):
    await _indexed_page(session, workspace, title="Restarting Payments")
    await _indexed_page(session, workspace, title="Payments Restart Runbook")

    first = await advisor.run_existing_content_duplicate_detector(
        session, workspace_id=workspace.workspace_id
    )
    await session.commit()
    assert len(first) == 1

    second = await advisor.run_existing_content_duplicate_detector(
        session, workspace_id=workspace.workspace_id
    )
    assert second == []


async def test_resolve_existing_duplicate_keep_both_leaves_pages_untouched(session, workspace):
    older = await _indexed_page(session, workspace, title="Restarting Payments")
    newer = await _indexed_page(session, workspace, title="Payments Restart Runbook")
    [item] = await advisor.run_existing_content_duplicate_detector(
        session, workspace_id=workspace.workspace_id
    )
    await session.commit()

    await advisor.resolve_existing_duplicate(session, item=item, action="keep_both", actor="user:admin")

    for page in (older, newer):
        p = await session.get(type(page), page.page_id)
        assert p.status is PageStatus.published


async def test_resolve_existing_duplicate_reject_leaves_pages_untouched(session, workspace):
    await _indexed_page(session, workspace, title="Restarting Payments")
    await _indexed_page(session, workspace, title="Payments Restart Runbook")
    [item] = await advisor.run_existing_content_duplicate_detector(
        session, workspace_id=workspace.workspace_id
    )
    await session.commit()

    resolved = await advisor.resolve_existing_duplicate(
        session, item=item, action="reject", actor="user:admin"
    )
    assert resolved is None
    assert item.status is ReviewStatus.resolved
    assert item.resolved_action == "reject"


async def test_resolve_existing_duplicate_supersede_archives_the_duplicate(session, workspace):
    older = await _indexed_page(session, workspace, title="Restarting Payments")
    newer = await _indexed_page(session, workspace, title="Payments Restart Runbook")
    [item] = await advisor.run_existing_content_duplicate_detector(
        session, workspace_id=workspace.workspace_id
    )
    await session.commit()

    await advisor.resolve_existing_duplicate(session, item=item, action="supersede", actor="user:admin")

    primary = await session.get(type(older), older.page_id)
    duplicate = await session.get(type(newer), newer.page_id)
    assert primary.status is PageStatus.published
    assert duplicate.status is PageStatus.archived


async def test_resolve_existing_duplicate_merge_writes_a_version_and_archives_the_duplicate(
    session, workspace
):
    older = await _indexed_page(session, workspace, title="Restarting Payments")
    newer = await _indexed_page(session, workspace, title="Payments Restart Runbook")
    [item] = await advisor.run_existing_content_duplicate_detector(
        session, workspace_id=workspace.workspace_id
    )
    await session.commit()
    original_version_id = older.current_version_id

    async def _fake_merge(**_kwargs):
        return MergedPage(body="Merged body from both pages.", change_summary="Merged a duplicate.")

    await advisor.resolve_existing_duplicate(
        session, item=item, action="merge", actor="user:admin", call=_fake_merge
    )
    await session.commit()

    primary = await session.get(type(older), older.page_id)
    await session.refresh(primary)
    duplicate = await session.get(type(newer), newer.page_id)
    await session.refresh(duplicate)

    assert primary.current_version_id != original_version_id
    new_version = await session.get(PageVersion, primary.current_version_id)
    assert "Merged body from both pages." in new_version.content
    assert duplicate.status is PageStatus.archived


async def test_resolve_existing_duplicate_rejects_ingest_time_items(session, workspace):
    """An ingest-time `duplicate` item (no `raised_by=advisor` tag) must not route here —
    `ingestion.resolve_duplicate` owns those, untouched."""
    item = await review.create(session, kind=ReviewKind.duplicate, subject_ref=str(uuid.uuid4()))
    with pytest.raises(advisor.InvalidResolutionError):
        await advisor.resolve_existing_duplicate(
            session, item=item, action="keep_both", actor="user:admin"
        )


async def test_resolve_existing_duplicate_rejects_an_unsupported_action(session, workspace):
    await _indexed_page(session, workspace, title="Restarting Payments")
    await _indexed_page(session, workspace, title="Payments Restart Runbook")
    [item] = await advisor.run_existing_content_duplicate_detector(
        session, workspace_id=workspace.workspace_id
    )
    await session.commit()

    with pytest.raises(advisor.InvalidResolutionError):
        await advisor.resolve_existing_duplicate(
            session, item=item, action="bogus", actor="user:admin"
        )


# --- Orphan/Low-Traffic Detector (05 §2, §4) — step 39 ------------------------------------


async def test_find_orphaned_pages_finds_an_unlinked_unqueried_page(session, workspace):
    page = await _page(session, workspace, title="Forgotten Page")
    findings = await advisor.find_orphaned_pages(session, workspace_id=workspace.workspace_id)
    assert [f.page_id for f in findings] == [page.page_id]


async def test_find_orphaned_pages_excludes_a_page_with_an_inbound_link(session, workspace):
    target = await _page(session, workspace, title="Linked Page")
    linker = await _page(session, workspace, title="Linker Page")
    session.add(PageLink(from_page_id=linker.page_id, to_page_id=target.page_id, link_type=LinkType.cross_reference))
    await session.flush()

    findings = await advisor.find_orphaned_pages(session, workspace_id=workspace.workspace_id)
    found_ids = {f.page_id for f in findings}
    assert target.page_id not in found_ids
    # `linker` itself has no inbound links and wasn't queried either — it's a legitimate
    # finding in its own right, just not the one this test is about.


async def test_find_orphaned_pages_excludes_a_recently_queried_page(session, workspace):
    page = await _page(session, workspace, title="Searched Page")
    session.add(
        QueryLog(
            principal="user:deepak",
            query_text="searched page",
            resolved_workspaces=[workspace.workspace_id],
            results=[{"page_id": str(page.page_id), "score": 0.9}],
        )
    )
    await session.flush()

    findings = await advisor.find_orphaned_pages(session, workspace_id=workspace.workspace_id)
    assert findings == []


async def test_find_orphaned_pages_ignores_a_query_outside_the_lookback_window(session, workspace):
    page = await _page(session, workspace, title="Once Popular Page")
    entry = QueryLog(
        principal="user:deepak",
        query_text="once popular page",
        resolved_workspaces=[workspace.workspace_id],
        results=[{"page_id": str(page.page_id), "score": 0.9}],
    )
    session.add(entry)
    await session.flush()
    entry.created_at = datetime.now(UTC) - timedelta(days=200)
    await session.flush()

    findings = await advisor.find_orphaned_pages(
        session, workspace_id=workspace.workspace_id, lookback_days=90
    )
    assert [f.page_id for f in findings] == [page.page_id]


async def test_find_orphaned_pages_ignores_structural_and_source_page_types(session, workspace):
    await _page(session, workspace, title="Overview", page_type=PageType.overview)
    await _page(session, workspace, title="Log", page_type=PageType.log)
    await _page(session, workspace, title="Index", page_type=PageType.index)
    await _page(session, workspace, title="Source Page", page_type=PageType.source)

    findings = await advisor.find_orphaned_pages(session, workspace_id=workspace.workspace_id)
    assert findings == []


async def test_run_orphan_detector_creates_a_prune_item(session, workspace):
    page = await _page(session, workspace, title="Forgotten Page")
    item = await advisor.run_orphan_detector(session, workspace_id=workspace.workspace_id)
    await session.commit()

    assert item is not None
    assert item.kind is ReviewKind.prune
    assert item.proposed_action == "archive page"
    assert item.detail["raised_by"] == "advisor"
    assert item.detail["reason"] == "orphaned"
    assert item.detail["pages"] == [{"page_id": str(page.page_id), "path": page.path}]


async def test_run_orphan_detector_coexists_with_an_open_superseded_source_item(session, workspace):
    """The reason-scoped `_open_prune_item` fix: an open `superseded_source_retention` item
    must not block a genuinely different `orphaned` finding."""
    old_source = await _superseded_source(session, workspace, filename="old.md", superseded_days_ago=200)
    superseded_item = await advisor.run_superseded_source_detector(
        session, workspace_id=workspace.workspace_id, retention_days=180
    )
    await session.commit()
    assert superseded_item is not None

    await _page(session, workspace, title="Forgotten Page")
    orphan_item = await advisor.run_orphan_detector(session, workspace_id=workspace.workspace_id)
    assert orphan_item is not None
    assert orphan_item.review_id != superseded_item.review_id


async def test_run_orphan_detector_skips_if_orphaned_item_already_open(session, workspace):
    await _page(session, workspace, title="Forgotten Page")
    first = await advisor.run_orphan_detector(session, workspace_id=workspace.workspace_id)
    await session.commit()
    assert first is not None

    await _page(session, workspace, title="Another Forgotten Page")
    second = await advisor.run_orphan_detector(session, workspace_id=workspace.workspace_id)
    assert second is None


async def test_resolve_prune_archive_page_archives_it(session, workspace):
    page = await _page(session, workspace, title="Forgotten Page")
    item = await advisor.run_orphan_detector(session, workspace_id=workspace.workspace_id)
    await session.commit()

    await advisor.resolve_prune(session, item=item, action="archive page", actor="user:admin")
    await session.commit()

    refreshed = await session.get(type(page), page.page_id)
    await session.refresh(refreshed)
    assert refreshed.status is PageStatus.archived


async def test_resolve_prune_orphaned_dismiss_leaves_page_untouched(session, workspace):
    page = await _page(session, workspace, title="Forgotten Page")
    item = await advisor.run_orphan_detector(session, workspace_id=workspace.workspace_id)
    await session.commit()

    await advisor.resolve_prune(session, item=item, action="dismiss", actor="user:admin")

    refreshed = await session.get(type(page), page.page_id)
    await session.refresh(refreshed)
    assert refreshed.status is PageStatus.published


async def test_resolve_prune_orphaned_rejects_delete_superseded_source_action(session, workspace):
    """Actions are scoped per reason — a superseded-source action doesn't apply here."""
    await _page(session, workspace, title="Forgotten Page")
    item = await advisor.run_orphan_detector(session, workspace_id=workspace.workspace_id)
    await session.commit()

    with pytest.raises(advisor.InvalidResolutionError):
        await advisor.resolve_prune(
            session, item=item, action="delete superseded source", actor="user:admin"
        )


# --- Contradiction Detector (05 §2, "Curator's periodic lint pass") — step 40 ------------

# Scores 0.5 each direction against `search.find_similar` — squarely inside the default
# [0.35, 0.60) candidate band, symmetric so either page's own scan finds the pair.
CONTRADICTION_BODY_A = (
    "Restart the payments worker daily using the automated recovery script during "
    "scheduled maintenance windows to clear the queue backlog."
)
CONTRADICTION_BODY_B = (
    "Restart the payments worker weekly using a manual failover checklist during "
    "unplanned incident response to clear the queue backlog."
)


def _fake_judgment(*, contradicts, outdated_page="a", explanation="Conflicting claim."):
    async def _call(**_kwargs):
        return advisor.ContradictionJudgment(
            contradicts=contradicts, outdated_page=outdated_page, explanation=explanation
        )

    return _call


async def test_find_contradiction_candidates_finds_a_same_topic_pair(session, workspace):
    a = await _indexed_page(session, workspace, title="Daily Restart", body=CONTRADICTION_BODY_A)
    b = await _indexed_page(session, workspace, title="Weekly Restart", body=CONTRADICTION_BODY_B)

    candidates = await advisor.find_contradiction_candidates(session, workspace_id=workspace.workspace_id)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert {candidate.page_a_id, candidate.page_b_id} == {a.page_id, b.page_id}
    assert candidate.score == 0.5


async def test_find_contradiction_candidates_excludes_near_duplicates(session, workspace):
    """Score >= max_similarity (dedup's near-duplicate threshold) is step 38's territory,
    not this detector's."""
    await _indexed_page(session, workspace, title="Restarting Payments")
    await _indexed_page(session, workspace, title="Payments Restart Runbook")

    candidates = await advisor.find_contradiction_candidates(session, workspace_id=workspace.workspace_id)
    assert candidates == []


async def test_find_contradiction_candidates_excludes_unrelated_pages(session, workspace):
    await _indexed_page(session, workspace, title="Restarting Payments")
    await _indexed_page(session, workspace, title="Holiday Policy", body="Staff accrue leave.")

    candidates = await advisor.find_contradiction_candidates(session, workspace_id=workspace.workspace_id)
    assert candidates == []


async def test_run_contradiction_detector_creates_a_prune_item(session, workspace):
    a = await _indexed_page(session, workspace, title="Daily Restart", body=CONTRADICTION_BODY_A)
    b = await _indexed_page(session, workspace, title="Weekly Restart", body=CONTRADICTION_BODY_B)

    items = await advisor.run_contradiction_detector(
        session,
        workspace_id=workspace.workspace_id,
        call=_fake_judgment(contradicts=True, outdated_page="a"),
    )
    await session.commit()

    assert len(items) == 1
    item = items[0]
    assert item.kind is ReviewKind.prune
    assert item.subject_ref == str(a.page_id)
    assert item.detail["raised_by"] == "advisor"
    assert item.detail["reason"] == "contradicted_by"
    assert item.detail["page_id"] == str(a.page_id)
    assert item.detail["contradicting_page_id"] == str(b.page_id)
    assert item.detail["explanation"] == "Conflicting claim."


async def test_run_contradiction_detector_flags_the_other_page_when_outdated_is_b(session, workspace):
    a = await _indexed_page(session, workspace, title="Daily Restart", body=CONTRADICTION_BODY_A)
    b = await _indexed_page(session, workspace, title="Weekly Restart", body=CONTRADICTION_BODY_B)

    [item] = await advisor.run_contradiction_detector(
        session,
        workspace_id=workspace.workspace_id,
        call=_fake_judgment(contradicts=True, outdated_page="b"),
    )

    assert item.detail["page_id"] == str(b.page_id)
    assert item.detail["contradicting_page_id"] == str(a.page_id)


async def test_run_contradiction_detector_no_findings_when_not_contradicting(session, workspace):
    await _indexed_page(session, workspace, title="Daily Restart", body=CONTRADICTION_BODY_A)
    await _indexed_page(session, workspace, title="Weekly Restart", body=CONTRADICTION_BODY_B)

    items = await advisor.run_contradiction_detector(
        session, workspace_id=workspace.workspace_id, call=_fake_judgment(contradicts=False)
    )
    assert items == []


async def test_run_contradiction_detector_skips_an_open_pair(session, workspace):
    await _indexed_page(session, workspace, title="Daily Restart", body=CONTRADICTION_BODY_A)
    await _indexed_page(session, workspace, title="Weekly Restart", body=CONTRADICTION_BODY_B)

    first = await advisor.run_contradiction_detector(
        session, workspace_id=workspace.workspace_id, call=_fake_judgment(contradicts=True)
    )
    await session.commit()
    assert len(first) == 1

    second = await advisor.run_contradiction_detector(
        session, workspace_id=workspace.workspace_id, call=_fake_judgment(contradicts=True)
    )
    assert second == []


async def test_run_contradiction_detector_respects_max_checks(session, workspace):
    """Three pages -> two candidate pairs in the contradiction band (the two same-body
    pages score 1.0 against each other, excluded); capped to one LLM check."""
    calls = []

    async def _counting_call(**_kwargs):
        calls.append(1)
        return advisor.ContradictionJudgment(contradicts=True, outdated_page="a", explanation="x")

    await _indexed_page(session, workspace, title="Daily Restart A", body=CONTRADICTION_BODY_A)
    await _indexed_page(session, workspace, title="Weekly Restart A", body=CONTRADICTION_BODY_B)
    await _indexed_page(session, workspace, title="Daily Restart B", body=CONTRADICTION_BODY_A)

    await advisor.run_contradiction_detector(
        session, workspace_id=workspace.workspace_id, max_checks=1, call=_counting_call
    )
    assert len(calls) == 1


async def test_resolve_prune_contradicted_by_archives_the_flagged_page(session, workspace):
    a = await _indexed_page(session, workspace, title="Daily Restart", body=CONTRADICTION_BODY_A)
    b = await _indexed_page(session, workspace, title="Weekly Restart", body=CONTRADICTION_BODY_B)
    [item] = await advisor.run_contradiction_detector(
        session,
        workspace_id=workspace.workspace_id,
        call=_fake_judgment(contradicts=True, outdated_page="a"),
    )
    await session.commit()

    await advisor.resolve_prune(session, item=item, action="archive page", actor="user:admin")

    flagged = await session.get(type(a), a.page_id)
    other = await session.get(type(b), b.page_id)
    assert flagged.status is PageStatus.archived
    assert other.status is PageStatus.published


async def test_resolve_prune_contradicted_by_dismiss_leaves_pages_untouched(session, workspace):
    a = await _indexed_page(session, workspace, title="Daily Restart", body=CONTRADICTION_BODY_A)
    b = await _indexed_page(session, workspace, title="Weekly Restart", body=CONTRADICTION_BODY_B)
    [item] = await advisor.run_contradiction_detector(
        session, workspace_id=workspace.workspace_id, call=_fake_judgment(contradicts=True)
    )
    await session.commit()

    await advisor.resolve_prune(session, item=item, action="dismiss", actor="user:admin")

    for page in (a, b):
        p = await session.get(type(page), page.page_id)
        assert p.status is PageStatus.published


async def test_resolve_prune_contradicted_by_rejects_an_unsupported_action(session, workspace):
    await _indexed_page(session, workspace, title="Daily Restart", body=CONTRADICTION_BODY_A)
    await _indexed_page(session, workspace, title="Weekly Restart", body=CONTRADICTION_BODY_B)
    [item] = await advisor.run_contradiction_detector(
        session, workspace_id=workspace.workspace_id, call=_fake_judgment(contradicts=True)
    )
    await session.commit()

    with pytest.raises(advisor.InvalidResolutionError):
        await advisor.resolve_prune(session, item=item, action="bogus", actor="user:admin")
