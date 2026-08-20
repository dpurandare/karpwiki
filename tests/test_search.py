"""Full-Text Index (02 §4, 04 §1-3, §7-8) — phase1-tasklist steps 16-18."""

import uuid
from datetime import date

import pytest

from karpwiki import search, versioning
from karpwiki.models import IndexState, IndexStatus, IndexType, PageStatus, PageType, PageVersion


async def _page(
    session,
    workspace,
    *,
    title,
    body,
    description=None,
    path=None,
    status=PageStatus.published,
    page_type=PageType.concept,
    tags=None,
    page_date=None,
):
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=path or f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=page_type,
        title=title,
        description=description or f"About {title}.",
        date=page_date or date(2026, 8, 14),
        tags=tags or ["a", "b"],
        body=body,
        author="system:curator",
        status=status,
    )
    version = await session.get(PageVersion, page.current_version_id)
    await search.index_page(session, page=page, version=version)
    return page


async def test_indexing_marks_the_page_indexed(session, workspace):
    page = await _page(session, workspace, title="Retry Backoff", body="Exponential backoff.")
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    assert status.state is IndexState.indexed
    assert status.last_content_version == page.current_version_id


async def test_search_finds_a_page_by_its_body(session, workspace):
    await _page(session, workspace, title="Retry Backoff", body="Use exponential backoff and jitter.")
    await _page(session, workspace, title="Payments Ledger", body="Double-entry accounting rules.")

    hits = await search.search(session, query="jitter", workspace_ids=[workspace.workspace_id])
    assert [h.path for h in hits] == ["concepts/retry-backoff.md"]


async def test_title_matches_outrank_body_matches(session, workspace):
    """The title is weighted above the body, which is what makes 04 §3's catalog boost
    expressible in the index rather than bolted on afterwards."""
    titled = await _page(session, workspace, title="Jitter", body="Unrelated prose here.")
    await _page(session, workspace, title="Something Else", body="We mention jitter in passing.")

    hits = await search.search(session, query="jitter", workspace_ids=[workspace.workspace_id])
    assert len(hits) == 2
    assert hits[0].page_id == titled.page_id
    assert hits[0].score > hits[1].score


async def test_description_weight_ranks_a_description_hit_over_a_body_only_hit(session, workspace):
    """Ordinary content-quality weighting (title > description > body,
    `search.index_page`'s own tsvector) — independent of, and not to be confused with, the
    real index.md catalog-match boost tested separately below
    (phase3-tasklist.md step 60)."""
    described = await _page(
        session,
        workspace,
        title="Retry Backoff",
        description="Covers exponential jitter for retrying failed requests.",
        body="See the referenced RFC for details.",
    )
    await _page(
        session,
        workspace,
        title="Payments Ledger",
        description="Double-entry accounting rules.",
        body="Occasionally a retry introduces jitter into settlement timing.",
    )

    hits = await search.search(session, query="jitter", workspace_ids=[workspace.workspace_id])
    assert len(hits) == 2
    assert hits[0].page_id == described.page_id
    assert hits[0].score > hits[1].score


async def _index_md(session, workspace, *, body):
    """A real index.md page — `versioning.create_page` runs `page_links.sync`, so any
    markdown link in `body` becomes a real `page_link` row, the structural fact
    `search.search`'s catalog-match boost joins against."""
    return await _page(
        session, workspace, title=f"{workspace.workspace_id} Index", path="index.md",
        page_type=PageType.index, body=body, description="Workspace index page.",
    )


async def test_catalog_match_boost_ranks_a_catalogued_page_above_an_uncatalogued_one(
    session, workspace
):
    """04 §3: "a query matching a page's catalog entry gets a ranking boost for that
    page" — a real join against a real index.md `page_link`, not the description weight
    tier tested above. Both candidate pages share identical body text (so their baseline
    `ts_rank_cd` is the same); only the catalogued one's title+description embed the
    query term, and only it is linked from a real index.md."""
    catalogued = await _page(
        session,
        workspace,
        title="Widget Alpha",
        description="A special automated retry mechanism for widgets.",
        body="Supporting detail.",
    )
    uncatalogued = await _page(
        session,
        workspace,
        title="Widget Beta",
        # Identical description text on purpose — isolates the catalog-match boost from
        # the description-weight tier, which would otherwise inflate both hits equally.
        description="A special automated retry mechanism for widgets.",
        body="Supporting detail.",
    )
    await _index_md(
        session,
        workspace,
        body=(
            "## Concepts\n\n"
            "- [Widget Alpha](concepts/widget-alpha.md) — A special automated retry mechanism "
            "for widgets.\n"
        ),
    )

    hits = await search.search(session, query="retry mechanism", workspace_ids=[workspace.workspace_id])
    by_id = {h.page_id: h for h in hits}
    assert catalogued.page_id in by_id
    assert uncatalogued.page_id in by_id
    assert by_id[catalogued.page_id].score > by_id[uncatalogued.page_id].score
    # Identical description text on both, so the ratio between them isolates exactly
    # CATALOG_MATCH_BOOST — not some other, unrelated score difference.
    assert by_id[catalogued.page_id].score / by_id[uncatalogued.page_id].score == pytest.approx(
        search.CATALOG_MATCH_BOOST
    )


