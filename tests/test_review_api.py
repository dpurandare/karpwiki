"""Review queue endpoints (05 §1, 06 §1) — phase1-tasklist step 19."""

import uuid

from karpwiki import pipeline, review
from karpwiki.models import (
    AccessPolicy,
    PipelineState,
    ReviewKind,
    ReviewStatus,
    Role,
)

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
ADMIN = {"X-Karpwiki-User": "avery"}


async def _grant_admin(session, workspace, principal="avery"):
    session.add(
        AccessPolicy(workspace_id=workspace.workspace_id, principal=principal, role=Role.admin)
    )
    await session.flush()


async def test_listing_requires_admin_somewhere(client):
    r = await client.get("/review-items", headers=CONTRIBUTOR)
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "forbidden"


async def test_admin_lists_the_queue(client, session, workspace):
    await _grant_admin(session, workspace)
    await client.post("/sources", headers=CONTRIBUTOR, data={"text": "x"})

    r = await client.get("/review-items", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["kind"] == ReviewKind.submission.value
    assert body["next_cursor"] is None


async def test_listing_paginates(client, session, workspace):
    await _grant_admin(session, workspace)
    for _ in range(3):
        await client.post("/sources", headers=CONTRIBUTOR, data={"text": "x"})

    page1 = await client.get("/review-items", headers=ADMIN, params={"limit": 2})
    assert len(page1.json()["items"]) == 2
    cursor = page1.json()["next_cursor"]
    assert cursor is not None

    page2 = await client.get(
        "/review-items", headers=ADMIN, params={"limit": 2, "cursor": cursor}
    )
    assert len(page2.json()["items"]) == 1
    assert page2.json()["next_cursor"] is None

    ids_1 = {i["review_id"] for i in page1.json()["items"]}
    ids_2 = {i["review_id"] for i in page2.json()["items"]}
    assert not (ids_1 & ids_2)


async def test_listing_filters_by_kind_and_rejects_a_bogus_one(client, session, workspace):
    await _grant_admin(session, workspace)
    await client.post("/sources", headers=CONTRIBUTOR, data={"text": "x"})

    r = await client.get("/review-items", headers=ADMIN, params={"kind": "duplicate"})
    assert r.json()["items"] == []

    r = await client.get("/review-items", headers=ADMIN, params={"kind": "bogus"})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request"


async def test_acknowledge_a_submission_item(client, session, workspace):
    await _grant_admin(session, workspace)
    await client.post("/sources", headers=CONTRIBUTOR, data={"text": "x"})
    item = (await client.get("/review-items", headers=ADMIN)).json()["items"][0]

    r = await client.post(
        f"/review-items/{item['review_id']}/resolve",
        headers=ADMIN,
        json={"action": "acknowledge"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == ReviewStatus.resolved.value
    assert r.json()["pipeline_state"] is None

    again = await client.post(
        f"/review-items/{item['review_id']}/resolve",
        headers=ADMIN,
        json={"action": "acknowledge"},
    )
    assert again.status_code == 409
    assert again.json()["error"]["type"] == "conflict"


async def test_non_admin_cannot_resolve(client, session, workspace):
    await _grant_admin(session, workspace)
    await client.post("/sources", headers=CONTRIBUTOR, data={"text": "x"})
    item = (await client.get("/review-items", headers=ADMIN)).json()["items"][0]

    r = await client.post(
        f"/review-items/{item['review_id']}/resolve",
        headers=CONTRIBUTOR,
        json={"action": "acknowledge"},
    )
    assert r.status_code == 403


async def test_resolve_a_classification_item_via_the_endpoint(client, session, workspace):
    await _grant_admin(session, workspace)
    submitted = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "x"})
    source_id = uuid.UUID(submitted.json()["source_id"])

    from karpwiki.models import RawSource

    source = await session.get(RawSource, source_id)
    await pipeline.transition(
        session, source=source, to_state=PipelineState.classifying, actor="system:classifier"
    )
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.pending_review,
        actor="system:classifier",
        detail={"reason": "confidence below threshold", "candidates": []},
    )
    item = await review.create(
        session, kind=ReviewKind.classification, subject_ref=str(source.source_id)
    )
    await session.commit()

    r = await client.post(
        f"/review-items/{item.review_id}/resolve",
        headers=ADMIN,
        json={"action": "eng.runbook"},
    )
    assert r.status_code == 200
    assert r.json()["pipeline_state"] == PipelineState.classified.value
    assert r.json()["resolved_action"] == "eng.runbook"


async def test_resolving_into_a_workspace_without_admin_there_is_forbidden(
    client, session, workspace, other_workspace
):
    """Granted admin on `workspace` only; resolving a classification into `other_workspace`
    must be checked against *that* workspace, not just "admin somewhere" (09 §22)."""
    await _grant_admin(session, workspace)
    submitted = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "x"})
    source_id = uuid.UUID(submitted.json()["source_id"])

    from karpwiki.models import RawSource

    source = await session.get(RawSource, source_id)
    await pipeline.transition(
        session, source=source, to_state=PipelineState.classifying, actor="system:classifier"
    )
    await pipeline.transition(
        session,
        source=source,
        to_state=PipelineState.pending_review,
        actor="system:classifier",
        detail={"reason": "confidence below threshold", "candidates": []},
    )
    item = await review.create(
        session, kind=ReviewKind.classification, subject_ref=str(source.source_id)
    )
    await session.commit()

    r = await client.post(
        f"/review-items/{item.review_id}/resolve",
        headers=ADMIN,
        json={"action": "policy.hr"},
    )
    assert r.status_code == 403


async def test_resolve_a_missing_item_is_404(client, session, workspace):
    await _grant_admin(session, workspace)
    r = await client.post(
        f"/review-items/{uuid.uuid4()}/resolve", headers=ADMIN, json={"action": "acknowledge"}
    )
    assert r.status_code == 404


async def test_idempotency_key_replays_the_resolution(client, session, workspace):
    await _grant_admin(session, workspace)
    await client.post("/sources", headers=CONTRIBUTOR, data={"text": "x"})
    item = (await client.get("/review-items", headers=ADMIN)).json()["items"][0]

    headers = {**ADMIN, "Idempotency-Key": "resolve-1"}
    first = await client.post(
        f"/review-items/{item['review_id']}/resolve", headers=headers, json={"action": "acknowledge"}
    )
    second = await client.post(
        f"/review-items/{item['review_id']}/resolve", headers=headers, json={"action": "acknowledge"}
    )
    assert first.status_code == second.status_code == 200
    assert second.headers.get("Idempotency-Replayed") == "true"
