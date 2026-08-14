"""Phase 1a step 6 verification (spec/phase1-tasklist.md).

"Can create a workspace, write a wiki_page + page_version directly (no ingestion yet),
and read it back with correct version pointer."
"""

from datetime import date

import pytest
from sqlalchemy import select

from karpwiki import objectstore, versioning
from karpwiki.frontmatter import FrontmatterError, split_frontmatter, validate_frontmatter
from karpwiki.models import (
    IndexState,
    IndexStatus,
    IndexType,
    PageStatus,
    PageType,
    PageVersion,
    VersionTrigger,
    WikiPage,
)

PAGE = dict(
    path="concepts/retry-backoff.md",
    page_type=PageType.concept,
    title="Retry and Backoff",
    description="How services retry failed calls.",
    date=date(2026, 8, 14),
    tags=["reliability", "patterns"],
    author="user:deepak",
)


async def test_create_page_sets_current_version_pointer(session, workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Retry\n", **PAGE
    )
    await session.commit()

    found = (
        await session.execute(select(WikiPage).where(WikiPage.page_id == page.page_id))
    ).scalar_one()
    version = await session.get(PageVersion, found.current_version_id)

    assert version is not None
    assert version.page_id == found.page_id
    assert version.trigger is VersionTrigger.ingest
    assert version.frontmatter["current_version"] == str(version.version_id)
    assert version.frontmatter["title"] == "Retry and Backoff"
    assert "# Retry" in version.content


async def test_new_page_is_queued_for_indexing(session, workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Retry\n", **PAGE
    )
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    assert status.state is IndexState.pending


async def test_write_version_appends_and_moves_pointer(session, workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Retry\n", **PAGE
    )
    first_version_id = page.current_version_id

    second = await versioning.write_version(
        session,
        page=page,
        body="# Retry\n\nUse exponential backoff.\n",
        author="user:deepak",
        trigger=VersionTrigger.manual_edit,
        change_summary="add guidance",
    )
    await session.commit()

    assert page.current_version_id == second.version_id
    assert second.version_id != first_version_id

    history = await versioning.history(session, page.page_id)
    assert [v.version_id for v in history] == [first_version_id, second.version_id]

    # History is intact: the first version's content is unchanged.
    first = await session.get(PageVersion, first_version_id)
    assert "exponential backoff" not in first.content


async def test_write_version_stores_diff_in_object_store(session, workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Retry\n", **PAGE
    )
    second = await versioning.write_version(
        session,
        page=page,
        body="# Retry\n\nUse exponential backoff.\n",
        author="user:deepak",
        trigger=VersionTrigger.manual_edit,
    )

    assert second.diff_ref == f"/{workspace.workspace_id}/diffs/{second.version_id}.diff"
    diff = objectstore.read_text(second.diff_ref)
    assert "+Use exponential backoff." in diff


async def test_indexed_page_goes_stale_on_write(session, workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Retry\n", **PAGE
    )
    status = await session.get(IndexStatus, (page.page_id, IndexType.fts))
    status.state = IndexState.indexed
    await session.flush()

    await versioning.write_version(
        session,
        page=page,
        body="# Retry\n\nEdited.\n",
        author="user:deepak",
        trigger=VersionTrigger.manual_edit,
    )
    assert status.state is IndexState.stale


async def test_rollback_is_non_destructive(session, workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Original\n", **PAGE
    )
    original_version_id = page.current_version_id

    await versioning.write_version(
        session,
        page=page,
        body="# Replaced\n",
        author="user:deepak",
        trigger=VersionTrigger.manual_edit,
    )

    restored = await versioning.rollback(
        session, page=page, target_version_id=original_version_id, author="user:admin"
    )
    await session.commit()

    assert page.current_version_id == restored.version_id
    assert restored.trigger is VersionTrigger.rollback
    assert restored.restored_from_version_id == original_version_id
    assert "# Original" in restored.content
    # Three versions exist — nothing was deleted.
    assert len(await versioning.history(session, page.page_id)) == 3


async def test_page_status_change_is_reflected_in_frontmatter(session, workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Retry\n", **PAGE
    )
    published = await versioning.write_version(
        session,
        page=page,
        body="# Retry\n",
        author="user:deepak",
        trigger=VersionTrigger.manual_edit,
        status=PageStatus.published,
    )
    assert page.status is PageStatus.published
    assert published.frontmatter["status"] == "published"


async def test_frontmatter_requires_two_tags(session, workspace):
    with pytest.raises(FrontmatterError, match="at least 2 tags"):
        await versioning.create_page(
            session,
            workspace_id=workspace.workspace_id,
            body="# Retry\n",
            **{**PAGE, "tags": ["reliability"]},
        )


async def test_frontmatter_round_trips(session, workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Retry\n", **PAGE
    )
    version = await session.get(PageVersion, page.current_version_id)
    parsed, body = split_frontmatter(version.content)
    validated = validate_frontmatter(parsed)

    assert validated.title == "Retry and Backoff"
    assert validated.page_type is PageType.concept
    assert body.strip() == "# Retry"
