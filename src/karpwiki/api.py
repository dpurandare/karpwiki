"""Common Gateway — submission, review-queue, page-version, document-type, and workspace
endpoints, and the conventions every endpoint shares.

Implements phase1-tasklist step 7 (03 §2's submission path, plus the two cross-cutting
pieces it is the first to need — principal resolution and role enforcement, 09 §15, and
the API conventions of 09 §14), step 19 (05 §1's review queue: `review-items` list and
`review-items/{id}/resolve` — the first list endpoint, so it's also where cursor
pagination, 09 §14, actually lands), step 20 (05 §6's Version Browser: `pages/{id}/
versions` list/get/diff and `pages/{id}/rollback`), and phase2-tasklist.md steps 22
(05 §7's document-type taxonomy CRUD: `document-types` list/create/update/delete), 23
(`workspaces` create/update/archive/list/get, plus access-policy grant/revoke — 05 §7,
06 §1, §3), and 25 (`GET /search` — federated resolution, the taxonomy pre-filter, and
`query_log` writes are gateway concerns per 01 §2, so they live here around a call into
`search.py` rather than in that module).

Not implemented here, deliberately: the rate limiter is 07 §3, a later phase (phase2-
tasklist.md step 48). `pages` get/list (06 §1's other row) isn't built either — out of this
file's steps' citations so far, and version history/rollback only ever take a `page_id`
path param, never a page-listing call to discover one. Dedicated-index score normalization
(04 §4) is step 26 — this endpoint only ever queries the one shared index.
"""

import hashlib
import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import (
    classify,
    document_types,
    ingestion,
    objectstore,
    pipeline,
    query_log,
    review,
    search,
    versioning,
    workspaces,
)
from .auth import (
    Authenticator,
    Principal,
    TrustedHeaderAuthenticator,
    any_workspace_with_role,
    has_role,
)
from .db import SessionLocal
from .models import (
    AccessPolicy,
    DocumentType,
    IdempotencyRecord,
    PageVersion,
    PipelineState,
    RawSource,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    Role,
    WikiPage,
    Workspace,
)

SUBMIT_ENDPOINT = "POST /sources"
RESOLVE_ENDPOINT = "POST /review-items/{id}/resolve"
ROLLBACK_ENDPOINT = "POST /pages/{id}/rollback"


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


class ResolveRequest(BaseModel):
    """POST /review-items/{id}/resolve body (06 §1, 05 §1).

    `action` means different things per `kind`: for `submission` it's always
    `"acknowledge"`; for `classification` it's the chosen `document_type` — the workspace
    is derived from it via the taxonomy's routing table (phase2-tasklist.md step 24), not
    supplied separately; for `duplicate` it's one of `reject`/`keep_both`/`supersede`/
    `merge` (03 §4).
    """

    action: str
    note: str | None = None


def _review_item_body(item: ReviewItem) -> dict[str, Any]:
    return {
        "review_id": str(item.review_id),
        "workspace_id": item.workspace_id,
        "kind": item.kind.value,
        "severity": item.severity,
        "subject_ref": item.subject_ref,
        "proposed_action": item.proposed_action,
        "status": item.status.value,
        "created_at": item.created_at.isoformat(),
        "resolved_action": item.resolved_action,
        "resolved_by": item.resolved_by,
        "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
    }


class RollbackRequest(BaseModel):
    """POST /pages/{id}/rollback body (06 §1, 05 §6, 01 §5)."""

    target_version_id: uuid.UUID
    change_summary: str | None = None


def _page_version_body(version: PageVersion, *, include_content: bool = False) -> dict[str, Any]:
    body = {
        "version_id": str(version.version_id),
        "page_id": str(version.page_id),
        "author": version.author,
        "created_at": version.created_at.isoformat(),
        "trigger": version.trigger.value,
        "change_summary": version.change_summary,
        "restored_from_version_id": (
            str(version.restored_from_version_id) if version.restored_from_version_id else None
        ),
    }
    if include_content:
        body["content"] = version.content
        body["frontmatter"] = version.frontmatter
    return body


