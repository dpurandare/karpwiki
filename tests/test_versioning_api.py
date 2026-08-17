"""Version Browser endpoints (05 §6, 06 §1) — phase1-tasklist step 20."""

import uuid
from datetime import date

from karpwiki import versioning
from karpwiki.models import AccessPolicy, PageStatus, PageType, Role

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
ADMIN = {"X-Karpwiki-User": "avery"}

PAGE = dict(
    path="concepts/retry-backoff.md",
    page_type=PageType.concept,
    title="Retry and Backoff",
    description="How services retry failed calls.",
    date=date(2026, 8, 14),
    tags=["reliability", "patterns"],
    author="user:deepak",
    status=PageStatus.published,
)


async def _grant_admin(session, workspace, principal="avery"):
    session.add(
        AccessPolicy(workspace_id=workspace.workspace_id, principal=principal, role=Role.admin)
    )
    await session.flush()


async def _page_with_history(session, workspace):
    page = await versioning.create_page(
        session, workspace_id=workspace.workspace_id, body="# v1\n", **PAGE
    )
    v1 = page.current_version_id
    v2 = (
        await versioning.write_version(
            session, page=page, body="# v2\n", author="user:deepak",
            trigger=versioning.VersionTrigger.manual_edit,
        )
    ).version_id
    await session.commit()
    return page, v1, v2


async def test_listing_versions_requires_admin_in_the_pages_workspace(client, session, workspace):
    page, _, _ = await _page_with_history(session, workspace)
    r = await client.get(f"/pages/{page.page_id}/versions", headers=CONTRIBUTOR)
    assert r.status_code == 403


async def test_admin_lists_versions_newest_first(client, session, workspace):
    await _grant_admin(session, workspace)
    page, v1, v2 = await _page_with_history(session, workspace)

    r = await client.get(f"/pages/{page.page_id}/versions", headers=ADMIN)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["version_id"] for i in items] == [str(v2), str(v1)]
    assert r.json()["next_cursor"] is None


async def test_listing_versions_paginates(client, session, workspace):
    await _grant_admin(session, workspace)
    page, v1, v2 = await _page_with_history(session, workspace)

    r = await client.get(f"/pages/{page.page_id}/versions", headers=ADMIN, params={"limit": 1})
    assert [i["version_id"] for i in r.json()["items"]] == [str(v2)]
    cursor = r.json()["next_cursor"]
    assert cursor is not None

    r2 = await client.get(
        f"/pages/{page.page_id}/versions", headers=ADMIN, params={"limit": 1, "cursor": cursor}
    )
    assert [i["version_id"] for i in r2.json()["items"]] == [str(v1)]
    assert r2.json()["next_cursor"] is None


async def test_get_one_version_includes_content(client, session, workspace):
    await _grant_admin(session, workspace)
    page, v1, _ = await _page_with_history(session, workspace)

    r = await client.get(f"/pages/{page.page_id}/versions/{v1}", headers=ADMIN)
    assert r.status_code == 200
    assert "# v1" in r.json()["content"]


async def test_get_a_missing_version_is_404(client, session, workspace):
    await _grant_admin(session, workspace)
    page, _, _ = await _page_with_history(session, workspace)
    r = await client.get(f"/pages/{page.page_id}/versions/{uuid.uuid4()}", headers=ADMIN)
    assert r.status_code == 404


async def test_diff_route_is_not_shadowed_by_the_version_id_route(client, session, workspace):
    """`/versions/diff` must resolve to the diff handler, not `get_page_version` with
    version_id='diff' (a route-ordering hazard the two similarly-shaped paths create)."""
    await _grant_admin(session, workspace)
    page, v1, v2 = await _page_with_history(session, workspace)

    r = await client.get(
        f"/pages/{page.page_id}/versions/diff",
        headers=ADMIN,
        params={"from_version_id": str(v1), "to_version_id": str(v2)},
    )
    assert r.status_code == 200
    assert "-# v1" in r.json()["diff"]
    assert "+# v2" in r.json()["diff"]


async def test_rollback_via_the_endpoint(client, session, workspace):
    await _grant_admin(session, workspace)
    page, v1, v2 = await _page_with_history(session, workspace)

    r = await client.post(
        f"/pages/{page.page_id}/rollback", headers=ADMIN, json={"target_version_id": str(v1)}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["trigger"] == "rollback"
    assert body["restored_from_version_id"] == str(v1)

    versions = (await client.get(f"/pages/{page.page_id}/versions", headers=ADMIN)).json()["items"]
    assert len(versions) == 3


async def test_non_admin_cannot_rollback(client, session, workspace):
    await _grant_admin(session, workspace)
    page, v1, _ = await _page_with_history(session, workspace)

    r = await client.post(
        f"/pages/{page.page_id}/rollback", headers=CONTRIBUTOR, json={"target_version_id": str(v1)}
    )
    assert r.status_code == 403


async def test_rollback_idempotency_key_replays(client, session, workspace):
    await _grant_admin(session, workspace)
    page, v1, _ = await _page_with_history(session, workspace)

    headers = {**ADMIN, "Idempotency-Key": "roll-1"}
    first = await client.post(
        f"/pages/{page.page_id}/rollback", headers=headers, json={"target_version_id": str(v1)}
    )
    second = await client.post(
        f"/pages/{page.page_id}/rollback", headers=headers, json={"target_version_id": str(v1)}
    )
    assert first.status_code == second.status_code == 200
    assert second.headers.get("Idempotency-Replayed") == "true"

    versions = (await client.get(f"/pages/{page.page_id}/versions", headers=ADMIN)).json()["items"]
    assert len(versions) == 3  # the replay did not roll back a second time
