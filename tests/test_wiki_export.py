"""Wiki markdown export mirror (01 §1, 02 §2) — phase3-tasklist.md step 57."""

from datetime import date

from karpwiki import bulk_move, objectstore, versioning, wiki_export, workspaces
from karpwiki.frontmatter import split_frontmatter
from karpwiki.models import PageType, VersionTrigger

PAGE = dict(
    path="concepts/retry-backoff.md",
    page_type=PageType.concept,
    title="Retry and Backoff",
    description="How services retry failed calls.",
    date=date(2026, 8, 19),
    tags=["reliability", "patterns"],
    author="user:deepak",
)


def test_export_path_matches_the_object_store_wiki_prefix():
    assert wiki_export.export_path("eng-docs", "concepts/foo.md") == "/eng-docs/wiki/concepts/foo.md"


async def test_create_page_writes_the_mirror(session, workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Retry\n", **PAGE
    )
    await session.commit()

    exported = objectstore.read_text(wiki_export.export_path(workspace.workspace_id, page.path))
    assert "# Retry" in exported
    _, body = split_frontmatter(exported)
    assert "# Retry" in body


async def test_write_version_overwrites_the_mirror_in_place(session, workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Original\n", **PAGE
    )
    await versioning.write_version(
        session,
        page=page,
        body="# Updated\n",
        author="user:deepak",
        trigger=VersionTrigger.manual_edit,
    )
    await session.commit()

    exported = objectstore.read_text(wiki_export.export_path(workspace.workspace_id, page.path))
    assert "# Updated" in exported
    assert "# Original" not in exported


async def test_delete_removes_the_mirrored_file(session, workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Retry\n", **PAGE
    )
    await session.commit()
    path = wiki_export.export_path(workspace.workspace_id, page.path)
    assert objectstore.exists(path)

    wiki_export.delete(workspace_id=workspace.workspace_id, path=page.path)
    assert not objectstore.exists(path)


async def test_schema_placeholder_names_the_pointer_when_set(session, workspace):
    await workspaces.update(session, workspace=workspace, schema_ref="s3://schemas/eng-docs.md")
    await session.commit()

    exported = objectstore.read_text(wiki_export.export_path(workspace.workspace_id, "SCHEMA.md"))
    assert "s3://schemas/eng-docs.md" in exported
    assert "step 59" in exported


async def test_schema_placeholder_written_at_workspace_create(session):
    created = await workspaces.create(
        session, workspace_id="fresh-ws", name="Fresh", schema_ref="s3://schemas/fresh.md"
    )
    await session.commit()

    exported = objectstore.read_text(wiki_export.export_path(created.workspace_id, "SCHEMA.md"))
    assert "s3://schemas/fresh.md" in exported


async def test_schema_placeholder_reports_not_set_when_absent(session):
    created = await workspaces.create(session, workspace_id="bare-ws", name="Bare")
    await session.commit()

    exported = objectstore.read_text(wiki_export.export_path(created.workspace_id, "SCHEMA.md"))
    assert "(not set)" in exported


async def test_export_workspace_backfills_every_current_page_and_schema(session, workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Retry\n", **PAGE
    )
    await session.commit()
    # Simulate a pre-existing page that predates the write-through hook: delete its
    # already-written mirror, then confirm the backfill puts it back.
    wiki_export.delete(workspace_id=workspace.workspace_id, path=page.path)
    path = wiki_export.export_path(workspace.workspace_id, page.path)
    assert not objectstore.exists(path)

    count = await wiki_export.export_workspace(session, workspace_id=workspace.workspace_id)

    assert count == 1
    assert objectstore.exists(path)
    assert objectstore.exists(wiki_export.export_path(workspace.workspace_id, "SCHEMA.md"))


async def test_bulk_move_removes_the_stale_mirror_at_the_old_workspace(session, workspace, other_workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Retry\n", **PAGE
    )
    await session.commit()
    old_path = wiki_export.export_path(workspace.workspace_id, page.path)
    assert objectstore.exists(old_path)

    await bulk_move.execute_batch(
        session,
        source_workspace_id=workspace.workspace_id,
        target_workspace_id=other_workspace.workspace_id,
        page_ids=[page.page_id],
        source_ids=[],
        actor="user:avery",
    )
    await session.commit()

    assert not objectstore.exists(old_path)
    new_path = wiki_export.export_path(other_workspace.workspace_id, page.path)
    assert objectstore.exists(new_path)
    exported = objectstore.read_text(new_path)
    assert "# Retry" in exported
