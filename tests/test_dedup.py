"""Duplicate detection at `duplicate_check` (03 §4) — phase1-tasklist step 11."""

import hashlib
import uuid
from datetime import date, datetime, timezone

import pytest

from karpwiki import dedup, ingestion, search, versioning
from karpwiki.dedup import Verdict
from karpwiki.models import (
    ContentShape,
    PageStatus,
    PageType,
    PageVersion,
    PipelineState,
    RawSource,
    RawSourceStatus,
)

BODY = (
    "The payments worker drains its queue before restart. Operators run a rollout restart "
    "and verify that consumer lag returns to zero within five minutes."
)


async def _source(
    session,
    workspace,
    *,
    body=b"content",
    state=PipelineState.classified,
    identity=None,
    version=None,
    modified=None,
    status=RawSourceStatus.active,
):
    source = RawSource(
        workspace_id=workspace.workspace_id,
        object_key=f"/{workspace.workspace_id}/sources/{uuid.uuid4()}/f.md",
        filename="f.md",
        content_hash=hashlib.sha256(body).hexdigest(),
        submitted_by="user:deepak",
        content_shape=ContentShape.structured_data if identity else ContentShape.narrative,
        artifact_identity=identity,
        source_version=version,
        source_modified_at=modified,
        status=status,
        pipeline_state=state,
    )
    session.add(source)
    await session.flush()
    return source


async def _indexed_page(session, workspace, *, title, body):
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


async def test_a_clean_source_reports_no_duplicate(session, workspace):
    source = await _source(session, workspace)
    finding = await dedup.check(session, source=source, summary="Something entirely new.")
    assert finding.verdict is Verdict.none
    assert not finding.blocks


async def test_an_exact_content_match_blocks_with_high_severity(session, workspace):
    existing = await _source(session, workspace, body=b"identical bytes")
    incoming = await _source(session, workspace, body=b"identical bytes")

    finding = await dedup.check(session, source=incoming, summary="x")
    assert finding.verdict is Verdict.exact
    assert (finding.severity, finding.proposed_action) == ("high", "reject")
    assert finding.source_ids == (existing.source_id,)


async def test_a_rejected_source_is_not_a_duplicate_of_anything(session, workspace):
    """A source the admin already declined must not block the next attempt."""
    await _source(session, workspace, body=b"same", status=RawSourceStatus.rejected)
    incoming = await _source(session, workspace, body=b"same")
    assert (await dedup.check(session, source=incoming, summary="x")).verdict is Verdict.none


async def test_a_newer_version_of_a_known_artifact_proposes_supersede(session, workspace):
    old = await _source(session, workspace, body=b"v1", identity="payments-api", version="2.9")
    new = await _source(session, workspace, body=b"v2", identity="payments-api", version="2.10")

    finding = await dedup.check(session, source=new, summary="x")
    assert finding.verdict is Verdict.newer_version
    assert (finding.severity, finding.proposed_action) == ("low", "supersede")
    assert finding.source_ids == (old.source_id,)


def test_versions_compare_numerically_not_lexically():
    """2.10 is newer than 2.9; string ordering would say the opposite."""
    assert dedup._is_older("2.9", "2.10")
    assert not dedup._is_older("2.10", "2.9")


async def test_an_older_arrival_is_not_treated_as_a_new_version(session, workspace):
    """Re-submitting a superseded version is not a supersede — it falls through."""
    await _source(session, workspace, body=b"v2", identity="payments-api", version="2.10")
    older = await _source(session, workspace, body=b"v1", identity="payments-api", version="2.9")

    finding = await dedup.check(session, source=older, summary="x")
    assert finding.verdict is not Verdict.newer_version


