"""OpenSearch-backed dedicated index (02 §4, 08 §2) — phase2-tasklist.md step 26."""

from datetime import date

from karpwiki import dedicated_index, versioning
from karpwiki.models import PageStatus, PageType, PageVersion


async def _dedicated_page(session, workspace, *, title, body, tags=None):
    page = await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=PageType.concept,
        title=title,
        description=f"About {title}.",
        date=date(2026, 8, 14),
        tags=tags or ["a", "b"],
        body=body,
        author="system:curator",
        status=PageStatus.published,
    )
    version = await session.get(PageVersion, page.current_version_id)
    await dedicated_index.index_page(page=page, version=version)
    return page


async def test_index_and_search_round_trip(session, dedicated_workspace):
    page = await _dedicated_page(
        session, dedicated_workspace, title="Vendor Contract", body="Net-30 payment terms apply."
    )
    hits = await dedicated_index.search(
        query="payment terms", workspace_ids=[dedicated_workspace.workspace_id]
    )
    assert [h.page_id for h in hits] == [page.page_id]
    assert hits[0].title == "Vendor Contract"
    assert hits[0].page_type == "concept"
    assert "terms" in hits[0].excerpt.lower()


async def test_result_carries_citations(session, dedicated_workspace):
    body = "Net-30 terms apply. [^1]\n\n[^1]: vendor-agreement.pdf, p. 4"
    await _dedicated_page(session, dedicated_workspace, title="Vendor Contract", body=body)
    hits = await dedicated_index.search(
        query="net-30", workspace_ids=[dedicated_workspace.workspace_id]
    )
    assert hits[0].citations == ("[^1]: vendor-agreement.pdf, p. 4",)


async def test_search_is_workspace_scoped(session, dedicated_workspace, other_workspace):
    await _dedicated_page(session, dedicated_workspace, title="Contract A", body="shared term")
    hits = await dedicated_index.search(
        query="shared", workspace_ids=[other_workspace.workspace_id]
    )
    assert hits == []


async def test_tags_and_page_type_filters(session, dedicated_workspace):
    await _dedicated_page(
        session, dedicated_workspace, title="Ops Contract", body="shared term",
        tags=["ops", "infra"],
    )
    await _dedicated_page(
        session, dedicated_workspace, title="Legal Contract", body="shared term",
        tags=["legal", "compliance"],
    )

    hits = await dedicated_index.search(
        query="shared", workspace_ids=[dedicated_workspace.workspace_id], tags=["ops"]
    )
    assert [h.title for h in hits] == ["Ops Contract"]

    hits = await dedicated_index.search(
        query="shared", workspace_ids=[dedicated_workspace.workspace_id], page_types=["entity"]
    )
    assert hits == []


async def test_reindexing_replaces_rather_than_duplicates(session, dedicated_workspace):
    page = await _dedicated_page(session, dedicated_workspace, title="Contract", body="v1 text")
    version = await session.get(PageVersion, page.current_version_id)
    await dedicated_index.index_page(page=page, version=version)

    hits = await dedicated_index.search(
        query="v1", workspace_ids=[dedicated_workspace.workspace_id]
    )
    assert len(hits) == 1


async def test_delete_page_removes_it(session, dedicated_workspace):
    page = await _dedicated_page(session, dedicated_workspace, title="Contract", body="shared term")
    await dedicated_index.delete_page(page.page_id)
    hits = await dedicated_index.search(
        query="shared", workspace_ids=[dedicated_workspace.workspace_id]
    )
    assert hits == []


async def test_search_index_page_writes_to_opensearch_for_a_dedicated_workspace(
    session, dedicated_workspace
):
    """search.index_page (the shared entry point every reindex path calls) dispatches to
    OpenSearch for a dedicated workspace, not only search.py's own Postgres table."""
    from karpwiki import search

    page = await versioning.create_page(
        session,
        workspace_id=dedicated_workspace.workspace_id,
        path="concepts/dispatched.md",
        page_type=PageType.concept,
        title="Dispatched Page",
        description="Routed via search.index_page.",
        date=date(2026, 8, 14),
        tags=["a", "b"],
        body="unique dispatch term",
        author="system:curator",
        status=PageStatus.published,
    )
    version = await session.get(PageVersion, page.current_version_id)
    await search.index_page(session, page=page, version=version)

    hits = await dedicated_index.search(
        query="dispatch", workspace_ids=[dedicated_workspace.workspace_id]
    )
    assert [h.page_id for h in hits] == [page.page_id]