async def test_catalog_match_boost_requires_the_query_to_match_that_pages_own_entry(
    session, workspace, other_workspace
):
    """Being linked from index.md alone isn't enough — the query must also match *this*
    page's own title+description (the text its catalog entry actually holds), not just
    match index.md somewhere else. Otherwise every catalogued page in a workspace would be
    boosted by any query that matched any one catalog entry. Proven by comparing the exact
    same content catalogued (in `workspace`) against an identical control page with no
    index.md at all (in `other_workspace`) — equal scores means no boost was applied."""
    catalogued_but_not_matching = await _page(
        session,
        workspace,
        title="Widget Gamma",
        description="Nothing to do with the query term.",
        body="This describes a mechanism in some depth with supporting detail.",
    )
    await _index_md(
        session,
        workspace,
        body=(
            "## Concepts\n\n"
            "- [Widget Gamma](concepts/widget-gamma.md) — Nothing to do with the query term.\n"
        ),
    )
    control = await _page(
        session,
        other_workspace,
        title="Widget Gamma",
        description="Nothing to do with the query term.",
        body="This describes a mechanism in some depth with supporting detail.",
    )

    catalogued_hits = await search.search(session, query="mechanism", workspace_ids=[workspace.workspace_id])
    control_hits = await search.search(session, query="mechanism", workspace_ids=[other_workspace.workspace_id])

    catalogued_score = {h.page_id: h for h in catalogued_hits}[catalogued_but_not_matching.page_id].score
    control_score = {h.page_id: h for h in control_hits}[control.page_id].score
    assert catalogued_score == pytest.approx(control_score)


async def test_search_is_workspace_scoped(session, workspace):
    await _page(session, workspace, title="Retry Backoff", body="jitter")
    elsewhere = await search.search(
        session, query="jitter", workspace_ids=["some-other-workspace"]
    )
    assert elsewhere == []


async def test_drafts_are_excluded_by_default(session, workspace):
    await _page(
        session, workspace, title="Draft Note", body="jitter", status=PageStatus.draft
    )
    assert await search.search(session, query="jitter", workspace_ids=[workspace.workspace_id]) == []

    included = await search.search(
        session, query="jitter", workspace_ids=[workspace.workspace_id], include_drafts=True
    )
    assert len(included) == 1


async def test_an_empty_query_returns_nothing_rather_than_everything(session, workspace):
    await _page(session, workspace, title="Retry Backoff", body="jitter")
    assert await search.search(session, query="   ", workspace_ids=[workspace.workspace_id]) == []
    assert await search.search(session, query="jitter", workspace_ids=[]) == []


async def test_similarity_scores_a_near_copy_near_one(session, workspace):
    """03 §4's near-match query. The score is normalised against the candidate's self-rank,
    so it is comparable to SCHEMA.md's fixed near_duplicate_score threshold."""
    body = (
        "The payments worker drains its queue before restart. Operators run a rollout "
        "restart and verify that consumer lag returns to zero within five minutes."
    )
    await _page(session, workspace, title="Restarting Payments", body=body)
    await _page(session, workspace, title="Holiday Policy", body="Staff accrue leave monthly.")

    hits = await search.find_similar(
        session, text_body=body, workspace_id=workspace.workspace_id
    )
    assert hits[0].path == "concepts/restarting-payments.md"
    assert hits[0].score > 0.9
    if len(hits) > 1:
        assert hits[1].score < hits[0].score


async def test_similarity_ignores_an_unrelated_document(session, workspace):
    await _page(session, workspace, title="Holiday Policy", body="Staff accrue leave monthly.")
    hits = await search.find_similar(
        session,
        text_body="Kubernetes rollout restart drains the consumer queue.",
        workspace_id=workspace.workspace_id,
    )
    assert hits == []


