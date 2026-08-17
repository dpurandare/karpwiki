"""Full-Text Index (02 §4, 04 §1-3, §7-8) — phase1-tasklist steps 16-18."""

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


async def test_catalog_match_boosts_a_description_hit_over_a_body_only_hit(session, workspace):
    """04 §3: a query matching a page's one-line catalog summary (`description`, the same
    content an index.md entry would hold) should rank that page above a page that only
    mentions the term in passing in its body."""
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
