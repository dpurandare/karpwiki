"""Common Gateway — submission entry point and the conventions every endpoint shares.

Implements phase1-tasklist step 7: 03 §2's submission path, plus the two cross-cutting
pieces it is the first to need — principal resolution and role enforcement (09 §15), and
the API conventions of 09 §14.

Not implemented here, deliberately: cursor pagination (09 §14) has no list endpoint to
exercise yet — it lands with the review queue — and the rate limiter is 07 §3, a later
phase. Building either now would be guesswork with no caller.
"""

import hashlib
import uuid
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Header, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import objectstore, pipeline, review
from .auth import Authenticator, Principal, TrustedHeaderAuthenticator, any_workspace_with_role
from .db import SessionLocal
from .models import IdempotencyRecord, PipelineState, RawSource, ReviewKind, Role

SUBMIT_ENDPOINT = "POST /sources"


class ApiError(Exception):
    """An error in 09 §14's envelope. `type` is the stable slug callers branch on."""

    def __init__(self, status: int, type_: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status = status
        self.type = type_
        self.message = message
        self.detail = detail


def _envelope(request: Request, exc: ApiError) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {
            "type": exc.type,
            "message": exc.message,
            "request_id": request.state.request_id,
        }
    }
    if exc.detail:
        body["error"]["detail"] = exc.detail
    return JSONResponse(status_code=exc.status, content=body)


def create_app(authenticator: Authenticator | None = None) -> FastAPI:
    app = FastAPI(title="karpwiki gateway")
    app.state.authenticator = authenticator or TrustedHeaderAuthenticator()

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        """Every response carries a request_id so a reported failure maps to a log line."""
        request.state.request_id = f"req_{uuid.uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        return _envelope(request, exc)

    _register_routes(app)
    return app


async def _session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


async def _principal(request: Request) -> Principal:
    resolved = request.app.state.authenticator.authenticate(dict(request.headers))
    if resolved is None:
        raise ApiError(401, "unauthenticated", "No authenticated principal on this request.")
    return resolved


def _register_routes(app: FastAPI) -> None:
    @app.post("/sources", status_code=202)
    async def submit_source(
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        file: UploadFile | None = None,
        text: Annotated[str | None, Form()] = None,
        url: Annotated[str | None, Form()] = None,
    ):
        """Accept a document (03 §2) in the target-undetermined state.

        The caller does not say which workspace it belongs to — that is the Classifier's
        job — so authorization asks whether the caller may contribute *anywhere* rather
        than to a named workspace.
        """
        supplied = [name for name, v in (("file", file), ("text", text), ("url", url)) if v]
        if len(supplied) != 1:
            raise ApiError(
                400,
                "invalid_request",
                "Provide exactly one of `file`, `text`, or `url`.",
                {"supplied": supplied},
            )

        if not await any_workspace_with_role(session, principal=principal, required=Role.contributor):
            raise ApiError(
                403,
                "forbidden",
                "Submitting requires the contributor role in at least one workspace.",
                {"principal": principal.id},
            )

        if idempotency_key:
            replayed = await _replay(session, idempotency_key, principal, SUBMIT_ENDPOINT)
            if replayed is not None:
                response.headers["Idempotency-Replayed"] = "true"
                return replayed

        if file is not None:
            payload, filename = await file.read(), file.filename or "upload"
        elif text is not None:
            payload, filename = text.encode(), "pasted.txt"
        else:
            payload, filename = url.encode(), "submitted-url.txt"

        source = await _store(session, payload, filename, submitted_by=f"user:{principal.id}")
        body = {
            "source_id": str(source.source_id),
            "pipeline_state": source.pipeline_state.value,
            "filename": source.filename,
        }
        if idempotency_key:
            session.add(
                IdempotencyRecord(
                    key=idempotency_key,
                    principal=principal.id,
                    endpoint=SUBMIT_ENDPOINT,
                    response_status=202,
                    response_body=body,
                )
            )
        await session.commit()
        return body

    @app.get("/sources/{source_id}")
    async def source_status(
        source_id: uuid.UUID,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
    ):
        """Pipeline state for a source (06 §1) — read off the denormalized pointer rather
        than scanning ingestion_log, since this is polled after every submit (09 §3)."""
        source = await session.get(RawSource, source_id)
        if source is None or source.submitted_by != f"user:{principal.id}":
            # Same response either way: whether a source exists is not public.
            raise ApiError(404, "not_found", f"No source {source_id}.")
        return {
            "source_id": str(source.source_id),
            "pipeline_state": source.pipeline_state.value,
            "status": source.status.value,
            "workspace_id": source.workspace_id,
            "filename": source.filename,
            # 03 §1's UI label for the placeholder source page — distinct from
            # `pipeline_state` (the raw enum) and from the page's own frontmatter status.
            "label": pipeline.placeholder_label(source.pipeline_state),
        }


async def _replay(
    session: AsyncSession, key: str, principal: Principal, endpoint: str
) -> dict | None:
    record = await session.get(IdempotencyRecord, (key, principal.id, endpoint))
    return record.response_body if record else None


async def _store(
    session: AsyncSession, payload: bytes, filename: str, *, submitted_by: str
) -> RawSource:
    """Write the object, create the raw_source row, and open its ingestion_log history."""
    source_id = uuid.uuid4()
    # Staged outside any workspace prefix: 02 §2's /{workspace_id}/sources/... scheme
    # cannot apply yet because 03 §2 accepts the source before the workspace is known.
    # See readiness item 0.6 — classification has to settle where it ends up.
    object_key = f"/_inbox/{source_id}/{filename}"
    objectstore.write_bytes(object_key, payload)

    source = RawSource(
        source_id=source_id,
        object_key=object_key,
        filename=filename,
        content_hash=hashlib.sha256(payload).hexdigest(),
        submitted_by=submitted_by,
        pipeline_state=PipelineState.submitted,
    )
    session.add(source)
    await session.flush()

    session.add(
        pipeline.IngestionLog(
            source_id=source.source_id,
            from_state=None,
            to_state=PipelineState.submitted,
            actor=submitted_by,
            detail={"object_key": object_key},
        )
    )
    # 03 §5: every submission gets an always-on informational review item, unconditionally
    # and regardless of what happens downstream. No workspace yet — none is resolved until
    # classification succeeds.
    await review.create(
        session, kind=ReviewKind.submission, subject_ref=str(source.source_id)
    )
    await session.flush()
    return source


async def find_by_hash(session: AsyncSession, content_hash: str) -> list[RawSource]:
    """Exact-duplicate lookup (03 §4). Here because submission is what populates it."""
    result = await session.execute(
        select(RawSource).where(RawSource.content_hash == content_hash)
    )
    return list(result.scalars())


app = create_app()
