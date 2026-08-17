"""Full-Text Index (02 §4, 04 §1-3) — phase1-tasklist step 16."""

from datetime import date

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
):
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=path or f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=PageType.concept,
        title=title,
        description=description or f"About {title}.",
        date=date(2026, 8, 14),
        tags=["a", "b"],
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
