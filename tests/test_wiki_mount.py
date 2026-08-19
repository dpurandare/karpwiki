"""Read-only FUSE-mount access to the wiki export (09 §12) — phase3-tasklist.md step 58.

No test here ever imports `fsspec.fuse` or performs a real mount — that needs a kernel-level
FUSE driver installed on the host (confirmed out of scope via AskUserQuestion). What's tested
is this module's own logic: the AuthZ check and the read-only view-scoping.
"""

from datetime import date

import pytest

from karpwiki import versioning, wiki_mount
from karpwiki.models import AccessPolicy, PageType, Role


async def test_check_fuse_access_denies_without_a_grant(session, workspace):
    with pytest.raises(wiki_mount.FuseAccessDenied):
        await wiki_mount.check_fuse_access(
            session, principal_keys=("user:deepak",), workspace_id=workspace.workspace_id
        )


async def test_check_fuse_access_denies_a_role_grant_without_fuse_access(session, workspace):
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="user:deepak",
            role=Role.admin,
            fuse_access=False,
        )
    )
    await session.flush()
    with pytest.raises(wiki_mount.FuseAccessDenied):
        await wiki_mount.check_fuse_access(
            session, principal_keys=("user:deepak",), workspace_id=workspace.workspace_id
        )


async def test_check_fuse_access_allows_a_real_grant(session, workspace):
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="user:deepak",
            role=Role.reader,
            fuse_access=True,
        )
    )
    await session.flush()
    await wiki_mount.check_fuse_access(
        session, principal_keys=("user:deepak",), workspace_id=workspace.workspace_id
    )  # does not raise


async def test_check_fuse_access_allows_via_a_group_grant(session, workspace):
    session.add(
        AccessPolicy(
            workspace_id=workspace.workspace_id,
            principal="group:eng",
            role=Role.reader,
            fuse_access=True,
        )
    )
    await session.flush()
    await wiki_mount.check_fuse_access(
        session,
        principal_keys=("user:deepak", "group:eng"),
        workspace_id=workspace.workspace_id,
    )  # does not raise


async def test_scoped_filesystem_serves_only_the_wiki_prefix(session, workspace):
    await versioning.create_page(
        session,
        workspace_id=workspace.workspace_id,
        path="concepts/retry-backoff.md",
        page_type=PageType.concept,
        title="Retry and Backoff",
        description="How services retry failed calls.",
        date=date(2026, 8, 19),
        tags=["reliability", "patterns"],
        body="# Retry\n",
        author="user:deepak",
    )
    await session.commit()

    fs = wiki_mount.scoped_filesystem(workspace.workspace_id)
    assert fs.exists("concepts/retry-backoff.md")
    assert "# Retry" in fs.open("concepts/retry-backoff.md", "rb").read().decode()
    # The view is rooted at exactly .../{workspace_id}/wiki — never sources/diffs/assets,
    # which live at sibling prefixes under the same workspace (09 §12's own scope boundary).
    assert fs._fs.path.rstrip("/").endswith(f"{workspace.workspace_id}/wiki")


def test_scoped_filesystem_blocks_writes():
    fs = wiki_mount.scoped_filesystem("some-workspace")
    with pytest.raises(PermissionError):
        fs.open("overview.md", "wb")
    with pytest.raises(PermissionError):
        fs.rm("overview.md")
    with pytest.raises(PermissionError):
        fs.mkdir("new-dir")
    with pytest.raises(PermissionError):
        fs.touch("new-file.md")


def test_scoped_filesystem_allows_reads_through():
    fs = wiki_mount.scoped_filesystem("some-workspace")
    # Read-mode open and inspection methods pass straight through, unblocked.
    assert callable(fs.ls)
    assert callable(fs.info)
