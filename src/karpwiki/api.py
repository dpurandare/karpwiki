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
06 §1, §3), 25 (`GET /search` — federated resolution, the taxonomy pre-filter, and
`query_log` writes are gateway concerns per 01 §2, so they live here around a call into
`search.py` rather than in that module), and 27 (`workspaces/{id}/bulk-move` preview and
execute — 05 §7, 09 §11 — the batch-loop/commit-per-batch boundary belongs here, not in
`bulk_move.py`, matching every other module's "caller commits" convention), and phase2-
tasklist.md step 43 (`pages` get/list — 06 §1's other row, previously unbuilt since
version history/rollback only ever took a `page_id` path param, never a listing call to
discover one; `sources` list — 05 §7's admin Raw Source Browser), and step 51
(`connectors` list/create/update — the real `Connector` model and its own admin CRUD,
storage only; the polling worker pool that actually runs one is step 52).

"""

import logging
import time
import uuid
from datetime import date
from typing import Annotated, Any

import redis.asyncio as redis
from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import (
    analytics,
    bulk_move,
    classify,
    config,
    connectors,
    dedicated_index,
    document_types,
    ingestion,
    monitoring,
    notifications,
    pipeline,
    query_log,
    ratelimit,
    review,
    schema,
    search,
    tasks,
    versioning,
    wiki_export,
    workspaces,
)
from .auth import (
    Authenticator,
    Principal,
    any_workspace_with_role,
    default_authenticator,
    has_role,
    has_role_for_page,
    page_type_scope,
    visible_page_types,
)
from .db import SessionLocal
from .models import (
    AccessPolicy,
    Connector,
    ConnectorState,
    DocumentType,
    FeedbackRating,
    IdempotencyRecord,
    PageLink,
    PageStatus,
    PageType,
    PageVersion,
    PipelineState,
    QueryFeedback,
    QueryLog,
    RawSource,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    Role,
    SchemaVersion,
    WikiPage,
    Workspace,
)
from .search_result import DEFAULT_SEARCH_LIMIT

logger = logging.getLogger(__name__)

SUBMIT_ENDPOINT = "POST /sources"
BULK_SUBMIT_ENDPOINT = "POST /sources/bulk"
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


def _rate_limit_category(request: Request) -> str:
    if request.method == "POST" and request.url.path in ("/sources", "/sources/bulk"):
        return "submit"
    if request.method == "GET" and request.url.path == "/search":
        return "search"
    return "general"


def _rate_limit_workspace_id(request: Request) -> str | None:
    """Opportunistic only (`ratelimit.py`'s module docstring) — a plain query or path
    param, never resolved via taxonomy pre-filter or classification."""
    return request.path_params.get("workspace_id") or request.query_params.get("workspace_id")


def create_app(authenticator: Authenticator | None = None) -> FastAPI:
    app = FastAPI(title="karpwiki gateway")
    app.state.authenticator = authenticator or default_authenticator()
    # Lazy connection pool — constructing this never blocks or binds to an event loop by
    # itself (unlike a client that connects eagerly), so it's safe to create here at
    # `create_app()` time rather than per-request; each real request's own event loop is
    # whichever one is running when a command is actually issued.
    app.state.redis = redis.from_url(config.CELERY_BROKER_URL)

    # Read once per app instance, not at module-import time — a test's `client` fixture
    # calls `create_app()` fresh per test, so monkeypatching `config.RATE_LIMIT_*` before
    # that call (conftest.py's `generous_rate_limits`) actually takes effect. 07 §3's own
    # three categories ("submissions, search calls, and API requests") — mutually
    # exclusive, by path/method; "API requests" is the general catch-all, not a fourth
    # layer on top of the other two. `(per_principal_limit, per_workspace_limit)`.
    rate_limit_categories: dict[str, tuple[int, int]] = {
        "submit": (config.RATE_LIMIT_SUBMIT_PER_PRINCIPAL, config.RATE_LIMIT_SUBMIT_PER_WORKSPACE),
        "search": (config.RATE_LIMIT_SEARCH_PER_PRINCIPAL, config.RATE_LIMIT_SEARCH_PER_WORKSPACE),
        "general": (config.RATE_LIMIT_GENERAL_PER_PRINCIPAL, config.RATE_LIMIT_GENERAL_PER_WORKSPACE),
    }

    @app.middleware("http")
    async def enforce_rate_limit(request: Request, call_next):
        """09 §14's `RateLimit-*`/`Retry-After` header contract, phase2-tasklist.md step
        48 — registered *before* `attach_request_id` below so it ends up the inner
        middleware (Starlette wraps in reverse registration order) and always sees a
        real `request.state.request_id` already set, for a consistent 429 body.

        `/healthz` (step 49) is exempt entirely, not folded into "general" — it's an
        infra liveness probe hit by every gateway instance's own Docker healthcheck and
        potentially the load balancer, not one of 07 §3's three real API categories; the
        real risk otherwise is self-inflicted (many replicas' healthchecks all present no
        auth header, so they'd all share the same "anon" Redis counter and could throttle
        each other's own healthcheck into failing)."""
        if request.url.path == "/healthz":
            return await call_next(request)

        category = _rate_limit_category(request)
        principal_limit, workspace_limit = rate_limit_categories[category]
        window = config.RATE_LIMIT_WINDOW_SECONDS

        principal_result = await ratelimit.check(
            request.app.state.redis,
            key=f"ratelimit:{category}:principal:{ratelimit.principal_key(dict(request.headers))}",
            limit=principal_limit,
            window_seconds=window,
        )
        workspace_result = None
        workspace_id = _rate_limit_workspace_id(request)
        if workspace_id:
            workspace_result = await ratelimit.check(
                request.app.state.redis,
                key=f"ratelimit:{category}:workspace:{workspace_id}",
                limit=workspace_limit,
                window_seconds=window,
            )

        breached = not principal_result.allowed or (workspace_result is not None and not workspace_result.allowed)
        # Report whichever bound is actually binding: the one that failed, or (if both
        # passed) the one with less headroom left — the caller's next request is
        # constrained by that one regardless of which counter reports it.
        if not principal_result.allowed:
            reported = principal_result
        elif workspace_result is not None and not workspace_result.allowed:
            reported = workspace_result
        elif workspace_result is not None and workspace_result.remaining < principal_result.remaining:
            reported = workspace_result
        else:
            reported = principal_result

        if breached:
            response = JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "type": "rate_limited",
                        "message": "Rate limit exceeded.",
                        "request_id": request.state.request_id,
                    }
                },
            )
            response.headers["Retry-After"] = str(reported.reset_seconds)
        else:
            response = await call_next(request)

        response.headers["RateLimit-Limit"] = str(reported.limit)
        response.headers["RateLimit-Remaining"] = str(reported.remaining)
        response.headers["RateLimit-Reset"] = str(reported.reset_seconds)
        return response

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
    resolved = await request.app.state.authenticator.authenticate(dict(request.headers))
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
        "detail": item.detail,
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


def _page_summary_body(row: versioning.PageSummary) -> dict[str, Any]:
    return {
        "page_id": str(row.page_id),
        "workspace_id": row.workspace_id,
        "path": row.path,
        "page_type": row.page_type,
        "status": row.status,
        "title": row.title,
        "description": row.description,
        "tags": row.tags,
        "date": row.date,
        # Content quality scoring (07 §4, phase3-tasklist.md step 69) — sortable Admin
        # Console column; null for a page never touched by curate_source (source/
        # overview/index/log pages, or one only ever hand-edited).
        "quality_score": row.quality_score,
    }


def _page_body(page: WikiPage, version: PageVersion | None) -> dict[str, Any]:
    body = {
        "page_id": str(page.page_id),
        "workspace_id": page.workspace_id,
        "path": page.path,
        "page_type": page.page_type.value,
        "status": page.status.value,
        "current_version_id": str(page.current_version_id) if page.current_version_id else None,
        "quality_score": page.quality_score,
    }
    if version is not None:
        body["title"] = version.frontmatter.get("title")
        body["description"] = version.frontmatter.get("description")
        body["tags"] = version.frontmatter.get("tags")
        body["date"] = version.frontmatter.get("date")
        body["content"] = version.content
    return body


async def _reader_page(session: AsyncSession, principal: Principal, page_id: uuid.UUID) -> WikiPage:
    """`GET /pages/{id}` (06 §1) — reader-visible, unlike `_admin_page` above. A `draft`
    page needs `contributor` instead: the same "elevated scope" `/search`'s
    `include_drafts` already established for this exact content model (04 §6) — an
    unreviewed page shouldn't leak to a mere reader just because this is a plainer
    lookup than search. `has_role_for_page` (not `has_role`) since step 70: a workspace
    role alone isn't enough once this page's `page_type` has been scope-restricted (07
    §2)."""
    page = await session.get(WikiPage, page_id)
    if page is None:
        raise ApiError(404, "not_found", f"No page {page_id}.")
    required = Role.contributor if page.status is PageStatus.draft else Role.reader
    if not await has_role_for_page(session, principal=principal, page=page, required=required):
        raise ApiError(403, "forbidden", f"Viewing this page requires the {required.value} role.")
    return page


async def _resolve_page_links(
    session: AsyncSession, principal: Principal, page: WikiPage
) -> list[dict[str, Any]]:
    """01 §3: "Gateway re-checks the caller's AuthZ against the *target* workspace before
    resolving a link... into another" — the read-time half `page_links.sync` (02 §3,
    phase2-tasklist.md step 28) was explicitly built without yet, since no caller existed.

    Reads the already-parsed `page_link` rows (no markdown re-parsing needed at read time)
    and applies the exact same access check `_reader_page` uses for a direct fetch of that
    target — same required role (`contributor` if the target is still `draft`, matching
    `_reader_page`'s own "elevated scope" reasoning, not just a workspace-level check),
    and since step 70 the same page_type-scope narrowing too (`has_role_for_page`) —
    inlined rather than calling `_reader_page` again since the target `WikiPage` row is
    already in hand from the join below.

    A link that fails the check is simply omitted, not included-but-flagged-inaccessible:
    `01` §3 says AuthZ is re-checked "before resolving," so an unauthorized target's very
    existence (id, path, even that a link once pointed there) is never confirmed to the
    caller — the same omission step 70's own fine-grained checks below use. Archived-
    workspace targets are never excluded on that basis alone (`01` §3: "archived workspaces
    remain queryable") — only the same role check applies.
    """
    result = await session.execute(
        select(PageLink, WikiPage)
        .join(WikiPage, WikiPage.page_id == PageLink.to_page_id)
        .where(PageLink.from_page_id == page.page_id)
    )
    links = []
    for link, target in result.all():
        required = Role.contributor if target.status is PageStatus.draft else Role.reader
        if not await has_role_for_page(session, principal=principal, page=target, required=required):
            continue
        links.append(
            {
                "page_id": str(target.page_id),
                "workspace_id": target.workspace_id,
                "path": target.path,
                "link_type": link.link_type.value,
            }
        )
    return links


_ALL_PAGE_TYPE_VALUES = {pt.value for pt in PageType}


async def _visible_page_type_filter(
    session: AsyncSession,
    principal: Principal,
    *,
    workspace_id: str,
    required: Role,
    requested: list[str] | None,
) -> list[str] | None:
    """Intersects a caller's own `page_type` filter (if any, `GET /pages`/`GET /search`)
    with what page_type scoping (07 §2, phase3-tasklist.md step 70) allows this principal
    to see. `None` means "no filter at all" — preserves `versioning.list_pages`'s/
    `search.search()`'s exact prior query when nothing in this workspace has ever been
    scope-restricted, rather than a pointless full-enumeration filter that changes
    nothing. An empty list is a real, distinct answer ("nothing this principal may see
    matches") — the caller must check for it and short-circuit before querying, since
    `list_pages`/`search.search()` both treat an empty *filter* list as "no filter"."""
    visible = await visible_page_types(
        session, principal=principal, workspace_id=workspace_id, required=required
    )
    if {pt.value for pt in visible} == _ALL_PAGE_TYPE_VALUES:
        return requested
    visible_values = {pt.value for pt in visible}
    allowed = (set(requested) & visible_values) if requested else visible_values
    return sorted(allowed)


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


def _source_body(source: RawSource) -> dict[str, Any]:
    return {
        "source_id": str(source.source_id),
        "workspace_id": source.workspace_id,
        "filename": source.filename,
        "status": source.status.value,
        "pipeline_state": source.pipeline_state.value,
        "submitted_by": source.submitted_by,
        "supersedes": str(source.supersedes) if source.supersedes else None,
        "superseded_at": source.superseded_at.isoformat() if source.superseded_at else None,
        "ingested_at": source.ingested_at.isoformat() if source.ingested_at else None,
        "created_at": source.created_at.isoformat(),
    }


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


INGESTION_POLICIES = ("auto", "gated")


class CreateConnectorRequest(BaseModel):
    """POST /connectors body (06 §1, 05 §7, 09 §13, phase2-tasklist.md step 51).

    `credential_ref` must already be a pointer into the deployment's own secrets manager —
    this endpoint never accepts or stores a raw credential value (09 §13's "never stored in
    the Metadata DB"); resolving the ref into a real secret happens only at poll time,
    in-memory (`secrets_manager.py`, step 53).
    `config`/`schedule` are opaque, connector-type-specific JSON (no type is implemented
    until step 54, so nothing here validates their shape).
    """

    workspace_id: str
    type: str
    config: dict = {}
    credential_ref: str | None = None
    schedule: dict = {}
    ingestion_policy: str = "auto"


class UpdateConnectorRequest(BaseModel):
    """POST /connectors/{id} body — only supplied fields change. `workspace_id` is
    deliberately not reassignable, see `models.Connector`'s docstring."""

    type: str | None = None
    config: dict | None = None
    credential_ref: str | None = None
    schedule: dict | None = None
    ingestion_policy: str | None = None
    state: ConnectorState | None = None


def _connector_body(connector: Connector) -> dict[str, Any]:
    return {
        "connector_id": str(connector.connector_id),
        "workspace_id": connector.workspace_id,
        "type": connector.type,
        "config": connector.config,
        # The pointer name only, per 09 §13 — never the secret it points at, which this
        # table never holds in the first place.
        "credential_ref": connector.credential_ref,
        "schedule": connector.schedule,
        "ingestion_policy": connector.ingestion_policy,
        "state": connector.state.value,
        "last_sync_cursor": connector.last_sync_cursor,
        "last_run_at": connector.last_run_at.isoformat() if connector.last_run_at else None,
        "last_run_detail": connector.last_run_detail,
    }


async def _admin_connector(
    session: AsyncSession, principal: Principal, connector_id: uuid.UUID
) -> Connector:
    connector = await session.get(Connector, connector_id)
    if connector is None:
        raise ApiError(404, "not_found", f"No connector {connector_id}.")
    if not await has_role(
        session, principal=principal, workspace_id=connector.workspace_id, required=Role.admin
    ):
        raise ApiError(403, "forbidden", "This operation requires the admin role.")
    return connector


class CreateWorkspaceRequest(BaseModel):
    """POST /workspaces body (06 §1, 05 §7, 01 §3). No `schema_ref` — since phase3-tasklist.md
    step 59, SCHEMA.md has real, versioned content written through `POST
    /workspaces/{id}/schema` instead of a caller-supplied pointer string."""

    workspace_id: str
    name: str
    description: str | None = None
    storage_bindings: dict | None = None


class UpdateWorkspaceRequest(BaseModel):
    """POST /workspaces/{id} body — only supplied fields change. `dedicated_index` switches
    a workspace onto the OpenSearch backend (02 §4, 06 §6 — no automatic trigger; an admin
    decides once a workspace approaches scale)."""

    name: str | None = None
    description: str | None = None
    storage_bindings: dict | None = None
    dedicated_index: bool | None = None


class WriteSchemaRequest(BaseModel):
    """POST /workspaces/{id}/schema body (01 §7, 09 §6, phase3-tasklist.md step 59)."""

    content: str
    change_summary: str | None = None


class RollbackSchemaRequest(BaseModel):
    """POST /workspaces/{id}/schema/rollback body — mirrors `RollbackRequest` for pages."""

    target_version_id: uuid.UUID
    change_summary: str | None = None


class GrantAccessRequest(BaseModel):
    """POST /workspaces/{id}/access-policy body (05 §7, 06 §3). `fuse_access` (09 §12,
    phase3-tasklist.md step 58) is optional and orthogonal to `role` — omitted leaves an
    existing grant's value unchanged. `page_type` (07 §2, step 70) is also optional and
    orthogonal to `fuse_access` — omitted grants workspace-wide, same as every grant
    before step 70; naming a `page_type` grants (or updates) a separate, narrower row
    scoped to just that type instead."""

    principal: str
    role: Role
    fuse_access: bool | None = None
    page_type: PageType | None = None


class BulkMoveRequest(BaseModel):
    """POST /workspaces/{id}/bulk-move(/preview) body (05 §7, 09 §11) — {id} in the path is
    the source workspace; `target_workspace_id` is where `page_ids`/`source_ids` move to."""

    target_workspace_id: str
    page_ids: list[uuid.UUID] = []
    source_ids: list[uuid.UUID] = []


class SubmitFeedbackRequest(BaseModel):
    """POST /search/{query_id}/feedback body (07 §4, phase3-tasklist.md step 68)."""

    page_id: uuid.UUID
    rating: FeedbackRating


def _move_item_body(item: bulk_move.MoveItem) -> dict[str, Any]:
    return {"id": str(item.id), "label": item.label}


def _skipped_item_body(item: bulk_move.SkippedItem) -> dict[str, Any]:
    return {"id": str(item.id), "reason": item.reason}


def _workspace_body(workspace: Workspace) -> dict[str, Any]:
    return {
        "workspace_id": workspace.workspace_id,
        "name": workspace.name,
        "description": workspace.description,
        # 01 §3's own definition ("pointer to this workspace's SCHEMA.md") — since step 59
        # this is the real current SchemaVersion's id, not a caller-supplied free-text
        # string; kept as the same JSON key for API stability.
        "schema_ref": (
            str(workspace.current_schema_version_id)
            if workspace.current_schema_version_id
            else None
        ),
        "status": workspace.status.value,
        "storage_bindings": workspace.storage_bindings,
        "dedicated_index": workspace.dedicated_index,
    }


def _schema_version_body(version: SchemaVersion, *, include_content: bool = False) -> dict[str, Any]:
    body = {
        "version_id": str(version.version_id),
        "workspace_id": version.workspace_id,
        "author": version.author,
        "created_at": version.created_at.isoformat(),
        "change_summary": version.change_summary,
        "restored_from_version_id": (
            str(version.restored_from_version_id) if version.restored_from_version_id else None
        ),
    }
    if include_content:
        body["content"] = version.content
    return body


def _access_policy_body(policy: AccessPolicy) -> dict[str, Any]:
    return {
        "workspace_id": policy.workspace_id,
        "principal": policy.principal,
        "role": policy.role.value,
        "fuse_access": policy.fuse_access,
        # step 70 (07 §2): `None` for the (still typical) workspace-wide row; the scoped
        # page_type value for a narrower one, read back off the raw `scope` string.
        "page_type": policy.scope.removeprefix("page_type:") if policy.scope else None,
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
    @app.get("/healthz")
    async def healthz():
        """Liveness probe (06 §5, phase2-tasklist.md step 49) — no auth, no DB touch,
        exempt from rate limiting (see `enforce_rate_limit` above). Docker's own
        healthcheck and the load balancer's upstream check hit this, not a real client."""
        return {"status": "ok"}

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

        try:
            source = await ingestion.store(
                session, payload, filename, submitted_by=f"user:{principal.id}"
            )
        except ingestion.UnsupportedContentError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc
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
        # 03 §2/phase2-tasklist.md step 32: submission enqueues classification. Dispatched
        # only after commit — the classification task opens its own session and must see
        # this source row.
        tasks.classify_source.delay(str(source.source_id))
        return body

    @app.post("/sources/bulk", status_code=202)
    async def bulk_submit_sources(
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        files: Annotated[list[UploadFile], File()],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        """Bulk import (07 §5, phase3-tasklist.md step 74) — "seed a new workspace from an
        existing document repository." No `workspace_id` parameter: `classify_source` routes
        against the whole central taxonomy exactly like a single `POST /sources` call
        (03 §3's own no-workspace-hint design) — this endpoint is a real batch of that SAME
        pipeline, not a second, workspace-scoped path. "Bypassing per-document review-item
        noise" (07 §5's own wording, confirmed via AskUserQuestion) means no more manual
        per-file `POST /sources` calls, not a change to when a review item gets raised — a
        document that's genuinely ambiguous still raises its own item exactly as today.

        Gated at admin (not the ordinary contributor bar `POST /sources` uses): this is
        framed as "Admin tooling" (07 §5), and a single call fanning out to N real pipelines
        is a higher-leverage entry point worth the higher bar.

        An unreadable file (`UnsupportedContentError`) is skipped, not a whole-batch
        failure — the same per-item-skip-not-whole-run-crash precedent `connector_polling.
        poll_connector` already established for this exact error (phase3-tasklist.md step
        78) — so one bad file among many doesn't force the admin to resubmit everything.
        """
        if not await any_workspace_with_role(session, principal=principal, required=Role.admin):
            raise ApiError(
                403,
                "forbidden",
                "Bulk import requires the admin role in at least one workspace.",
                {"principal": principal.id},
            )

        if idempotency_key:
            replayed = await _replay(session, idempotency_key, principal, BULK_SUBMIT_ENDPOINT)
            if replayed is not None:
                response.headers["Idempotency-Replayed"] = "true"
                return replayed

        submitted: list[dict] = []
        rejected: list[dict] = []
        for upload in files:
            payload, filename = await upload.read(), upload.filename or "upload"
            try:
                source = await ingestion.store(
                    session, payload, filename, submitted_by=f"user:{principal.id}"
                )
            except ingestion.UnsupportedContentError as exc:
                rejected.append({"filename": filename, "error": str(exc)})
                continue
            submitted.append(
                {
                    "source_id": str(source.source_id),
                    "pipeline_state": source.pipeline_state.value,
                    "filename": source.filename,
                }
            )
        body = {"submitted": submitted, "rejected": rejected}
        if idempotency_key:
            session.add(
                IdempotencyRecord(
                    key=idempotency_key,
                    principal=principal.id,
                    endpoint=BULK_SUBMIT_ENDPOINT,
                    response_status=202,
                    response_body=body,
                )
            )
        await session.commit()
        # Dispatched only after commit, same discipline every other submission path uses.
        for entry in submitted:
            tasks.classify_source.delay(entry["source_id"])
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

    @app.get("/sources")
    async def list_sources_endpoint(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        workspace_id: str,
        status: str | None = None,
        limit: int = ingestion.DEFAULT_LIST_LIMIT,
        cursor: str | None = None,
    ):
        """05 §7's admin Raw Source Browser (06 §1's `connectors` row's sibling; `sources`
        itself has no dedicated 06 §1 row beyond `submit`/`get status`, so this follows
        `document-types`' own admin-list shape). Each item carries its raw `supersedes`
        pointer — a client walks a full chain by following it through this same list."""
        if not await has_role(
            session, principal=principal, workspace_id=workspace_id, required=Role.admin
        ):
            raise ApiError(403, "forbidden", "Listing sources requires the admin role.")

        sources, next_cursor = await ingestion.list_sources(
            session, workspace_id=workspace_id, status=status, limit=limit, cursor=cursor
        )
        return {
            "items": [_source_body(s) for s in sources],
            "next_cursor": next_cursor,
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
        `ResolveRequest`. Idempotency-key replay is REST-specific (no equivalent MCP
        convention, 06 §2), so it wraps `run_resolve_review_item` below rather than living
        inside it — that function is the shared Common Gateway logic (01 §2) the MCP
        `wiki_resolve_review_item` tool (phase2-tasklist.md step 45) also calls, with no
        idempotency support of its own."""
        if idempotency_key:
            replayed = await _replay(session, idempotency_key, principal, RESOLVE_ENDPOINT)
            if replayed is not None:
                response.headers["Idempotency-Replayed"] = "true"
                return replayed

        body = await run_resolve_review_item(
            session, principal, review_id=review_id, action=payload.action, note=payload.note
        )

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

    @app.get("/pages")
    async def list_pages_endpoint(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        workspace_id: str,
        page_type: Annotated[list[str] | None, Query()] = None,
        tags: Annotated[list[str] | None, Query()] = None,
        date_from: date | None = None,
        date_to: date | None = None,
        status: str | None = None,
        limit: int = versioning.DEFAULT_LIST_LIMIT,
        cursor: str | None = None,
    ):
        """06 §1's `pages` list — workspace-scoped like every resource but `/search` (06
        §1's own framing), so `workspace_id` is required rather than resolved across every
        accessible workspace. `status=draft` needs `contributor` (elevated scope, same
        reasoning as `_reader_page` above); the default (no `status` filter) and an
        explicit `published`/`archived` stay reader-visible."""
        required = Role.contributor if status == PageStatus.draft.value else Role.reader
        if not await has_role(session, principal=principal, workspace_id=workspace_id, required=required):
            raise ApiError(403, "forbidden", f"Listing pages here requires the {required.value} role.")

        # Step 70 (07 §2): narrow the requested page_type filter to what scoping allows —
        # an empty result here means a real "nothing this principal may see," not "no
        # filter," so it short-circuits before ever querying (see the helper's own note).
        effective_page_types = await _visible_page_type_filter(
            session, principal, workspace_id=workspace_id, required=required, requested=page_type
        )
        if effective_page_types == []:
            return {"items": [], "next_cursor": None}

        statuses = [status] if status else [PageStatus.published.value]
        pages, next_cursor = await versioning.list_pages(
            session,
            workspace_id=workspace_id,
            page_types=effective_page_types,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
            statuses=statuses,
            limit=limit,
            cursor=cursor,
        )
        return {"items": [_page_summary_body(p) for p in pages], "next_cursor": next_cursor}

    @app.get("/pages/{page_id}")
    async def get_page_endpoint(
        page_id: uuid.UUID,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
    ):
        page = await _reader_page(session, principal, page_id)
        version = (
            await session.get(PageVersion, page.current_version_id)
            if page.current_version_id
            else None
        )
        body = _page_body(page, version)
        # 01 §3, phase3-tasklist.md step 63: resolved, AuthZ-checked link targets — the
        # raw `content` field's embedded markdown link syntax is unresolved on purpose
        # (it's the stored document verbatim); this is the separate, structured field a
        # renderer/agent actually follows.
        body["links"] = await _resolve_page_links(session, principal, page)
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

        # 05 §6: rollback is logged to log.md, not just admin_action_log (09 §23); a
        # rollback can also change the page's title/description, so its index.md catalog
        # entry needs refreshing too (phase3-tasklist.md step 60).
        await ingestion.refresh_log(session, workspace_id=page.workspace_id)
        await ingestion.refresh_index(session, workspace_id=page.workspace_id)

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
        # 02 §7/phase2-tasklist.md step 32: a page write enqueues reindex — the exact page
        # is already known here, no need for a workspace-scoped sweep like `tasks._curate`'s.
        tasks.reindex.delay(str(page.page_id))
        return body

    @app.get("/document-types")
    async def list_document_types(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        workspace_id: str | None = None,
        limit: int = document_types.DEFAULT_LIST_LIMIT,
    ):
        """05 §7's taxonomy list — admin-only per 06 §1's `document-types` caller column
        (unlike `workspaces` list/get, this resource has no reader-visible half).

        Deliberately not cursor-paginated (09 §14, phase3-tasklist.md step 66) — a
        deployment-config-cardinality list (a workspace's own taxonomy, or an admin's
        taxonomy across every workspace they administer), not an append-heavy content
        table; `limit` (capped, no `next_cursor`) is the honest contract instead."""
        if workspace_id is not None:
            if not await has_role(
                session, principal=principal, workspace_id=workspace_id, required=Role.admin
            ):
                raise ApiError(
                    403,
                    "forbidden",
                    "Listing document types for this workspace requires the admin role.",
                )
            types = await document_types.list_for_workspace(
                session, workspace_id=workspace_id, limit=limit
            )
        else:
            admin_workspaces = await any_workspace_with_role(
                session, principal=principal, required=Role.admin
            )
            if not admin_workspaces:
                raise ApiError(
                    403, "forbidden", "Listing document types requires the admin role somewhere."
                )
            types = await document_types.list_for_workspaces(
                session, workspace_ids=admin_workspaces, limit=limit
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

    @app.get("/connectors")
    async def list_connectors(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        workspace_id: str | None = None,
        limit: int = connectors.DEFAULT_LIST_LIMIT,
    ):
        """06 §1's `connectors` list (phase2-tasklist.md step 51) — same admin-scoping
        shape as `document-types`' list, including its "deliberately capped, not cursor-
        paginated" reasoning (09 §14, phase3-tasklist.md step 66)."""
        if workspace_id is not None:
            if not await has_role(
                session, principal=principal, workspace_id=workspace_id, required=Role.admin
            ):
                raise ApiError(
                    403, "forbidden", "Listing connectors for this workspace requires the admin role."
                )
            found = await connectors.list_for_workspace(
                session, workspace_id=workspace_id, limit=limit
            )
        else:
            admin_workspaces = await any_workspace_with_role(
                session, principal=principal, required=Role.admin
            )
            if not admin_workspaces:
                raise ApiError(
                    403, "forbidden", "Listing connectors requires the admin role somewhere."
                )
            found = await connectors.list_for_workspaces(
                session, workspace_ids=admin_workspaces, limit=limit
            )
        return {"items": [_connector_body(c) for c in found]}

    @app.post("/connectors", status_code=201)
    async def create_connector(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: CreateConnectorRequest,
    ):
        """06 §1's `connectors` configure (create half), 09 §13 — admin-gated on the
        connector's own target workspace, same as `POST /document-types`."""
        if not await has_role(
            session, principal=principal, workspace_id=payload.workspace_id, required=Role.admin
        ):
            raise ApiError(
                403, "forbidden", "Creating a connector requires the admin role in its workspace."
            )
        if payload.ingestion_policy not in INGESTION_POLICIES:
            raise ApiError(
                400,
                "invalid_request",
                f"ingestion_policy must be one of {INGESTION_POLICIES}.",
                {"ingestion_policy": payload.ingestion_policy},
            )
        connector = await connectors.create(
            session,
            workspace_id=payload.workspace_id,
            type=payload.type,
            config=payload.config,
            credential_ref=payload.credential_ref,
            schedule=payload.schedule,
            ingestion_policy=payload.ingestion_policy,
        )
        await session.commit()
        return _connector_body(connector)

    @app.post("/connectors/{connector_id}")
    async def update_connector(
        connector_id: uuid.UUID,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: UpdateConnectorRequest,
    ):
        """06 §1's `connectors` configure (update half) — schedule/policy/credential/state,
        09 §13's admin-driven enable/disable included via `state`."""
        connector = await _admin_connector(session, principal, connector_id)
        if payload.ingestion_policy is not None and payload.ingestion_policy not in INGESTION_POLICIES:
            raise ApiError(
                400,
                "invalid_request",
                f"ingestion_policy must be one of {INGESTION_POLICIES}.",
                {"ingestion_policy": payload.ingestion_policy},
            )
        updated = await connectors.update(
            session,
            connector=connector,
            type=payload.type,
            config=payload.config,
            credential_ref=payload.credential_ref,
            schedule=payload.schedule,
            ingestion_policy=payload.ingestion_policy,
            state=payload.state,
        )
        await session.commit()
        return _connector_body(updated)

    @app.get("/workspaces")
    async def list_workspaces(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        limit: int = workspaces.DEFAULT_LIST_LIMIT,
    ):
        """06 §1: returns only workspaces the caller can access, any role — unlike
        `document-types`, this resource has a reader-visible half. Deliberately capped, not
        cursor-paginated (09 §14, phase3-tasklist.md step 66) — a deployment's own
        workspace count, not an append-heavy content table."""
        found = await workspaces.list_for_principal(
            session, principal_keys=principal.policy_keys, limit=limit
        )
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
            storage_bindings=payload.storage_bindings,
            dedicated_index=payload.dedicated_index,
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

    @app.get("/workspaces/{workspace_id}/schema")
    async def get_schema(
        workspace_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
    ):
        """01 §7, 09 §6, phase3-tasklist.md step 59. Admin-only, same as the access-policy
        endpoints below — workspace governance configuration, not plain metadata (unlike
        `GET /workspaces/{id}` itself, which any `reader` can see)."""
        await _admin_workspace(session, principal, workspace_id)
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None or workspace.current_schema_version_id is None:
            raise ApiError(404, "not_found", f"No schema configured for workspace {workspace_id!r}.")
        version = await session.get(SchemaVersion, workspace.current_schema_version_id)
        return _schema_version_body(version, include_content=True)

    @app.get("/workspaces/{workspace_id}/schema/versions")
    async def list_schema_versions(
        workspace_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        limit: int = schema.DEFAULT_LIST_LIMIT,
        cursor: str | None = None,
    ):
        await _admin_workspace(session, principal, workspace_id)
        versions, next_cursor = await schema.history(
            session, workspace_id=workspace_id, limit=limit, cursor=cursor
        )
        return {"items": [_schema_version_body(v) for v in versions], "next_cursor": next_cursor}

    @app.post("/workspaces/{workspace_id}/schema", status_code=201)
    async def write_schema(
        workspace_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: WriteSchemaRequest,
    ):
        workspace = await _admin_workspace(session, principal, workspace_id)
        try:
            version = await schema.write(
                session,
                workspace=workspace,
                content=payload.content,
                author=f"user:{principal.id}",
                change_summary=payload.change_summary,
            )
        except schema.SchemaValidationError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc
        await session.commit()
        return _schema_version_body(version, include_content=True)

    @app.post("/workspaces/{workspace_id}/schema/rollback")
    async def rollback_schema(
        workspace_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: RollbackSchemaRequest,
    ):
        workspace = await _admin_workspace(session, principal, workspace_id)
        try:
            version = await schema.rollback(
                session,
                workspace=workspace,
                target_version_id=payload.target_version_id,
                author=f"user:{principal.id}",
                change_summary=payload.change_summary,
            )
        except ValueError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc
        await session.commit()
        return _schema_version_body(version, include_content=True)

    @app.get("/workspaces/{workspace_id}/access-policy")
    async def list_access_policy(
        workspace_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        limit: int = workspaces.DEFAULT_LIST_LIMIT,
    ):
        """Deliberately capped, not cursor-paginated (09 §14, phase3-tasklist.md step 66) —
        a workspace's grant count, not an append-heavy content table."""
        await _admin_workspace(session, principal, workspace_id)
        grants = await workspaces.list_access(session, workspace_id=workspace_id, limit=limit)
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
            session,
            workspace_id=workspace_id,
            principal=payload.principal,
            role=payload.role,
            fuse_access=payload.fuse_access,
            scope=page_type_scope(payload.page_type) if payload.page_type else "",
        )
        await session.commit()
        return _access_policy_body(granted)

    @app.delete("/workspaces/{workspace_id}/access-policy/{revoked_principal}", status_code=204)
    async def revoke_access_policy(
        workspace_id: str,
        revoked_principal: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        page_type: PageType | None = None,
    ):
        # step 70 (07 §2): no `page_type` revokes the workspace-wide grant, matching this
        # endpoint's exact behavior before this step; naming one revokes only that
        # narrower scoped row, leaving the workspace-wide grant (and any other scope)
        # untouched.
        await _admin_workspace(session, principal, workspace_id)
        await workspaces.revoke(
            session,
            workspace_id=workspace_id,
            principal=revoked_principal,
            scope=page_type_scope(page_type) if page_type else "",
        )
        await session.commit()

    @app.get("/workspaces/{workspace_id}/export")
    async def export_workspace_endpoint(
        workspace_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
    ):
        """Bulk export (07 §5, phase3-tasklist.md step 74) — "export a workspace's wiki +
        sources for migration/backup," reusing step 57's real wiki markdown mirror rather
        than a second, parallel export mechanism (02 §2 already names "backup/migration/
        export" as that mirror's own first purpose). Everything under `/{workspace_id}/`
        in the object store — `wiki/`, `sources/`, `diffs/` alike — is already namespaced
        by workspace, so one prefix packages the whole thing; no filtering needed.

        Runs `wiki_export.export_workspace` first: the write-through hook (step 57) keeps
        the mirror current on every page write, so this is normally a no-op, but it's the
        same repair-pass guarantee that function already exists to give (its own docstring
        names this exact "doubles as a repair tool" use), cheap insurance that the archive
        never ships a stale mirror.

        Built fully in memory, not streamed — the simplest correct implementation for a
        reference deployment; a production-scale workspace would want a streaming tar
        instead (documented limitation, not silently missed, matching this module's own
        precedent for the `storage_utilization` content-byte approximation)."""
        await _admin_workspace(session, principal, workspace_id)
        await wiki_export.export_workspace(session, workspace_id=workspace_id)
        archive = wiki_export.build_archive(workspace_id)
        return Response(
            content=archive,
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{workspace_id}-export.tar.gz"'},
        )

    async def _admin_both_workspaces(
        session: AsyncSession, principal: Principal, source_workspace_id: str, payload: BulkMoveRequest
    ) -> None:
        """A bulk move needs admin in *both* workspaces — the source it drains and the
        target it fills — same rule as reassigning a document type's workspace (05 §7,
        `update_document_type` above) and on-behalf-of submission (09 §5)."""
        if payload.target_workspace_id == source_workspace_id:
            raise ApiError(
                400, "invalid_request", "target_workspace_id must differ from the source workspace."
            )
        if await session.get(Workspace, payload.target_workspace_id) is None:
            raise ApiError(404, "not_found", f"No workspace {payload.target_workspace_id!r}.")
        if not await has_role(
            session,
            principal=principal,
            workspace_id=payload.target_workspace_id,
            required=Role.admin,
        ):
            raise ApiError(
                403,
                "forbidden",
                "Moving into this workspace requires the admin role there.",
                {"workspace_id": payload.target_workspace_id},
            )

    @app.post("/workspaces/{workspace_id}/bulk-move/preview")
    async def bulk_move_preview(
        workspace_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: BulkMoveRequest,
    ):
        """Dry run, no writes (09 §11) — safe to call repeatedly while deciding what to
        move."""
        await _admin_workspace(session, principal, workspace_id)
        await _admin_both_workspaces(session, principal, workspace_id, payload)
        result = await bulk_move.preview(
            session,
            source_workspace_id=workspace_id,
            target_workspace_id=payload.target_workspace_id,
            page_ids=payload.page_ids,
            source_ids=payload.source_ids,
        )
        return {
            "pages": [_move_item_body(i) for i in result.pages],
            "sources": [_move_item_body(i) for i in result.sources],
            "skipped_pages": [_skipped_item_body(i) for i in result.skipped_pages],
            "skipped_sources": [_skipped_item_body(i) for i in result.skipped_sources],
        }

    @app.post("/workspaces/{workspace_id}/bulk-move")
    async def bulk_move_execute(
        workspace_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: BulkMoveRequest,
    ):
        """Batched execute (09 §11). No Idempotency-Key: a retry with the same id lists is
        already safe on its own — `execute_batch` skips anything no longer in the source
        workspace — and per-batch commits (below) don't compose with the single-commit
        idempotency-record pattern the other mutating endpoints use.

        A failed batch halts the loop without rolling back batches already committed
        (09 §11) — the `except` below is that spec-mandated halt, not incidental error
        handling.
        """
        await _admin_workspace(session, principal, workspace_id)
        await _admin_both_workspaces(session, principal, workspace_id, payload)

        items: list[tuple[str, uuid.UUID]] = [("page", i) for i in payload.page_ids] + [
            ("source", i) for i in payload.source_ids
        ]
        moved_page_ids: list[uuid.UUID] = []
        moved_source_ids: list[uuid.UUID] = []
        batch_count = 0
        error: str | None = None
        for start in range(0, len(items), bulk_move.BATCH_SIZE):
            chunk = items[start : start + bulk_move.BATCH_SIZE]
            chunk_page_ids = [item_id for kind, item_id in chunk if kind == "page"]
            chunk_source_ids = [item_id for kind, item_id in chunk if kind == "source"]
            try:
                batch = await bulk_move.execute_batch(
                    session,
                    source_workspace_id=workspace_id,
                    target_workspace_id=payload.target_workspace_id,
                    page_ids=chunk_page_ids,
                    source_ids=chunk_source_ids,
                    actor=f"user:{principal.id}",
                )
            except Exception as exc:  # noqa: BLE001 — 09 §11's halt-without-rollback
                await session.rollback()
                error = str(exc)
                break
            await session.commit()
            batch_count += 1
            moved_page_ids.extend(batch.moved_page_ids)
            moved_source_ids.extend(batch.moved_source_ids)
            # 02 §7/step 32: a page write enqueues reindex — dispatched per batch, right
            # after its own commit, so an already-committed batch's pages get reindexed
            # even if a later batch halts the loop.
            for moved_page_id in batch.moved_page_ids:
                tasks.reindex.delay(str(moved_page_id))

        # 05 §6: same as rollback, a bulk move is logged to log.md as well as
        # admin_action_log (09 §23) — for both workspaces it touched. Moved pages also
        # leave one workspace's catalog and enter the other's (phase3-tasklist.md step 60).
        await ingestion.refresh_log(session, workspace_id=workspace_id)
        await ingestion.refresh_log(session, workspace_id=payload.target_workspace_id)
        await ingestion.refresh_index(session, workspace_id=workspace_id)
        await ingestion.refresh_index(session, workspace_id=payload.target_workspace_id)
        await session.commit()

        return {
            "completed": error is None,
            "error": error,
            "batch_count": batch_count,
            "moved_page_ids": [str(i) for i in moved_page_ids],
            "moved_source_ids": [str(i) for i in moved_source_ids],
        }

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
        limit: int = DEFAULT_SEARCH_LIMIT,
    ):
        """04 §1, §4-8: single-stage federated lexical search — any authenticated caller
        (06 §1). Thin wrapper around `run_search` below, the shared Common Gateway logic
        (01 §2) the MCP `wiki_search` tool (phase2-tasklist.md step 45) also calls."""
        return await run_search(
            session,
            principal,
            q=q,
            workspace_id=workspace_id,
            page_type=page_type,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
            include_drafts=include_drafts,
            limit=limit,
        )

    @app.post("/search/{query_id}/feedback", status_code=201)
    async def submit_search_feedback(
        query_id: uuid.UUID,
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        payload: SubmitFeedbackRequest,
    ):
        """07 §4, phase3-tasklist.md step 68: thumbs-up/down on one result of one search
        call. Thin wrapper around `run_submit_search_feedback` below, the shared Common
        Gateway logic the MCP `wiki_submit_search_feedback` tool also calls."""
        return await run_submit_search_feedback(
            session, principal, query_id=query_id, page_id=payload.page_id, rating=payload.rating
        )

    async def _require_admin_scope(
        session: AsyncSession, principal: Principal, workspace_id: str | None
    ) -> None:
        """Shared by every `/metrics/*` dashboard below (05 §8, phase2-tasklist.md step
        44) — the same optional-workspace admin shape `document-types`' list endpoint
        already established: scoped+gated to one workspace when given, "admin somewhere"
        otherwise."""
        if workspace_id is not None:
            if not await has_role(
                session, principal=principal, workspace_id=workspace_id, required=Role.admin
            ):
                raise ApiError(
                    403, "forbidden", "This dashboard requires the admin role for this workspace."
                )
        elif not await any_workspace_with_role(session, principal=principal, required=Role.admin):
            raise ApiError(403, "forbidden", "This dashboard requires the admin role somewhere.")

    @app.get("/metrics/index-health")
    async def index_health_metrics(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        workspace_id: str | None = None,
    ):
        await _require_admin_scope(session, principal, workspace_id)
        return await monitoring.index_health(session, workspace_id=workspace_id)

    @app.get("/metrics/ingestion-pipeline")
    async def ingestion_pipeline_metrics(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        workspace_id: str | None = None,
    ):
        await _require_admin_scope(session, principal, workspace_id)
        result = await monitoring.ingestion_pipeline(session, workspace_id=workspace_id)
        # Global, not workspace-scoped (a Celery queue mixes every workspace's work) —
        # merged in here rather than returned by `monitoring.ingestion_pipeline` itself,
        # which stays pure-DB and needs no live Redis connection to test.
        result["queue_depths"] = await monitoring.queue_depths()
        return result

    @app.get("/metrics/search-performance")
    async def search_performance_metrics(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        workspace_id: str | None = None,
    ):
        await _require_admin_scope(session, principal, workspace_id)
        return await monitoring.search_performance(session, workspace_id=workspace_id)

    @app.get("/metrics/storage-utilization")
    async def storage_utilization_metrics(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        workspace_id: str | None = None,
    ):
        await _require_admin_scope(session, principal, workspace_id)
        return await monitoring.storage_utilization(session, workspace_id=workspace_id)

    @app.get("/metrics/review-queue-health")
    async def review_queue_health_metrics(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        workspace_id: str | None = None,
    ):
        await _require_admin_scope(session, principal, workspace_id)
        return await monitoring.review_queue_health(session, workspace_id=workspace_id)

    @app.get("/analytics/usage-trends")
    async def usage_trends_analytics(
        principal: Annotated[Principal, Depends(_principal)],
        session: Annotated[AsyncSession, Depends(_session)],
        workspace_id: str | None = None,
    ):
        await _require_admin_scope(session, principal, workspace_id)
        result = await analytics.usage_trends(session, workspace_id=workspace_id)
        # Global, not workspace-scoped (analytics.active_workspaces_trend's own docstring
        # explains why) — merged in here the same way ingestion_pipeline_metrics above
        # merges in monitoring.queue_depths().
        result["active_workspaces"] = await analytics.active_workspaces_trend(session)
        return result


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


async def run_search(
    session: AsyncSession,
    principal: Principal,
    *,
    q: str,
    workspace_id: list[str] | None = None,
    page_type: list[str] | None = None,
    tags: list[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    include_drafts: bool = False,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> dict[str, Any]:
    """04 §1, §4-8: single-stage federated lexical search — the shared Common Gateway
    logic (01 §2) both `GET /search` and the MCP `wiki_search` tool (phase2-tasklist.md
    step 45) call, rather than each re-deriving it. Workspace resolution and the taxonomy
    pre-filter (04 §4, 01 §2's Workspace Router) and the `query_log` write (04 §8) are
    gateway concerns handled here; the retrieval itself is `search.search()`. Seeing
    drafts needs `contributor`, not just `reader` — "elevated scope" per 04 §6 — so
    resolution uses a stricter role when requested rather than filtering drafts out of a
    reader-scoped set after the fact.

    Times the call for `query_log.duration_ms` (phase2-tasklist.md step 44's Search
    Performance dashboard) — wall-clock from here, not just the retrieval call, since
    that's what a caller actually experiences.

    **Partial failure** (09 §14, phase3-tasklist.md step 62): one backend being down must
    not fail the whole query — each of the two backend calls below is caught separately
    and degrades to an empty result for just that pool, with the affected workspace ids
    recorded. `partial`/`unavailable` are only present in the response when a degradation
    actually happened (09 §14: "single-workspace operations... never carry the field" —
    read here as "the non-degraded case never carries it either"). A shared-index failure
    also rolls the session back before continuing — the failed raw-SQL statement can leave
    the transaction unusable for the `query_log` write that follows (same recovery
    `bulk_move`'s own halt-without-rollback batching already uses, above); a dedicated-
    index (OpenSearch) failure never touches this session at all, so needs no rollback.
    """
    started_at = time.monotonic()
    required_role = Role.contributor if include_drafts else Role.reader
    accessible = await any_workspace_with_role(session, principal=principal, required=required_role)

    if workspace_id:
        # Intersected with what the caller can access, never expanded (04 §4).
        resolved = [w for w in workspace_id if w in accessible]
    else:
        resolved = await _taxonomy_prefilter(session, query=q, accessible=accessible)

    # Split by backend (phase2-tasklist.md step 26): a dedicated workspace's traffic
    # goes to OpenSearch, everything else to the shared Postgres index. Either list can
    # be empty — both search()/dedicated_index.search() already return [] for that.
    dedicated_ids = (
        set(
            (
                await session.execute(
                    select(Workspace.workspace_id).where(
                        Workspace.workspace_id.in_(resolved),
                        Workspace.dedicated_index.is_(True),
                    )
                )
            ).scalars()
        )
        if resolved
        else set()
    )
    shared_ids = [w for w in resolved if w not in dedicated_ids]

    unavailable: list[str] = []

    try:
        shared_hits = await search.search(
            session,
            query=q,
            workspace_ids=shared_ids,
            limit=limit,
            include_drafts=include_drafts,
            page_types=page_type,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception:
        logger.exception("shared index search failed for workspaces %s", shared_ids)
        await session.rollback()
        shared_hits = []
        unavailable.extend(shared_ids)

    try:
        dedicated_hits = await dedicated_index.search(
            query=q,
            workspace_ids=list(dedicated_ids),
            limit=limit,
            include_drafts=include_drafts,
            page_types=page_type,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception:
        logger.exception("dedicated index search failed for workspaces %s", dedicated_ids)
        dedicated_hits = []
        unavailable.extend(dedicated_ids)

    # 04 §4: normalize the dedicated backend's scores, merge, sort, truncate to `limit`
    # only after the two pools are combined — taking `limit` from each independently
    # first could drop a higher-ranked hit in favor of a lower one from the other pool.
    results = search.merge_federated(shared_hits, dedicated_hits)[:limit]

    # Step 70 (07 §2): page_type scoping applies per result's own workspace — computed
    # once per distinct workspace_id present, not once per result. A filtered-out result
    # is omitted outright (same "never confirm an inaccessible target's existence"
    # reasoning `_resolve_page_links` already uses), so it never reaches `query_log`
    # either — this can return fewer than `limit` items even when more exist beyond what
    # was fetched, the same accepted approximation every other omission-based filter here
    # already carries (no re-query/re-paginate to backfill the difference).
    visible_by_workspace: dict[str, set[PageType]] = {}
    filtered_results = []
    for r in results:
        if r.workspace_id not in visible_by_workspace:
            visible_by_workspace[r.workspace_id] = await visible_page_types(
                session, principal=principal, workspace_id=r.workspace_id, required=required_role
            )
        if PageType(r.page_type) in visible_by_workspace[r.workspace_id]:
            filtered_results.append(r)
    results = filtered_results

    logged = await query_log.record(
        session,
        principal=principal.id,
        query_text=q,
        resolved_workspaces=resolved,
        results=[{"page_id": str(r.page_id), "score": r.score} for r in results],
        duration_ms=round((time.monotonic() - started_at) * 1000),
    )
    await session.commit()

    # Step 68 (07 §4): the search result feedback loop needs a caller-visible handle on
    # this specific call to rate its results against (`POST /search/{query_id}/feedback`)
    # — nothing before this step ever needed the caller to see `query_log`'s own id.
    body: dict[str, Any] = {
        "query_id": str(logged.query_id),
        "items": [_search_result_body(r) for r in results],
    }
    if unavailable:
        body["partial"] = True
        body["unavailable"] = sorted(unavailable)  # deterministic — dedicated_ids is a set
    return body


def _feedback_body(entry: QueryFeedback) -> dict[str, Any]:
    return {
        "feedback_id": str(entry.feedback_id),
        "query_id": str(entry.query_id),
        "page_id": str(entry.page_id),
        "rating": entry.rating.value,
        "created_at": entry.created_at.isoformat(),
    }


async def run_submit_search_feedback(
    session: AsyncSession,
    principal: Principal,
    *,
    query_id: uuid.UUID,
    page_id: uuid.UUID,
    rating: FeedbackRating,
) -> dict[str, Any]:
    """07 §4, phase3-tasklist.md step 68 — the shared Common Gateway logic both `POST
    /search/{query_id}/feedback` and the MCP `wiki_submit_search_feedback` tool call.

    Only the principal who ran the search may rate its own results — the same "you can
    only act on what's yours" reasoning step 46's on-behalf-of scoping already
    established; a stranger rating results they never saw would corrupt the very signal
    this loop exists to produce."""
    query = await session.get(QueryLog, query_id)
    if query is None:
        raise ApiError(404, "not_found", f"No search call {query_id}.")
    if query.principal != principal.id:
        raise ApiError(
            403, "forbidden", "Only the principal who ran this search may rate its results."
        )
    try:
        entry = await query_log.submit_feedback(
            session, query_id=query_id, page_id=page_id, principal=principal.id, rating=rating
        )
    except query_log.InvalidFeedbackError as exc:
        raise ApiError(400, "invalid_request", str(exc)) from exc
    await session.commit()
    return _feedback_body(entry)


async def run_resolve_review_item(
    session: AsyncSession,
    principal: Principal,
    *,
    review_id: uuid.UUID,
    action: str,
    note: str | None = None,
    notification_sink: notifications.NotificationSink | None = None,
) -> dict[str, Any]:
    """Execute a resolution (06 §1, 05 §1) — the shared Common Gateway logic (01 §2) both
    `POST /review-items/{id}/resolve` and the MCP `wiki_resolve_review_item` tool
    (phase2-tasklist.md step 45) call. No idempotency support here (that's REST-specific,
    06 §2 names no MCP equivalent) — the REST endpoint wraps this with its own replay
    check/record around it, in a separate commit; a crash between the two just means a
    retried request sees a 409 (`AlreadyResolvedError`) instead of a replayed 200, never a
    duplicate side effect, so splitting the commit this way is safe.

    Admin-gated against the item's own workspace when it has one; against any workspace
    the caller administers when it doesn't yet (09 §22). A `classification` resolution
    additionally needs admin in the workspace the chosen `document_type` routes to (09
    §27) — that workspace isn't known until `action` is read, so it's checked as a
    second, more specific gate below."""
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
        doc_type = await session.get(DocumentType, action)
        if doc_type is None:
            raise ApiError(400, "invalid_request", f"{action!r} is not a registered document type.")
        if not await has_role(
            session, principal=principal, workspace_id=doc_type.workspace_id, required=Role.admin
        ):
            raise ApiError(
                403,
                "forbidden",
                "Resolving into this workspace requires the admin role there.",
                {"workspace_id": doc_type.workspace_id},
            )

    try:
        state = await ingestion.resolve_review_item(
            session, item=item, action=action, actor=f"user:{principal.id}", note=note
        )
    except review.AlreadyResolvedError as exc:
        raise ApiError(409, "conflict", str(exc)) from exc
    except ingestion.InvalidResolutionError as exc:
        raise ApiError(400, "invalid_request", str(exc)) from exc

    # phase2-tasklist.md step 32's "acceptance enqueues dedup then curate": a
    # `classification` resolution always lands at `classified` (fresh dedup still to
    # run); `duplicate`'s `keep_both`/`supersede` land at `ingesting` (dedup already
    # resolved by this admin action — `tasks._curate` skips re-running it, 09 §35).
    # `merge` writes its target page directly and reaches `ingested` here, so it needs
    # a reindex dispatch instead — the page it touched isn't returned by
    # `resolve_review_item`, so it's read back off `ingestion_log` the same way
    # `ingestion._duplicate_evidence` reads other resolution detail.
    merge_page_id: uuid.UUID | None = None
    if item.kind is ReviewKind.duplicate and action == "merge" and state is PipelineState.ingested:
        for entry in reversed(await pipeline.history(session, uuid.UUID(item.subject_ref))):
            if entry.detail.get("resolution") == "merge" and "target_page_id" in entry.detail:
                merge_page_id = uuid.UUID(entry.detail["target_page_id"])
                break

    # Step 67 (07 §3): notify the submitter on the two single-source `duplicate` outcomes
    # this handler completes directly — `reject` and `merge` (a fresh-submission/keep_both/
    # supersede's own eventual `ingested` notification fires from `tasks._curate` instead,
    # once that deferred task actually runs). Read here, before commit, so the source (and
    # merge target) rows are still cheap to fetch off this same session.
    notify_rejected_source: RawSource | None = None
    notify_merged_source: RawSource | None = None
    merge_target_path: str | None = None
    if item.kind is ReviewKind.duplicate and action == "reject" and state is PipelineState.rejected:
        notify_rejected_source = await session.get(RawSource, uuid.UUID(item.subject_ref))
    elif merge_page_id is not None:
        notify_merged_source = await session.get(RawSource, uuid.UUID(item.subject_ref))
        target_page = await session.get(WikiPage, merge_page_id)
        merge_target_path = target_page.path if target_page is not None else str(merge_page_id)

    # phase2-tasklist.md step 36: approving a Staleness Detector `reindex` item
    # dispatches reindex for exactly the pages it found (05 §3) — read from the item's
    # own `detail` (advisor.py), the same evidence the admin console would have shown.
    reindex_page_ids: list[str] = []
    if item.kind is ReviewKind.reindex and action == "reindex now":
        reindex_page_ids = [p["page_id"] for p in (item.detail or {}).get("pages", [])]
    # Step 38: an advisor-raised duplicate's `merge` writes a new version on the
    # primary page directly (advisor.resolve_existing_duplicate) — the page id is
    # already in `detail`, no ingestion_log archaeology needed for this one.
    elif (
        item.kind is ReviewKind.duplicate
        and (item.detail or {}).get("raised_by") == "advisor"
        and action == "merge"
    ):
        reindex_page_ids = [item.detail["primary_page_id"]]

    # Step 64: approving a Stuck-Pipeline Sweep item's `retry` re-dispatches exactly the
    # sources it found, from each one's own recorded state (advisor.py) — `classify_source`
    # for a `submitted` source (safe to re-run: that's the state a lost first dispatch
    # would still be sitting in, no transition needed) and `curate_source` for
    # `classified`/`ingesting` (already re-entrant by design, `tasks._curate`'s own
    # if/elif branching on `pipeline_state`). `abort`/`dismiss` need no dispatch here —
    # `abort`'s pipeline-side work already happened inside `resolve_review_item` above.
    retry_sources: list[dict] = []
    if item.kind is ReviewKind.stuck and action == "retry":
        retry_sources = (item.detail or {}).get("sources", [])

    body = {
        "review_id": str(item.review_id),
        "status": item.status.value,
        "resolved_action": item.resolved_action,
        "pipeline_state": state.value if state is not None else None,
    }
    await session.commit()
    if state in (PipelineState.classified, PipelineState.ingesting):
        tasks.curate_source.delay(item.subject_ref)
    elif item.kind is ReviewKind.pii_review and action == "acknowledge":
        # Step 71 (07 §2): the source stays at `pending_review` (`resolve_pii_review`
        # makes no transition of its own — see its own docstring for why) — re-dispatching
        # here is what actually moves it, via `classify_source`'s own `pending_review ->
        # classifying` first line, the same "admin retries a failed classification" shape
        # 03 §1's own state diagram already names. Checked by kind/action, not `state`
        # (which is still `pending_review` at this point, correctly).
        tasks.classify_source.delay(item.subject_ref)
    elif merge_page_id is not None:
        tasks.reindex.delay(str(merge_page_id))
    for page_id in reindex_page_ids:
        tasks.reindex.delay(page_id)
    for source_entry in retry_sources:
        if source_entry["pipeline_state"] == PipelineState.submitted.value:
            tasks.classify_source.delay(source_entry["source_id"])
        else:
            tasks.curate_source.delay(source_entry["source_id"])
    if notify_rejected_source is not None:
        sink = notification_sink or notifications.default_notification_sink()
        await sink.notify_source_rejected(
            submitted_by=notify_rejected_source.submitted_by,
            filename=notify_rejected_source.filename,
            source_id=str(notify_rejected_source.source_id),
            reason=note or "duplicate",
        )
    elif notify_merged_source is not None:
        sink = notification_sink or notifications.default_notification_sink()
        await sink.notify_source_merged(
            submitted_by=notify_merged_source.submitted_by,
            filename=notify_merged_source.filename,
            source_id=str(notify_merged_source.source_id),
            target_page_path=merge_target_path,
        )
    return body


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


async def find_by_hash(session: AsyncSession, content_hash: str) -> list[RawSource]:
    """Exact-duplicate lookup (03 §4). Here because submission is what populates it."""
    result = await session.execute(
        select(RawSource).where(RawSource.content_hash == content_hash)
    )
    return list(result.scalars())


app = create_app()
