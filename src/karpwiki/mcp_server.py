"""MCP server (06 §2, phase2-tasklist.md step 45) — a thin protocol adapter over the
same Common Gateway logic `api.py`'s REST endpoints call. `01` §2 frames AuthN/AuthZ,
workspace resolution, and dispatch as ONE shared Common Gateway layer with REST/MCP as
two protocol adapters on top of it, not two independent copies of that logic — so the two
genuinely complex operations here (`wiki_search`, `wiki_resolve_review_item`) call the
exact same `api.run_search`/`api.run_resolve_review_item` the REST endpoints do. The other
eight tools are thin enough (one role check plus one existing service-layer call) that
writing the equivalent code directly here matches how `api.py`'s own many endpoints
already look — extracting a shared helper for each would be pure ceremony.

**Identity per transport** (06 §2 wants both `stdio` and streamable HTTP):

- **Streamable HTTP**: a real per-request `Principal`, resolved from `ctx.headers`
  through the exact same `Authenticator` interface every REST endpoint already uses
  (`api._principal`'s equivalent, `_resolve_principal` below).
- **`stdio`**: no per-call headers exist at all — `ctx.headers` is `None` on this
  transport by the SDK's own design (a stdio server is one local process for one caller,
  e.g. a local agent/IDE integration). The principal is resolved ONCE, lazily, from
  `KARPWIKI_MCP_USER`/`KARPWIKI_MCP_GROUPS` env vars — synthesized into the same
  header-shaped dict any `Authenticator` (including `OidcAuthenticator`, step 47) expects,
  since `stdio` has no real headers to carry a bearer token in the first place. This
  remains a `TrustedHeaderAuthenticator`-shaped stand-in regardless of which
  `Authenticator` streamable HTTP resolves to — a real IdP has no way to authenticate a
  bare local subprocess with no browser or token of its own.

**On-behalf-of delegation** (`wiki_submit`'s `acting_as` argument, 09 §5, phase2-tasklist.md
step 46): the only delegated operation 06 §2 names. Every other tool authenticates as the
calling agent's own credential only. `wiki_get_source_status`'s submitter-only check is
NOT extended for this — it still matches the literal `submitted_by` (the represented
user, for a delegated submission), so the calling agent itself can't poll status on a
submission it made on someone else's behalf. Not named anywhere in `09` §5, and a real,
known gap rather than a silent omission — the represented user (who does have a direct
credential, by the AuthZ check's own requirement) can always check it themselves.

`wiki_submit` accepts pasted text only, not a file/URL upload — `POST /sources`'s other
two input modes don't map cleanly onto MCP's JSON-shaped tool arguments, and every
existing test in this codebase already exercises the text path as primary; file/URL
support isn't named anywhere in 06 §2's tool table as its own requirement.
"""

import os
import uuid
from datetime import date

from mcp.server.mcpserver import Context, MCPServer

from . import api, ingestion, review, versioning, workspaces
from .auth import Authenticator, Principal, any_workspace_with_role, default_authenticator, has_role
from .db import session_scope
from .models import RawSource, ReviewKind, ReviewStatus, Role
from .search_result import DEFAULT_SEARCH_LIMIT


class McpAuthError(Exception):
    """No authenticated principal for this call. MCP has no HTTP status codes, so tools
    raise a plain exception and let the SDK surface it as a tool-call error."""


async def _resolve_http_principal(authenticator: Authenticator, headers) -> Principal:
    """The streamable-HTTP identity path — a standalone, directly testable function
    (unlike the stdio path's caching, this needs no per-server-instance state)."""
    principal = await authenticator.authenticate(dict(headers))
    if principal is None:
        raise McpAuthError("No authenticated principal on this request.")
    return principal