async def test_modified_timestamps_settle_it_when_versions_are_absent(session, workspace):
    old = await _source(
        session,
        workspace,
        body=b"a",
        identity="cfg",
        modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    new = await _source(
        session,
        workspace,
        body=b"b",
        identity="cfg",
        modified=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    finding = await dedup.check(session, source=new, summary="x")
    assert finding.verdict is Verdict.newer_version
    assert finding.source_ids == (old.source_id,)


async def test_a_near_duplicate_page_blocks_with_the_matching_pages(session, workspace):
    await _indexed_page(session, workspace, title="Restarting Payments", body=BODY)
    incoming = await _source(session, workspace)

    finding = await dedup.check(session, source=incoming, summary=BODY)
    assert finding.verdict is Verdict.near
    assert (finding.severity, finding.proposed_action) == (
        "medium",
        "merge_or_supersede_or_keep_both",
    )
    assert finding.page_hits[0].path == "concepts/restarting-payments.md"
    assert finding.page_hits[0].score >= dedup.DEFAULT_NEAR_DUPLICATE_SCORE


async def test_an_unrelated_page_does_not_block(session, workspace):
    await _indexed_page(session, workspace, title="Holiday Policy", body="Staff accrue leave.")
    incoming = await _source(session, workspace)
    assert (await dedup.check(session, source=incoming, summary=BODY)).verdict is Verdict.none


async def test_identical_text_scores_one(session, workspace):
    """The normalisation's anchor: a summary identical to the page is maximally similar."""
    await _indexed_page(session, workspace, title="Restarting Payments", body=BODY)
    [hit] = await search.find_similar(
        session, text_body=BODY, workspace_id=workspace.workspace_id
    )
    assert hit.score == 1.0


async def test_the_threshold_is_what_decides_a_near_match(session, workspace):
    """Bracket the real score of a paraphrase rather than assuming where it lands."""
    await _indexed_page(session, workspace, title="Restarting Payments", body=BODY)
    incoming = await _source(session, workspace)
    paraphrase = (
        "Before a restart the payments worker empties its queue; operators then confirm "
        "consumer lag drops back to zero."
    )

    [hit] = await search.find_similar(
        session, text_body=paraphrase, workspace_id=workspace.workspace_id
    )
    assert 0.0 < hit.score < 1.0

    above = await dedup.check(
        session, source=incoming, summary=paraphrase, near_duplicate_score=hit.score + 0.01
    )
    below = await dedup.check(
        session, source=incoming, summary=paraphrase, near_duplicate_score=hit.score - 0.01
    )
    assert above.verdict is Verdict.none
    assert below.verdict is Verdict.near


async def test_duplicate_check_needs_a_resolved_workspace(session, workspace):
    unrouted = await _source(session, workspace)
    unrouted.workspace_id = None
    with pytest.raises(ValueError, match="after a workspace is resolved"):
        await dedup.check(session, source=unrouted, summary="x")


async def test_similarity_never_looks_outside_the_workspace(session, workspace, other_workspace):
    await _indexed_page(session, other_workspace, title="Restarting Payments", body=BODY)
    incoming = await _source(session, workspace)
    assert (await dedup.check(session, source=incoming, summary=BODY)).verdict is Verdict.none


# --- routing (03 §4 + §7) -------------------------------------------------------------


async def test_a_clean_source_proceeds_under_auto_policy(session, workspace):
    source = await _source(session, workspace)
    state = await ingestion.check_duplicates(
        session, source=source, summary="Nothing alike.", ingestion_policy="auto"
    )
    assert state is PipelineState.ingesting


async def test_a_clean_source_still_waits_under_gated_policy(session, workspace):
    source = await _source(session, workspace)
    state = await ingestion.check_duplicates(
        session, source=source, summary="Nothing alike.", ingestion_policy="gated"
    )
    assert state is PipelineState.pending_review


async def test_a_duplicate_blocks_even_under_auto_policy(session, workspace):
    """03 §4: scores above threshold always block; policy governs only the clean path."""
    await _indexed_page(session, workspace, title="Restarting Payments", body=BODY)
    source = await _source(session, workspace)

    state = await ingestion.check_duplicates(
        session, source=source, summary=BODY, ingestion_policy="auto"
    )
    assert state is PipelineState.pending_review

    from karpwiki import pipeline

    last = (await pipeline.history(session, source.source_id))[-1]
    assert last.detail["reason"] == "duplicate: near"
    assert last.detail["similar_pages"][0]["path"] == "concepts/restarting-payments.md"
