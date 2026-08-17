"""Document-type taxonomy CRUD (02 §3, 05 §7) — phase2-tasklist.md step 22.

Promotes Phase 1's `Workspace.document_types` array column into the real `document_type`
table 02 §3 always described. Each row is one taxonomy label the Classifier can route a
submission to (03 §3); `type_code` is the primary key, not `(workspace_id, type_code)` —
see `models.DocumentType`'s docstring for why.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DocumentType


class DuplicateTypeCodeError(ValueError):
    """`type_code` already belongs to some workspace — codes are unique platform-wide."""


async def create(
    session: AsyncSession, *, type_code: str, workspace_id: str, description: str | None = None
) -> DocumentType:
    if await session.get(DocumentType, type_code) is not None:
        raise DuplicateTypeCodeError(f"document type {type_code!r} already exists")
    doc_type = DocumentType(type_code=type_code, workspace_id=workspace_id, description=description)
    session.add(doc_type)
    await session.flush()
    return doc_type


async def list_for_workspace(session: AsyncSession, *, workspace_id: str) -> list[DocumentType]:
    result = await session.execute(
        select(DocumentType)
        .where(DocumentType.workspace_id == workspace_id)
        .order_by(DocumentType.type_code)
    )
    return list(result.scalars())


async def list_for_workspaces(
    session: AsyncSession, *, workspace_ids: list[str]
) -> list[DocumentType]:
    """Every type across a set of workspaces — the admin listing endpoint's shape, since an
    admin's queue/version-browser scope is already always a workspace *set* (09 §22)."""
    if not workspace_ids:
        return []
    result = await session.execute(
        select(DocumentType)
        .where(DocumentType.workspace_id.in_(workspace_ids))
        .order_by(DocumentType.workspace_id, DocumentType.type_code)
    )
    return list(result.scalars())


async def type_codes_for_workspace(session: AsyncSession, *, workspace_id: str) -> list[str]:
    """The bare `list[str]` shape `classify.py`'s pure functions expect — what
    `workspace.document_types` used to be, now read from the real table."""
    return [dt.type_code for dt in await list_for_workspace(session, workspace_id=workspace_id)]


async def update(
    session: AsyncSession,
    *,
    doc_type: DocumentType,
    new_type_code: str | None = None,
    workspace_id: str | None = None,
    description: str | None = None,
) -> DocumentType:
    """Rename, reassign to a different workspace, and/or redescribe (05 §7: "add/remove/rename
    document types; reassign a type's target workspace"). Reassignment affects future routing
    only — it does not move any already-ingested content (05 §7); that's the separate bulk-move
    admin action (09 §11, phase2-tasklist.md step 27).
    """
    if new_type_code is not None and new_type_code != doc_type.type_code:
        if await session.get(DocumentType, new_type_code) is not None:
            raise DuplicateTypeCodeError(f"document type {new_type_code!r} already exists")
        doc_type.type_code = new_type_code
    if workspace_id is not None:
        doc_type.workspace_id = workspace_id
    if description is not None:
        doc_type.description = description
    await session.flush()
    return doc_type


async def delete(session: AsyncSession, *, doc_type: DocumentType) -> None:
    await session.delete(doc_type)
    await session.flush()
