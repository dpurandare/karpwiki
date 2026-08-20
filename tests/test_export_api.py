"""Bulk export (07 §5, phase3-tasklist.md step 74) — GET /workspaces/{id}/export."""

import io
import tarfile
from datetime import date

from karpwiki.models import AccessPolicy, PageType, Role

ADMIN = {"X-Karpwiki-User": "avery"}
READER = {"X-Karpwiki-User": "casey"}

PAGE = dict(
    path="concepts/retry-backoff.md",
    page_type=PageType.concept,
    title="Retry and Backoff",
    description="How services retry failed calls.",
    date=date(2026, 8, 19),
    tags=["reliability", "patterns"],
    author="user:deepak",
)


async def test_export_endpoint_rejects_non_admin(client, session, workspace):
    r = await client.get(f"/workspaces/{workspace.workspace_id}/export", headers=READER)
    assert r.status_code == 403


async def test_export_endpoint_rejects_unauthenticated(client, workspace):
    r = await client.get(f"/workspaces/{workspace.workspace_id}/export")
    assert r.status_code == 401


async def test_export_endpoint_404s_for_an_unknown_workspace(client, session, workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await session.commit()

    r = await client.get("/workspaces/does-not-exist/export", headers=ADMIN)
    assert r.status_code == 404


async def test_export_endpoint_rejects_admin_of_a_different_workspace(
    client, session, workspace, other_workspace
):
    session.add(
        AccessPolicy(workspace_id=other_workspace.workspace_id, principal="avery", role=Role.admin)
    )
    await session.commit()

    r = await client.get(f"/workspaces/{workspace.workspace_id}/export", headers=ADMIN)
    assert r.status_code == 403


async def test_export_endpoint_returns_a_real_downloadable_archive(client, session, workspace):
    from karpwiki import versioning

    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Retry\n", **PAGE
    )
    await session.commit()

    r = await client.get(f"/workspaces/{workspace.workspace_id}/export", headers=ADMIN)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    assert f'{workspace.workspace_id}-export.tar.gz' in r.headers["content-disposition"]

    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        names = tar.getnames()
        assert f"{workspace.workspace_id}/wiki/{PAGE['path']}" in names


async def test_export_endpoint_repairs_a_stale_mirror_before_packaging(client, session, workspace):
    """The write-through hook normally keeps the mirror current — this proves the endpoint's
    own repair pass (`wiki_export.export_workspace`) actually runs, not just relies on it."""
    from karpwiki import objectstore, versioning, wiki_export

    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# Retry\n", **PAGE
    )
    await session.commit()
    wiki_export.delete(workspace_id=workspace.workspace_id, path=page.path)
    assert not objectstore.exists(wiki_export.export_path(workspace.workspace_id, page.path))

    r = await client.get(f"/workspaces/{workspace.workspace_id}/export", headers=ADMIN)
    assert r.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        assert f"{workspace.workspace_id}/wiki/{page.path}" in tar.getnames()
