"""Bulk import (07 §5, phase3-tasklist.md step 74) — POST /sources/bulk."""

import uuid

from sqlalchemy import select

from karpwiki.models import AccessPolicy, RawSource, ReviewItem, ReviewKind, Role

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
ADMIN = {"X-Karpwiki-User": "avery"}
READER = {"X-Karpwiki-User": "casey"}


async def _grant_admin(session, workspace, principal="avery"):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal=principal, role=Role.admin))
    await session.commit()


async def test_bulk_submit_accepts_multiple_real_files(client, session, workspace):
    await _grant_admin(session, workspace)

    r = await client.post(
        "/sources/bulk",
        headers=ADMIN,
        files=[
            ("files", ("a.txt", b"first document", "text/plain")),
            ("files", ("b.txt", b"second document", "text/plain")),
        ],
    )
    assert r.status_code == 202
    body = r.json()
    assert len(body["submitted"]) == 2
    assert body["rejected"] == []

    source_ids = {uuid.UUID(item["source_id"]) for item in body["submitted"]}
    rows = (await session.execute(select(RawSource).where(RawSource.source_id.in_(source_ids)))).scalars().all()
    assert {r.filename for r in rows} == {"a.txt", "b.txt"}
    assert all(r.workspace_id is None for r in rows)


async def test_bulk_submit_each_file_gets_its_own_submission_review_item(client, session, workspace):
    """Confirms the resolved design: review items are unaffected by bulk import — the
    "bypassed noise" is the manual per-file API call, not the review queue."""
    await _grant_admin(session, workspace)

    r = await client.post(
        "/sources/bulk",
        headers=ADMIN,
        files=[
            ("files", ("a.txt", b"first document", "text/plain")),
            ("files", ("b.txt", b"second document", "text/plain")),
        ],
    )
    source_ids = [item["source_id"] for item in r.json()["submitted"]]

    items = (
        await session.execute(
            select(ReviewItem).where(ReviewItem.subject_ref.in_(source_ids))
        )
    ).scalars().all()
    assert len(items) == 2
    assert all(i.kind is ReviewKind.submission for i in items)


async def test_bulk_submit_skips_an_unreadable_file_without_failing_the_batch(client, session, workspace):
    await _grant_admin(session, workspace)
    png_like = b"\x89PNG\r\n\x1a\n\x00\x01\xff\xfe" * 4

    r = await client.post(
        "/sources/bulk",
        headers=ADMIN,
        files=[
            ("files", ("good.txt", b"a real document", "text/plain")),
            ("files", ("bad.png", png_like, "image/png")),
        ],
    )
    assert r.status_code == 202
    body = r.json()
    assert len(body["submitted"]) == 1
    assert body["submitted"][0]["filename"] == "good.txt"
    assert len(body["rejected"]) == 1
    assert body["rejected"][0]["filename"] == "bad.png"


async def test_bulk_submit_dispatches_classify_source_for_each_stored_file(client, session, workspace, monkeypatch):
    await _grant_admin(session, workspace)
    dispatched = []
    from karpwiki import tasks

    monkeypatch.setattr(tasks.classify_source, "delay", lambda source_id: dispatched.append(source_id))

    r = await client.post(
        "/sources/bulk",
        headers=ADMIN,
        files=[
            ("files", ("a.txt", b"first document", "text/plain")),
            ("files", ("b.txt", b"second document", "text/plain")),
        ],
    )
    submitted_ids = {item["source_id"] for item in r.json()["submitted"]}
    assert set(dispatched) == submitted_ids


async def test_bulk_submit_requires_admin_not_just_contributor(client, session, workspace):
    """Framed as "Admin tooling" (07 §5) — a higher bar than the ordinary POST /sources
    contributor requirement, confirmed via AskUserQuestion."""
    r = await client.post(
        "/sources/bulk", headers=CONTRIBUTOR, files=[("files", ("a.txt", b"x", "text/plain"))]
    )
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "forbidden"


async def test_bulk_submit_rejects_a_reader(client):
    r = await client.post(
        "/sources/bulk", headers=READER, files=[("files", ("a.txt", b"x", "text/plain"))]
    )
    assert r.status_code == 403


async def test_bulk_submit_rejects_unauthenticated(client):
    r = await client.post("/sources/bulk", files=[("files", ("a.txt", b"x", "text/plain"))])
    assert r.status_code == 401


async def test_a_retried_bulk_submission_creates_sources_once(client, session, workspace):
    """09 §14: the same idempotency-key replay guarantee POST /sources already has,
    extended to the bulk endpoint's list-shaped response body."""
    await _grant_admin(session, workspace)
    headers = {**ADMIN, "Idempotency-Key": "bulk-key-1"}

    first = await client.post(
        "/sources/bulk", headers=headers, files=[("files", ("a.txt", b"same content", "text/plain"))]
    )
    second = await client.post(
        "/sources/bulk", headers=headers, files=[("files", ("a.txt", b"same content", "text/plain"))]
    )

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert second.headers.get("Idempotency-Replayed") == "true"

    rows = (await session.execute(select(RawSource).where(RawSource.filename == "a.txt"))).scalars().all()
    assert len(rows) == 1
