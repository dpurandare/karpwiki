"""Submission endpoint and gateway conventions — phase1-tasklist step 7."""

import hashlib
import uuid

from karpwiki import objectstore
from karpwiki.models import PipelineState, RawSource

CONTRIBUTOR = {"X-Karpwiki-User": "deepak"}
READER = {"X-Karpwiki-User": "casey"}
VIA_GROUP = {"X-Karpwiki-User": "morgan", "X-Karpwiki-Groups": "group:eng, group:ops"}


async def test_submission_is_accepted_without_naming_a_workspace(client, session):
    """03 §2: the gateway accepts a source in the target-undetermined state."""
    r = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "retry with backoff"})
    assert r.status_code == 202
    body = r.json()
    assert body["pipeline_state"] == PipelineState.submitted.value

    source = await session.get(RawSource, uuid.UUID(body["source_id"]))
    assert source.workspace_id is None
    assert source.submitted_by == "user:deepak"


async def test_the_payload_reaches_the_object_store(client, session):
    r = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "durable body"})
    source = await session.get(RawSource, uuid.UUID(r.json()["source_id"]))
    assert objectstore.read_bytes(source.object_key) == b"durable body"


async def test_submission_opens_the_ingestion_log(client, session):
    from karpwiki import pipeline

    r = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "x"})
    history = await pipeline.history(session, uuid.UUID(r.json()["source_id"]))
    assert [e.to_state for e in history] == [PipelineState.submitted]
    assert history[0].from_state is None


async def test_submission_always_creates_a_review_item(client, session):
    """03 §5: every submission, unconditionally, informational."""
    from sqlalchemy import select

    from karpwiki.models import ReviewItem, ReviewKind, ReviewStatus

    r = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "x"})
    result = await session.execute(
        select(ReviewItem).where(ReviewItem.subject_ref == r.json()["source_id"])
    )
    item = result.scalar_one()
    assert item.kind is ReviewKind.submission
    assert item.status is ReviewStatus.open
    assert item.workspace_id is None  # no workspace resolved yet
    assert item.proposed_action is None


async def test_file_upload_extracts_and_stores_the_payload(client, session):
    """A plain-text file upload — the `file=` multipart path, not `data={"text": ...}`."""
    r = await client.post(
        "/sources", headers=CONTRIBUTOR, files={"file": ("notes.txt", b"a plain text file", "text/plain")}
    )
    assert r.status_code == 202
    source = await session.get(RawSource, uuid.UUID(r.json()["source_id"]))
    assert objectstore.read_bytes(source.object_key) == b"a plain text file"


async def test_a_real_docx_upload_is_accepted(client, session):
    """phase3-tasklist.md step 62 prep gap: DOCX is a real binary format now, not
    something the direct upload path silently garbles."""
    import io

    import docx

    document = docx.Document()
    document.add_paragraph("Real DOCX content for the upload test.")
    buf = io.BytesIO()
    document.save(buf)

    r = await client.post(
        "/sources",
        headers=CONTRIBUTOR,
        files={
            "file": (
                "report.docx",
                buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert r.status_code == 202


async def test_an_unsupported_binary_upload_is_rejected(client):
    """A genuinely unsupported binary (not text, not PDF, not DOCX) is refused at
    submission — never silently garbled into the pipeline (found live during Phase 3 step
    62 prep)."""
    png_like = b"\x89PNG\r\n\x1a\n\x00\x01\xff\xfe" * 4
    r = await client.post(
        "/sources", headers=CONTRIBUTOR, files={"file": ("photo.png", png_like, "image/png")}
    )
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request"


async def test_unauthenticated_requests_are_refused(client):
    r = await client.post("/sources", data={"text": "x"})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "unauthenticated"


async def test_a_reader_may_not_submit(client):
    r = await client.post("/sources", headers=READER, data={"text": "x"})
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "forbidden"


async def test_a_grant_held_through_a_group_is_honoured(client):
    r = await client.post("/sources", headers=VIA_GROUP, data={"text": "x"})
    assert r.status_code == 202


async def test_exactly_one_payload_is_required(client):
    both = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "a", "url": "b"})
    assert both.status_code == 400
    assert both.json()["error"]["detail"]["supplied"] == ["text", "url"]

    neither = await client.post("/sources", headers=CONTRIBUTOR, data={})
    assert neither.status_code == 400


async def test_errors_use_the_envelope_and_carry_a_request_id(client):
    r = await client.post("/sources", headers=READER, data={"text": "x"})
    error = r.json()["error"]
    assert set(error) >= {"type", "message", "request_id"}
    assert error["request_id"] == r.headers["X-Request-Id"]


async def test_a_retried_submission_creates_one_source(client, session):
    """09 §14: without this, a client retry after a timeout ingests the document twice."""
    headers = {**CONTRIBUTOR, "Idempotency-Key": "key-abc"}
    first = await client.post("/sources", headers=headers, data={"text": "same body"})
    second = await client.post("/sources", headers=headers, data={"text": "same body"})

    assert first.status_code == second.status_code == 202
    assert first.json()["source_id"] == second.json()["source_id"]
    assert second.headers.get("Idempotency-Replayed") == "true"

    from karpwiki.api import find_by_hash

    digest = hashlib.sha256(b"same body").hexdigest()
    assert len(await find_by_hash(session, digest)) == 1


async def test_the_same_key_from_another_principal_is_a_different_request(client, session):
    await client.post(
        "/sources", headers={**CONTRIBUTOR, "Idempotency-Key": "shared"}, data={"text": "a"}
    )
    other = await client.post(
        "/sources", headers={**VIA_GROUP, "Idempotency-Key": "shared"}, data={"text": "b"}
    )
    assert other.status_code == 202
    assert other.headers.get("Idempotency-Replayed") is None


async def test_status_reads_the_denormalized_pointer(client):
    created = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "x"})
    r = await client.get(f"/sources/{created.json()['source_id']}", headers=CONTRIBUTOR)
    assert r.status_code == 200
    assert r.json()["pipeline_state"] == PipelineState.submitted.value
    assert r.json()["workspace_id"] is None
    assert r.json()["label"] == "processing"  # 03 §1's UI label, distinct from pipeline_state


async def test_another_principals_source_is_indistinguishable_from_a_missing_one(client):
    created = await client.post("/sources", headers=CONTRIBUTOR, data={"text": "x"})
    mine = await client.get(f"/sources/{created.json()['source_id']}", headers=VIA_GROUP)
    absent = await client.get(f"/sources/{uuid.uuid4()}", headers=VIA_GROUP)
    assert mine.status_code == absent.status_code == 404
    assert mine.json()["error"]["type"] == absent.json()["error"]["type"]