async def _resolve_stdio_principal(authenticator: Authenticator) -> Principal:
    """stdio has no real headers, so this synthesizes whichever shape the active
    `Authenticator` actually expects, from env vars — the two aren't interchangeable:
    `TrustedHeaderAuthenticator` reads `x-karpwiki-user`/`_groups`; `OidcAuthenticator`
    (step 47) reads `authorization: Bearer <token>`, and a bare local stdio process can't
    run an interactive OIDC login to obtain one itself, so it has to already be handed
    one. `KARPWIKI_MCP_TOKEN` is tried first (the only shape that can possibly satisfy
    `OidcAuthenticator`); falling back to `KARPWIKI_MCP_USER`/`_GROUPS` preserves the
    unconfigured-OIDC (`TrustedHeaderAuthenticator`) default from steps 45/46 unchanged."""
    token = os.environ.get("KARPWIKI_MCP_TOKEN", "").strip()
    if token:
        headers = {"authorization": f"Bearer {token}"}
    else:
        headers = {}
        user = os.environ.get("KARPWIKI_MCP_USER", "").strip()
        if user:
            headers["x-karpwiki-user"] = user
        groups = os.environ.get("KARPWIKI_MCP_GROUPS", "")
        if groups:
            headers["x-karpwiki-groups"] = groups
    principal = await authenticator.authenticate(headers)
    if principal is None:
        raise McpAuthError(
            "No stdio identity configured — set KARPWIKI_MCP_TOKEN (a bearer token, for "
            "real OIDC) or KARPWIKI_MCP_USER (and optionally KARPWIKI_MCP_GROUPS, for the "
            "trusted-header default) in the environment this MCP server process runs in."
        )
    return principal