async def test_similarity_is_workspace_scoped(session, workspace):
    body = "The payments worker drains its queue before restart."
    await _page(session, workspace, title="Restarting Payments", body=body)
    assert await search.find_similar(session, text_body=body, workspace_id="elsewhere") == []


async def test_a_new_version_makes_the_page_stale(session, workspace):
    page = await _page(session, workspace, title="Retry Backoff", body="jitter")
    await search.mark_stale(session, page.page_id)
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    assert status.state is IndexState.stale
    assert page.page_id in await search.pending_pages(session)


async def test_reindexing_replaces_rather_than_duplicates(session, workspace):
    page = await _page(session, workspace, title="Retry Backoff", body="jitter")
    version = await session.get(PageVersion, page.current_version_id)
    await search.index_page(session, page=page, version=version)

    hits = await search.search(session, query="jitter", workspace_ids=[workspace.workspace_id])
    assert len(hits) == 1


async def _unindexed_page(session, workspace, *, title="Retry Backoff", body="jitter"):
    """A page left at index_status `pending` — versioning.create_page's default, before
    anything has called search.index_page/reindex on it."""
    return await versioning.create_page(
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


async def test_a_new_page_starts_pending(session, workspace):
    page = await _unindexed_page(session, workspace)
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    assert status.state is IndexState.pending


async def test_reindex_moves_a_pending_page_to_indexed_and_makes_it_findable(session, workspace):
    page = await _unindexed_page(session, workspace, body="Uses jitter.")

    assert await search.reindex(session, page.page_id) is IndexState.indexed

    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    assert status.state is IndexState.indexed
    assert status.last_indexed_at is not None

    hits = await search.search(session, query="jitter", workspace_ids=[workspace.workspace_id])
    assert [h.page_id for h in hits] == [page.page_id]


async def test_reindex_rejects_a_page_that_is_not_pending_or_stale(session, workspace):
    """02 §7's diagram only admits `indexing` from `pending`/`stale` — calling reindex
    again on an already-`indexed` page is a misuse, not a no-op retry."""
    page = await _unindexed_page(session, workspace)
    await search.reindex(session, page.page_id)

    with pytest.raises(ValueError):
        await search.reindex(session, page.page_id)


async def test_reindex_marks_a_failure_as_error(session, workspace, monkeypatch):
    page = await _unindexed_page(session, workspace)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(search, "index_page", _boom)

    assert await search.reindex(session, page.page_id) is IndexState.error
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    assert status.state is IndexState.error


async def test_reindex_pending_drains_every_pending_page(session, workspace):
    a = await _unindexed_page(session, workspace, title="Alpha", body="jitter")
    b = await _unindexed_page(session, workspace, title="Beta", body="jitter")

    done = await search.reindex_pending(session)
    assert set(done) == {a.page_id, b.page_id}
    assert await search.pending_pages(session) == []

    hits = await search.search(session, query="jitter", workspace_ids=[workspace.workspace_id])
    assert {h.page_id for h in hits} == {a.page_id, b.page_id}


async def test_retry_errored_reopens_a_page_for_the_next_sweep(session, workspace):
    page = await _unindexed_page(session, workspace)
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    status.state = IndexState.error
    await session.flush()

    assert await search.retry_errored(session) == [page.page_id]
    assert status.state is IndexState.pending
    assert page.page_id in await search.pending_pages(session)


# --- result provenance (04 §7) and filters (04 §6) — phase2-tasklist.md step 25 -------


async def test_result_carries_title_page_type_and_excerpt(session, workspace):
    page = await _page(
        session, workspace, title="Retry Backoff", body="Use exponential backoff and jitter."
    )
    hits = await search.search(session, query="jitter", workspace_ids=[workspace.workspace_id])
    assert len(hits) == 1
    assert hits[0].title == "Retry Backoff"
    assert hits[0].page_type == "concept"
    assert "jitter" in hits[0].excerpt.lower()


async def test_result_carries_citations(session, workspace):
    body = "Drain the queue, then restart. [^1]\n\n## Source\n\n[^1]: restart-runbook.pdf, p. 3"
    await _page(session, workspace, title="Restart Runbook", body=body)
    hits = await search.search(session, query="drain", workspace_ids=[workspace.workspace_id])
    assert hits[0].citations == ("[^1]: restart-runbook.pdf, p. 3",)


async def test_a_page_with_no_footnotes_has_no_citations(session, workspace):
    await _page(session, workspace, title="No Citations", body="Just prose, no footnotes.")
    hits = await search.search(session, query="prose", workspace_ids=[workspace.workspace_id])
    assert hits[0].citations == ()


async def test_page_type_filter(session, workspace):
    await _page(session, workspace, title="A Concept", body="shared term", page_type=PageType.concept)
    await _page(session, workspace, title="An Entity", body="shared term", page_type=PageType.entity)

    hits = await search.search(
        session, query="shared", workspace_ids=[workspace.workspace_id], page_types=["entity"]
    )
    assert [h.title for h in hits] == ["An Entity"]


async def test_tags_filter_matches_any(session, workspace):
    await _page(session, workspace, title="Ops Page", body="shared term", tags=["ops", "infra"])
    await _page(session, workspace, title="Legal Page", body="shared term", tags=["legal", "compliance"])

    hits = await search.search(
        session, query="shared", workspace_ids=[workspace.workspace_id], tags=["ops"]
    )
    assert [h.title for h in hits] == ["Ops Page"]


async def test_date_range_filter(session, workspace):
    await _page(session, workspace, title="Old Page", body="shared term", page_date=date(2020, 1, 1))
    await _page(session, workspace, title="New Page", body="shared term", page_date=date(2026, 1, 1))

    hits = await search.search(
        session,
        query="shared",
        workspace_ids=[workspace.workspace_id],
        date_from=date(2025, 1, 1),
    )
    assert [h.title for h in hits] == ["New Page"]

    hits = await search.search(
        session,
        query="shared",
        workspace_ids=[workspace.workspace_id],
        date_to=date(2021, 1, 1),
    )
    assert [h.title for h in hits] == ["Old Page"]


# --- merge_federated (04 §4) — phase2-tasklist.md step 26, pure logic, no I/O ----------


def _result(page_id, workspace_id, score, title="T"):
    return search.SearchResult(
        page_id=page_id,
        workspace_id=workspace_id,
        path=f"concepts/{title.lower()}.md",
        page_type="concept",
        title=title,
        score=score,
        excerpt="...",
        citations=(),
    )


def test_merge_federated_leaves_shared_scores_raw(workspace):
    shared = [_result(uuid.uuid4(), workspace.workspace_id, 12.5)]
    merged = search.merge_federated(shared, [])
    assert merged[0].score == 12.5


def test_merge_federated_normalizes_dedicated_scores_to_unit_range():
    ded_ids = [uuid.uuid4() for _ in range(3)]
    dedicated = [
        _result(ded_ids[0], "ded-ws", 4.0),
        _result(ded_ids[1], "ded-ws", 2.0),
        _result(ded_ids[2], "ded-ws", 1.0),
    ]
    merged = search.merge_federated([], dedicated)
    scores = {r.page_id: r.score for r in merged}
    assert scores[ded_ids[0]] == 1.0  # max -> 1.0
    assert scores[ded_ids[2]] == 0.0  # min -> 0.0
    assert 0.0 < scores[ded_ids[1]] < 1.0


def test_merge_federated_sorts_by_score_descending_across_both():
    shared_id, ded_id = uuid.uuid4(), uuid.uuid4()
    shared = [_result(shared_id, "shared-ws", 0.5)]
    dedicated = [_result(ded_id, "ded-ws", 99.0)]  # normalizes to 1.0, still highest

    merged = search.merge_federated(shared, dedicated)
    assert [r.page_id for r in merged] == [ded_id, shared_id]


def test_merge_federated_ties_break_on_workspace_then_page_id():
    a, b = sorted([uuid.uuid4(), uuid.uuid4()], key=str)
    shared = [_result(a, "workspace-a", 1.0, "A"), _result(b, "workspace-a", 1.0, "B")]
    merged = search.merge_federated(shared, [])
    assert [r.page_id for r in merged] == [a, b]


def test_merge_federated_handles_identical_dedicated_scores():
    """04 §4 doesn't define min-max normalization's zero-range case; mapping every tied
    score to 1.0 keeps them all maximally ranked rather than dividing by zero."""
    ids = [uuid.uuid4(), uuid.uuid4()]
    dedicated = [_result(ids[0], "ded-ws", 7.0), _result(ids[1], "ded-ws", 7.0)]
    merged = search.merge_federated([], dedicated)
    assert all(r.score == 1.0 for r in merged)


def test_merge_federated_handles_empty_inputs():
    assert search.merge_federated([], []) == []
