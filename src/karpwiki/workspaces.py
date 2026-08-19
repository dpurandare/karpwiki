"""Workspace CRUD and access-policy management (01 §3, 05 §7, 06 §1, §3) —
phase2-tasklist.md step 23.

`06` §1 names exactly three workspace mutations — create, update, archive — no delete (01
§3: deletion is rare, explicit, and requires an export prerequisite, 05 §7; out of scope
here) and no unarchive (archived is described as read-only and excluded from default
routing, never as reversible in the operation table).

`schema_ref` is carried as a plain pointer field here, matching 01 §3's own framing
("pointer to this workspace's SCHEMA.md") — actually storing and versioning SCHEMA.md
content, and wiring workspaces' thresholds/model overrides to something real instead of
`09` §6's hardcoded Python defaults, is a separate, currently-undesigned piece of work
carried forward rather than built as a side effect of workspace CRUD.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import wiki_export
from .models import AccessPolicy, Role, Workspace, WorkspaceStatus


class DuplicateWorkspaceError(ValueError):
    """`workspace_id` is already taken."""


async def create(
    session: AsyncSession,
    *,
    workspace_id: str,
    name: str,
    description: str | None = None,
    schema_ref: str | None = None,
    storage_bindings: dict | None = None,
) -> Workspace:
    if await session.get(Workspace, workspace_id) is not None:
        raise DuplicateWorkspaceError(f"workspace {workspace_id!r} already exists")
    workspace = Workspace(
        workspace_id=workspace_id,
        name=name,
        description=description,
        schema_ref=schema_ref,
        status=WorkspaceStatus.active,
        storage_bindings=storage_bindings or {},
    )
    session.add(workspace)
    await session.flush()
    wiki_export.write_schema_placeholder(workspace_id=workspace_id, schema_ref=schema_ref)
    return workspace


async def update(
    session: AsyncSession,
    *,
    workspace: Workspace,
    name: str | None = None,
    description: str | None = None,
    schema_ref: str | None = None,
    storage_bindings: dict | None = None,
    dedicated_index: bool | None = None,
) -> Workspace:
    if name is not None:
        workspace.name = name
    if description is not None:
        workspace.description = description
    if schema_ref is not None:
        workspace.schema_ref = schema_ref
        wiki_export.write_schema_placeholder(workspace_id=workspace.workspace_id, schema_ref=schema_ref)
    if storage_bindings is not None:
        workspace.storage_bindings = storage_bindings
    if dedicated_index is not None:
        # 02 §4 / 06 §6: an operational decision made once a workspace approaches scale,
        # not a create-time choice — so it's update-only, not in `create()` above.
        workspace.dedicated_index = dedicated_index
    await session.flush()
    return workspace


async def archive(session: AsyncSession, *, workspace: Workspace) -> Workspace:
    """01 §3: read-only, excluded from default search/ingestion routing, still queryable."""
    workspace.status = WorkspaceStatus.archived
    await session.flush()
    return workspace


async def grant(
    session: AsyncSession,
    *,
    workspace_id: str,
    principal: str,
    role: Role,
    fuse_access: bool | None = None,
) -> AccessPolicy:
    """Assign a principal a role in a workspace (05 §7). Upserts — granting a role to a
    principal that already has one changes it, rather than requiring a separate revoke.

    `fuse_access` (09 §12, phase3-tasklist.md step 58) is read-only FUSE-mount access to
    the wiki export, orthogonal to `role` — omitted (`None`) leaves an existing grant's
    value unchanged, matching `update()`'s own "only supplied fields change" convention;
    a brand-new grant defaults to `False` when omitted, same as the column's own default.
    """
    existing = await session.get(AccessPolicy, (workspace_id, principal))
    if existing is not None:
        existing.role = role
        if fuse_access is not None:
            existing.fuse_access = fuse_access
        await session.flush()
        return existing
    policy = AccessPolicy(
        workspace_id=workspace_id, principal=principal, role=role, fuse_access=fuse_access or False
    )
    session.add(policy)
    await session.flush()
    return policy


async def revoke(session: AsyncSession, *, workspace_id: str, principal: str) -> None:
    policy = await session.get(AccessPolicy, (workspace_id, principal))
    if policy is not None:
        await session.delete(policy)
        await session.flush()


async def list_access(session: AsyncSession, *, workspace_id: str) -> list[AccessPolicy]:
    result = await session.execute(
        select(AccessPolicy)
        .where(AccessPolicy.workspace_id == workspace_id)
        .order_by(AccessPolicy.principal)
    )
    return list(result.scalars())


async def list_for_principal(session: AsyncSession, *, principal_keys: tuple[str, ...]) -> list[Workspace]:
    """06 §1: `workspaces` list/get returns only workspaces the caller can access — any role,
    not admin-only, unlike `document-types` (09 §25)."""
    result = await session.execute(
        select(Workspace)
        .join(AccessPolicy, AccessPolicy.workspace_id == Workspace.workspace_id)
        .where(AccessPolicy.principal.in_(principal_keys))
        .order_by(Workspace.workspace_id)
        .distinct()
    )
    return list(result.scalars())