async def _admin_page(session: AsyncSession, principal: Principal, page_id: uuid.UUID) -> WikiPage:
    """05 §6's Version Browser is admin-only (06 §1's `pages/{id}/versions` caller column)."""
    page = await session.get(WikiPage, page_id)
    if page is None:
        raise ApiError(404, "not_found", f"No page {page_id}.")
    if not await has_role(
        session, principal=principal, workspace_id=page.workspace_id, required=Role.admin
    ):
        raise ApiError(403, "forbidden", "This operation requires the admin role.")
    return page


class CreateDocumentTypeRequest(BaseModel):
    """POST /document-types body (06 §1, 05 §7)."""

    type_code: str
    workspace_id: str
    description: str | None = None


class UpdateDocumentTypeRequest(BaseModel):
    """POST /document-types/{type_code} body — rename, reassign, and/or redescribe (05 §7).
    `new_type_code`, not `type_code`: the path identifies which type is being updated."""

    new_type_code: str | None = None
    workspace_id: str | None = None
    description: str | None = None


def _document_type_body(doc_type: DocumentType) -> dict[str, Any]:
    return {
        "type_code": doc_type.type_code,
        "workspace_id": doc_type.workspace_id,
        "description": doc_type.description,
    }


async def _admin_document_type(
    session: AsyncSession, principal: Principal, type_code: str
) -> DocumentType:
    doc_type = await session.get(DocumentType, type_code)
    if doc_type is None:
        raise ApiError(404, "not_found", f"No document type {type_code!r}.")
    if not await has_role(
        session, principal=principal, workspace_id=doc_type.workspace_id, required=Role.admin
    ):
        raise ApiError(403, "forbidden", "This operation requires the admin role.")
    return doc_type


class CreateWorkspaceRequest(BaseModel):
    """POST /workspaces body (06 §1, 05 §7, 01 §3)."""

    workspace_id: str
    name: str
    description: str | None = None
    schema_ref: str | None = None
    storage_bindings: dict | None = None


class UpdateWorkspaceRequest(BaseModel):
    """POST /workspaces/{id} body — only supplied fields change."""

    name: str | None = None
    description: str | None = None
    schema_ref: str | None = None
    storage_bindings: dict | None = None


class GrantAccessRequest(BaseModel):
    """POST /workspaces/{id}/access-policy body (05 §7, 06 §3)."""

    principal: str
    role: Role


def _workspace_body(workspace: Workspace) -> dict[str, Any]:
    return {
        "workspace_id": workspace.workspace_id,
        "name": workspace.name,
        "description": workspace.description,
        "schema_ref": workspace.schema_ref,
        "status": workspace.status.value,
        "storage_bindings": workspace.storage_bindings,
    }


def _access_policy_body(policy: AccessPolicy) -> dict[str, Any]:
    return {
        "workspace_id": policy.workspace_id,
        "principal": policy.principal,
        "role": policy.role.value,
    }


