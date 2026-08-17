"""Markdown cross-reference parsing into page_link rows (02 §3, 01 §6) —
phase2-tasklist.md step 28."""

from datetime import date

from sqlalchemy import select

from karpwiki import page_links, versioning
from karpwiki.models import LinkType, PageLink, PageStatus, PageType


def test_extract_link_targets_finds_inline_links_only():
    body = (
        "See [the runbook](concepts/runbook.md) and [external site](https://example.com).\n"
        "An image: ![diagram](assets/diagram.png) is not a link.\n"
        "A footnote[^1] is not a link either.\n"
        "[^1]: citation text, not a link target.\n"
    )
    targets = page_links.extract_link_targets(body)
    assert targets == ["concepts/runbook.md", "https://example.com"]


async def _page(session, workspace, *, title, body, path=None):
    return await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path=path or f"concepts/{title.lower().replace(' ', '-')}.md",
        page_type=PageType.concept,
        title=title,
        description=f"About {title}.",
        date=date(2026, 8, 17),
        tags=["a", "b"],
        body=body,
        author="system:curator",
        status=PageStatus.published,
    )


async def _links_from(session, page_id):
    rows = (
        (await session.execute(select(PageLink).where(PageLink.from_page_id == page_id)))
        .scalars()
        .all()
    )
    return {(r.to_page_id, r.link_type) for r in rows}


async def test_create_page_parses_same_workspace_link(session, workspace):
    target = await _page(session, workspace, title="Target Page", body="Target content.")
    source = await _page(
        session, workspace, title="Source Page", body=f"See [target](concepts/target-page.md)."
    )

    assert await _links_from(session, source.page_id) == {(target.page_id, LinkType.cross_reference)}


async def test_create_page_parses_cross_workspace_link(session, workspace, other_workspace):
    target = await _page(session, other_workspace, title="Other Target", body="Other content.")
    fq_path = f"/{other_workspace.workspace_id}/concepts/other-target.md"
    source = await _page(session, workspace, title="Linker", body=f"See [target]({fq_path}).")

    assert await _links_from(session, source.page_id) == {(target.page_id, LinkType.cross_workspace)}


async def test_dangling_and_external_links_create_no_row(session, workspace):
    source = await _page(
        session,
        workspace,
        title="Dangler",
        body="See [missing](concepts/nope.md) and [ext](https://example.com).",
    )
    assert await _links_from(session, source.page_id) == set()


async def test_self_link_is_excluded(session, workspace):
    source = await _page(session, workspace, title="Self Linker", body="placeholder")
    # A page can't reference its own path in its very first version (path is derived from
    # the title at creation time), so exercise the self-link exclusion via write_version.
    await versioning.write_version(
        session,
        page=source,
        body="See [myself](concepts/self-linker.md).",
        author="system:curator",
        trigger=versioning.VersionTrigger.manual_edit,
    )
    assert await _links_from(session, source.page_id) == set()


async def test_malformed_fully_qualified_path_does_not_crash(session, workspace):
    source = await _page(session, workspace, title="Malformed", body="See [bad](/onlyworkspace).")
    assert await _links_from(session, source.page_id) == set()


async def test_write_version_replaces_prior_links(session, workspace):
    old_target = await _page(session, workspace, title="Old Target", body="old")
    new_target = await _page(session, workspace, title="New Target", body="new")
    source = await _page(
        session, workspace, title="Rewriter", body="See [old](concepts/old-target.md)."
    )
    assert await _links_from(session, source.page_id) == {(old_target.page_id, LinkType.cross_reference)}

    await versioning.write_version(
        session,
        page=source,
        body="See [new](concepts/new-target.md) instead.",
        author="system:curator",
        trigger=versioning.VersionTrigger.manual_edit,
    )
    assert await _links_from(session, source.page_id) == {(new_target.page_id, LinkType.cross_reference)}