def create_mcp_server(authenticator: Authenticator | None = None) -> MCPServer:
    """Mirrors `api.create_app`'s own factory shape (an injectable `Authenticator` for
    tests, `auth.default_authenticator()` otherwise) — a separate process/entry point from
    the REST gateway, so it can't just reuse `create_app`'s own instance."""
    resolved_authenticator = authenticator or default_authenticator()
    mcp = MCPServer("karpwiki")
    # Resolved lazily on first stdio call, then cached — a stdio server is one process
    # for one caller, so this never needs to change within a run.
    stdio_principal: Principal | None = None

    async def _resolve_principal(ctx: Context) -> Principal:
        nonlocal stdio_principal
        if ctx.headers is not None:
            return await _resolve_http_principal(resolved_authenticator, ctx.headers)
        if stdio_principal is None:
            stdio_principal = await _resolve_stdio_principal(resolved_authenticator)
        return stdio_principal

    @mcp.tool()
    async def wiki_search(
        ctx: Context,
        q: str,
        workspace_id: list[str] | None = None,
        page_type: list[str] | None = None,
        tags: list[str] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        include_drafts: bool = False,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> dict:
        """Single-stage lexical/catalog search (04 §1) — ranked, cited page snippets, no
        synthesis. Maps to `GET /search`."""
        principal = await _resolve_principal(ctx)
        async with session_scope() as session:
            return await api.run_search(
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

    @mcp.tool()
    async def wiki_get_page(ctx: Context, page_id: str) -> dict:
        """Fetch a specific page by id, e.g. for an agent following a citation. Maps to
        `GET /pages/{id}`."""
        principal = await _resolve_principal(ctx)
        async with session_scope() as session:
            page = await api._reader_page(session, principal, uuid.UUID(page_id))
            version = (
                await session.get(api.PageVersion, page.current_version_id)
                if page.current_version_id
                else None
            )
            return api._page_body(page, version)

    @mcp.tool()
    async def wiki_list_pages(
        ctx: Context,
        workspace_id: str,
        page_type: list[str] | None = None,
        tags: list[str] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        status: str | None = None,
        limit: int = versioning.DEFAULT_LIST_LIMIT,
        cursor: str | None = None,
    ) -> dict:
        """Browse/filter a workspace's pages — e.g. to walk its `index.md` catalog
        programmatically. Maps to `GET /pages`. `status="draft"` needs `contributor`
        (elevated scope, 04 §6's reasoning applied the same way `api.py`'s own
        `list_pages_endpoint` does)."""
        principal = await _resolve_principal(ctx)
        async with session_scope() as session:
            required = Role.contributor if status == "draft" else Role.reader
            if not await has_role(
                session, principal=principal, workspace_id=workspace_id, required=required
            ):
                raise McpAuthError(f"Listing pages here requires the {required.value} role.")
            statuses = [status] if status else ["published"]
            pages, next_cursor = await versioning.list_pages(
                session,
                workspace_id=workspace_id,
                page_types=page_type,
                tags=tags,
                date_from=date_from,
                date_to=date_to,
                statuses=statuses,
                limit=limit,
                cursor=cursor,
            )
            return {
                "items": [api._page_summary_body(p) for p in pages],
                "next_cursor": next_cursor,
            }

    @mcp.tool()
    async def wiki_list_workspaces(ctx: Context) -> dict:
        """Discover which workspaces the caller can search or submit to. Maps to
        `GET /workspaces`."""
        principal = await _resolve_principal(ctx)
        async with session_scope() as session:
            found = await workspaces.list_for_principal(session, principal_keys=principal.policy_keys)
            return {"items": [api._workspace_body(w) for w in found]}

    @mcp.tool()
    async def wiki_submit(ctx: Context, text: str, acting_as: str | None = None) -> dict:
        """Submit a pasted-text document — still goes through the full pipeline,
        including the `submission` review item. Maps to `POST /sources` (text mode
        only — see module docstring).

        `acting_as` (format `"user:<id>"`, phase2-tasklist.md step 46, 09 §5) lets the
        calling agent submit on a represented end user's behalf: AuthZ requires BOTH the
        agent's own credential and the represented user to independently hold
        `contributor` access somewhere in common — whichever is more restrictive
        applies, so an agent can't use its own broader access to submit "as" a user who
        couldn't have submitted there themselves. On success, `submitted_by` (and any
        resulting page's `author`) record the represented user, not the agent; the
        agent's own identity is recorded in the `ingestion_log` entry's `detail` for
        audit, no new core field. Omit `acting_as` for an ordinary, non-delegated
        submission as the calling identity itself."""
        principal = await _resolve_principal(ctx)
        async with session_scope() as session:
            agent_workspaces = set(
                await any_workspace_with_role(session, principal=principal, required=Role.contributor)
            )
            if acting_as is None:
                if not agent_workspaces:
                    raise McpAuthError(
                        "Submitting requires the contributor role in at least one workspace."
                    )
                submitted_by = f"user:{principal.id}"
                extra_detail = None
            else:
                if not acting_as.startswith("user:"):
                    raise McpAuthError('acting_as must be in the form "user:<id>" (09 §5).')
                acted_id = acting_as.removeprefix("user:")
                acted_workspaces = set(
                    await any_workspace_with_role(
                        session, principal=Principal(id=acted_id), required=Role.contributor
                    )
                )
                if not (agent_workspaces & acted_workspaces):
                    raise McpAuthError(
                        "On-behalf-of submission requires both the calling agent and the "
                        "represented user to independently hold the contributor role "
                        "somewhere in common (09 §5) — whichever is more restrictive applies."
                    )
                submitted_by = acting_as
                extra_detail = {"acting_agent": f"user:{principal.id}"}

            source = await ingestion.store(
                session,
                text.encode(),
                "pasted.txt",
                submitted_by=submitted_by,
                extra_detail=extra_detail,
            )
            body = {
                "source_id": str(source.source_id),
                "pipeline_state": source.pipeline_state.value,
                "filename": source.filename,
            }
            await session.commit()
            api.tasks.classify_source.delay(str(source.source_id))
            return body

    @mcp.tool()
    async def wiki_get_source_status(ctx: Context, source_id: str) -> dict:
        """Check a submission's pipeline state — typically polled after `wiki_submit`.
        Maps to `GET /sources/{id}` (submitter-only, no admin override, matching that
        endpoint exactly: "whether a source exists is not public")."""
        principal = await _resolve_principal(ctx)
        async with session_scope() as session:
            source = await session.get(RawSource, uuid.UUID(source_id))
            if source is None or source.submitted_by != f"user:{principal.id}":
                raise McpAuthError(f"No source {source_id}.")
            return {
                "source_id": str(source.source_id),
                "pipeline_state": source.pipeline_state.value,
                "status": source.status.value,
                "workspace_id": source.workspace_id,
                "filename": source.filename,
            }

    @mcp.tool()
    async def wiki_list_review_items(
        ctx: Context,
        workspace_id: str | None = None,
        kind: str | None = None,
        status: str = "open",
        severity: str | None = None,
        limit: int = review.DEFAULT_LIST_LIMIT,
        cursor: str | None = None,
    ) -> dict:
        """List/filter the admin review queue (05 §1). Maps to `GET /review-items`."""
        principal = await _resolve_principal(ctx)
        async with session_scope() as session:
            admin_workspaces = await any_workspace_with_role(
                session, principal=principal, required=Role.admin
            )
            if not admin_workspaces:
                raise McpAuthError("Listing review items requires the admin role somewhere.")
            items, next_cursor = await review.list_items(
                session,
                admin_workspaces=admin_workspaces,
                workspace_id=workspace_id,
                kind=ReviewKind(kind) if kind else None,
                status=ReviewStatus(status) if status else None,
                severity=severity,
                limit=limit,
                cursor=cursor,
            )
            return {
                "items": [api._review_item_body(i) for i in items],
                "next_cursor": next_cursor,
            }

    @mcp.tool()
    async def wiki_resolve_review_item(
        ctx: Context, review_id: str, action: str, note: str | None = None
    ) -> dict:
        """Execute a resolution — action set depends on `kind` (03 §3-5, 05 §3-5). Maps to
        `POST /review-items/{id}/resolve`."""
        principal = await _resolve_principal(ctx)
        async with session_scope() as session:
            return await api.run_resolve_review_item(
                session, principal, review_id=uuid.UUID(review_id), action=action, note=note
            )

    @mcp.tool()
    async def wiki_get_page_versions(
        ctx: Context,
        page_id: str,
        limit: int = versioning.DEFAULT_LIST_LIMIT,
        cursor: str | None = None,
    ) -> dict:
        """List version history (05 §6). Maps to `GET /pages/{id}/versions`."""
        principal = await _resolve_principal(ctx)
        async with session_scope() as session:
            await api._admin_page(session, principal, uuid.UUID(page_id))
            versions, next_cursor = await versioning.list_versions(
                session, page_id=uuid.UUID(page_id), limit=limit, cursor=cursor
            )
            return {
                "items": [api._page_version_body(v) for v in versions],
                "next_cursor": next_cursor,
            }

    @mcp.tool()
    async def wiki_rollback_page(
        ctx: Context, page_id: str, target_version_id: str, change_summary: str | None = None
    ) -> dict:
        """Roll back a page to a prior version (01 §5) — creates a new version, never
        deletes history. Maps to `POST /pages/{id}/rollback` (no idempotency support here,
        same reasoning as `wiki_resolve_review_item` — that's a REST-specific header
        convention)."""
        principal = await _resolve_principal(ctx)
        async with session_scope() as session:
            page = await api._admin_page(session, principal, uuid.UUID(page_id))
            try:
                version = await versioning.rollback(
                    session,
                    page=page,
                    target_version_id=uuid.UUID(target_version_id),
                    author=f"user:{principal.id}",
                    change_summary=change_summary,
                )
            except ValueError as exc:
                raise api.ApiError(400, "invalid_request", str(exc)) from exc
            await api.ingestion.refresh_log(session, workspace_id=page.workspace_id)
            body = api._page_version_body(version)
            await session.commit()
            api.tasks.reindex.delay(str(page.page_id))
            return body

    return mcp


if __name__ == "__main__":
    create_mcp_server().run()