async def _admin_workspace(
    session: AsyncSession, principal: Principal, workspace_id: str
) -> Workspace:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise ApiError(404, "not_found", f"No workspace {workspace_id!r}.")
    if not await has_role(
        session, principal=principal, workspace_id=workspace_id, required=Role.admin
    ):
        raise ApiError(403, "forbidden", "This operation requires the admin role.")
    return workspace


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

    @app.get("/review-items")
    async def list_review_items(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        workspace_id: str | None = None,
        kind: str | None = None,
        status: str | None = "open",
        severity: str | None = None,
        limit: int = review.DEFAULT_LIST_LIMIT,
        cursor: str | None = None,
    ):
        """05 §1's consolidated queue (06 §1). Admin-only — a caller with no admin grant
        anywhere has nothing to list, since every item is either workspace-scoped to a
        workspace they'd need to administer, or workspace-less (09 §22)."""
        admin_workspaces = await any_workspace_with_role(
            session, principal=principal, required=Role.admin
        )
        if not admin_workspaces:
            raise ApiError(
                403, "forbidden", "Listing review items requires the admin role somewhere."
            )

        items, next_cursor = await review.list_items(
            session,
            admin_workspaces=admin_workspaces,
            workspace_id=workspace_id,
            kind=_parse_enum(ReviewKind, kind, "kind") if kind else None,
            status=_parse_enum(ReviewStatus, status, "status") if status else None,
            severity=severity,
            limit=limit,
            cursor=cursor,
        )
        return {"items": [_review_item_body(i) for i in items], "next_cursor": next_cursor}

    @app.post("/review-items/{review_id}/resolve")
    async def resolve_review_item_endpoint(
        review_id: uuid.UUID,
        response: Response,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: ResolveRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        """Execute a resolution (06 §1, 05 §1) — action semantics depend on `kind`, see
        `ResolveRequest`. Admin-gated against the item's own workspace when it has one;
        against any workspace the caller administers when it doesn't yet (09 §22). A
        `classification` resolution additionally needs admin in the workspace the chosen
        `document_type` routes to (09 §27) — that workspace isn't known until `action` is
        read, so it's checked as a second, more specific gate below."""
        item = await session.get(ReviewItem, review_id)
        if item is None:
            raise ApiError(404, "not_found", f"No review item {review_id}.")

        if item.workspace_id is not None:
            authorized = await has_role(
                session, principal=principal, workspace_id=item.workspace_id, required=Role.admin
            )
        else:
            authorized = bool(
                await any_workspace_with_role(session, principal=principal, required=Role.admin)
            )
        if not authorized:
            raise ApiError(403, "forbidden", "Resolving this review item requires the admin role.")

        if item.kind is ReviewKind.classification:
            doc_type = await session.get(DocumentType, payload.action)
            if doc_type is None:
                raise ApiError(
                    400, "invalid_request", f"{payload.action!r} is not a registered document type."
                )
            if not await has_role(
                session,
                principal=principal,
                workspace_id=doc_type.workspace_id,
                required=Role.admin,
            ):
                raise ApiError(
                    403,
                    "forbidden",
                    "Resolving into this workspace requires the admin role there.",
                    {"workspace_id": doc_type.workspace_id},
                )

        if idempotency_key:
            replayed = await _replay(session, idempotency_key, principal, RESOLVE_ENDPOINT)
            if replayed is not None:
                response.headers["Idempotency-Replayed"] = "true"
                return replayed

        try:
            state = await ingestion.resolve_review_item(
                session,
                item=item,
                action=payload.action,
                actor=f"user:{principal.id}",
                note=payload.note,
            )
        except review.AlreadyResolvedError as exc:
            raise ApiError(409, "conflict", str(exc)) from exc
        except ingestion.InvalidResolutionError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc

        body = {
            "review_id": str(item.review_id),
            "status": item.status.value,
            "resolved_action": item.resolved_action,
            "pipeline_state": state.value if state is not None else None,
        }
        if idempotency_key:
            session.add(
                IdempotencyRecord(
                    key=idempotency_key,
                    principal=principal.id,
                    endpoint=RESOLVE_ENDPOINT,
                    response_status=200,
                    response_body=body,
                )
            )
        await session.commit()
        return body

    @app.get("/pages/{page_id}/versions/diff")
    async def diff_page_versions(
        page_id: uuid.UUID,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        from_version_id: uuid.UUID,
        to_version_id: uuid.UUID,
    ):
        """05 §6's diff view. Registered ahead of `/versions/{version_id}` below so this
        literal path isn't shadowed by that one's path parameter."""
        await _admin_page(session, principal, page_id)
        try:
            text = await versioning.diff(
                session, page_id=page_id, from_version_id=from_version_id, to_version_id=to_version_id
            )
        except ValueError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc
        return {
            "from_version_id": str(from_version_id),
            "to_version_id": str(to_version_id),
            "diff": text,
        }

    @app.get("/pages/{page_id}/versions/{version_id}")
    async def get_page_version(
        page_id: uuid.UUID,
        version_id: uuid.UUID,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
    ):
        await _admin_page(session, principal, page_id)
        version = await session.get(PageVersion, version_id)
        if version is None or version.page_id != page_id:
            raise ApiError(404, "not_found", f"No version {version_id} on page {page_id}.")
        return _page_version_body(version, include_content=True)

    @app.get("/pages/{page_id}/versions")
    async def list_page_versions(
        page_id: uuid.UUID,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        limit: int = versioning.DEFAULT_LIST_LIMIT,
        cursor: str | None = None,
    ):
        await _admin_page(session, principal, page_id)
        versions, next_cursor = await versioning.list_versions(
            session, page_id=page_id, limit=limit, cursor=cursor
        )
        return {
            "items": [_page_version_body(v) for v in versions],
            "next_cursor": next_cursor,
        }

    @app.post("/pages/{page_id}/rollback")
    async def rollback_page(
        page_id: uuid.UUID,
        response: Response,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: RollbackRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        page = await _admin_page(session, principal, page_id)

        if idempotency_key:
            replayed = await _replay(session, idempotency_key, principal, ROLLBACK_ENDPOINT)
            if replayed is not None:
                response.headers["Idempotency-Replayed"] = "true"
                return replayed

        try:
            version = await versioning.rollback(
                session,
                page=page,
                target_version_id=payload.target_version_id,
                author=f"user:{principal.id}",
                change_summary=payload.change_summary,
            )
        except ValueError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc

        # 05 §6: rollback is logged to log.md, not just admin_action_log (09 §23).
        await ingestion.refresh_log(session, workspace_id=page.workspace_id)

        body = _page_version_body(version)
        if idempotency_key:
            session.add(
                IdempotencyRecord(
                    key=idempotency_key,
                    principal=principal.id,
                    endpoint=ROLLBACK_ENDPOINT,
                    response_status=200,
                    response_body=body,
                )
            )
        await session.commit()
        return body

    @app.get("/document-types")
    async def list_document_types(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        workspace_id: str | None = None,
    ):
        """05 §7's taxonomy list — admin-only per 06 §1's `document-types` caller column
        (unlike `workspaces` list/get, this resource has no reader-visible half)."""
        if workspace_id is not None:
            if not await has_role(
                session, principal=principal, workspace_id=workspace_id, required=Role.admin
            ):
                raise ApiError(
                    403,
                    "forbidden",
                    "Listing document types for this workspace requires the admin role.",
                )
            types = await document_types.list_for_workspace(session, workspace_id=workspace_id)
        else:
            admin_workspaces = await any_workspace_with_role(
                session, principal=principal, required=Role.admin
            )
            if not admin_workspaces:
                raise ApiError(
                    403, "forbidden", "Listing document types requires the admin role somewhere."
                )
            types = await document_types.list_for_workspaces(
                session, workspace_ids=admin_workspaces
            )
        return {"items": [_document_type_body(t) for t in types]}

    @app.post("/document-types", status_code=201)
    async def create_document_type(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: CreateDocumentTypeRequest,
    ):
        if not await has_role(
            session, principal=principal, workspace_id=payload.workspace_id, required=Role.admin
        ):
            raise ApiError(
                403,
                "forbidden",
                "Creating a document type requires the admin role in its workspace.",
            )
        try:
            doc_type = await document_types.create(
                session,
                type_code=payload.type_code,
                workspace_id=payload.workspace_id,
                description=payload.description,
            )
        except document_types.DuplicateTypeCodeError as exc:
            raise ApiError(409, "conflict", str(exc)) from exc
        await session.commit()
        return _document_type_body(doc_type)

    @app.post("/document-types/{type_code}")
    async def update_document_type(
        type_code: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: UpdateDocumentTypeRequest,
    ):
        doc_type = await _admin_document_type(session, principal, type_code)
        # Reassigning to a new workspace requires admin in *that* workspace too — whichever
        # grant is more restrictive applies, same rule as on-behalf-of submission (09 §5).
        if payload.workspace_id is not None and payload.workspace_id != doc_type.workspace_id:
            if not await has_role(
                session,
                principal=principal,
                workspace_id=payload.workspace_id,
                required=Role.admin,
            ):
                raise ApiError(
                    403,
                    "forbidden",
                    "Reassigning into this workspace requires the admin role there.",
                    {"workspace_id": payload.workspace_id},
                )
        try:
            updated = await document_types.update(
                session,
                doc_type=doc_type,
                new_type_code=payload.new_type_code,
                workspace_id=payload.workspace_id,
                description=payload.description,
            )
        except document_types.DuplicateTypeCodeError as exc:
            raise ApiError(409, "conflict", str(exc)) from exc
        await session.commit()
        return _document_type_body(updated)

    @app.delete("/document-types/{type_code}", status_code=204)
    async def delete_document_type(
        type_code: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
    ):
        doc_type = await _admin_document_type(session, principal, type_code)
        await document_types.delete(session, doc_type=doc_type)
        await session.commit()

    @app.get("/workspaces")
    async def list_workspaces(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
    ):
        """06 §1: returns only workspaces the caller can access, any role — unlike
        `document-types`, this resource has a reader-visible half."""
        found = await workspaces.list_for_principal(session, principal_keys=principal.policy_keys)
        return {"items": [_workspace_body(w) for w in found]}

    @app.get("/workspaces/{workspace_id}")
    async def get_workspace(
        workspace_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
    ):
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None or not await has_role(
            session, principal=principal, workspace_id=workspace_id, required=Role.reader
        ):
            # Same response either way: whether a workspace exists is not public to a
            # caller with no access to it (mirrors /sources/{id}, step 7).
            raise ApiError(404, "not_found", f"No workspace {workspace_id!r}.")
        return _workspace_body(workspace)

    @app.post("/workspaces", status_code=201)
    async def create_workspace(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: CreateWorkspaceRequest,
    ):
        """06 §1: create requires admin. The target workspace doesn't exist yet, so this
        checks admin in at least one *existing* workspace — the same bootstrap answer
        09 §22 already gave for workspace-less review items, reused here rather than
        inventing the global-admin grant 09 §22 explicitly declined to build."""
        admin_workspaces = await any_workspace_with_role(
            session, principal=principal, required=Role.admin
        )
        if not admin_workspaces:
            raise ApiError(
                403, "forbidden", "Creating a workspace requires the admin role somewhere."
            )
        try:
            workspace = await workspaces.create(
                session,
                workspace_id=payload.workspace_id,
                name=payload.name,
                description=payload.description,
                schema_ref=payload.schema_ref,
                storage_bindings=payload.storage_bindings,
            )
        except workspaces.DuplicateWorkspaceError as exc:
            raise ApiError(409, "conflict", str(exc)) from exc
        # Without this, nobody could manage the workspace they just created through the API
        # at all — every other mutation here requires admin *in that workspace*, which
        # nothing yet grants once creation itself succeeds.
        await workspaces.grant(
            session, workspace_id=workspace.workspace_id, principal=principal.id, role=Role.admin
        )
        await session.commit()
        return _workspace_body(workspace)

    @app.post("/workspaces/{workspace_id}")
    async def update_workspace(
        workspace_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: UpdateWorkspaceRequest,
    ):
        workspace = await _admin_workspace(session, principal, workspace_id)
        updated = await workspaces.update(
            session,
            workspace=workspace,
            name=payload.name,
            description=payload.description,
            schema_ref=payload.schema_ref,
            storage_bindings=payload.storage_bindings,
        )
        await session.commit()
        return _workspace_body(updated)

    @app.post("/workspaces/{workspace_id}/archive")
    async def archive_workspace(
        workspace_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
    ):
        workspace = await _admin_workspace(session, principal, workspace_id)
        archived = await workspaces.archive(session, workspace=workspace)
        await session.commit()
        return _workspace_body(archived)

    @app.get("/workspaces/{workspace_id}/access-policy")
    async def list_access_policy(
        workspace_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
    ):
        await _admin_workspace(session, principal, workspace_id)
        grants = await workspaces.list_access(session, workspace_id=workspace_id)
        return {"items": [_access_policy_body(g) for g in grants]}

    @app.post("/workspaces/{workspace_id}/access-policy", status_code=201)
    async def grant_access_policy(
        workspace_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: GrantAccessRequest,
    ):
        await _admin_workspace(session, principal, workspace_id)
        granted = await workspaces.grant(
            session, workspace_id=workspace_id, principal=payload.principal, role=payload.role
        )
        await session.commit()
        return _access_policy_body(granted)

    @app.delete("/workspaces/{workspace_id}/access-policy/{revoked_principal}", status_code=204)
    async def revoke_access_policy(
        workspace_id: str,
        revoked_principal: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
    ):
        await _admin_workspace(session, principal, workspace_id)
        await workspaces.revoke(session, workspace_id=workspace_id, principal=revoked_principal)
        await session.commit()

    @app.get("/search")
    async def search_endpoint(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        q: str,
        workspace_id: Annotated[list[str] | None, Query()] = None,
        page_type: Annotated[list[str] | None, Query()] = None,
        tags: Annotated[list[str] | None, Query()] = None,
        date_from: date | None = None,
        date_to: date | None = None,
        include_drafts: bool = False,
        limit: int = 20,
    ):
        """04 §1, §4-8: single-stage federated lexical search — any authenticated caller
        (06 §1). Workspace resolution and the taxonomy pre-filter (04 §4, 01 §2's Workspace
        Router) and the `query_log` write (04 §8) are gateway concerns handled here; the
        retrieval itself is `search.search()`. Seeing drafts needs `contributor`, not just
        `reader` — "elevated scope" per 04 §6 — so resolution uses a stricter role when
        requested rather than filtering drafts out of a reader-scoped set after the fact.
        """
        required_role = Role.contributor if include_drafts else Role.reader
        accessible = await any_workspace_with_role(
            session, principal=principal, required=required_role
        )

        if workspace_id:
            # Intersected with what the caller can access, never expanded (04 §4).
            resolved = [w for w in workspace_id if w in accessible]
        else:
            resolved = await _taxonomy_prefilter(session, query=q, accessible=accessible)

        results = await search.search(
            session,
            query=q,
            workspace_ids=resolved,
            limit=limit,
            include_drafts=include_drafts,
            page_types=page_type,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
        )

        await query_log.record(
            session,
            principal=principal.id,
            query_text=q,
            resolved_workspaces=resolved,
            results=[{"page_id": str(r.page_id), "score": r.score} for r in results],
        )
        await session.commit()

        return {"items": [_search_result_body(r) for r in results]}


async def _taxonomy_prefilter(
    session: AsyncSession, *, query: str, accessible: list[str]
) -> list[str]:
    """04 §4's optional pre-filter: only applies when the caller didn't already scope the
    search explicitly (an accessible-workspace default, not a correction to an explicit
    choice). Reuses the same lexical taxonomy lookup 03 §3 runs at ingest
    (`classify.lexical_match`), here going from query text to a candidate workspace instead
    of from a document to one. Never expands beyond `accessible`; falls back to it
    unchanged on no confident match."""
    if not accessible:
        return accessible
    active_types = [dt.type_code for dt in await document_types.list_active(session)]
    lexical = classify.lexical_match(query, active_types)
    if lexical is None:
        return accessible
    target = await document_types.workspace_for_type(session, type_code=lexical.label)
    if target is None or target.workspace_id not in accessible:
        return accessible
    return [target.workspace_id]


def _search_result_body(result: search.SearchResult) -> dict[str, Any]:
    return {
        "page_id": str(result.page_id),
        "workspace_id": result.workspace_id,
        "path": result.path,
        "page_type": result.page_type,
        "title": result.title,
        "score": result.score,
        "excerpt": result.excerpt,
        "citations": list(result.citations),
    }


def _parse_enum(enum_cls, value: str, field: str):
    try:
        return enum_cls(value)
    except ValueError:
        valid = ", ".join(m.value for m in enum_cls)
        raise ApiError(
            400,
            "invalid_request",
            f"{value!r} is not a valid {field}.",
            {"field": field, "valid": valid},
        ) from None


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
