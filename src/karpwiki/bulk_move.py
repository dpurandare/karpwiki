"""Taxonomy bulk-move admin action (05 §7, 09 §11) — phase2-tasklist.md step 27.

Dry-run `preview`, then batched `execute_batch`. Each page re-home is a `workspace_id`
update plus a `page_version` with `trigger=manual_edit`; each source re-home also relocates
its object under the new workspace's prefix (`ingestion.relocate`). Batching, per-batch
commit, and halt-without-rollback-on-failure are the API layer's job (`api.py`), not this
module's — this module never commits, matching every other domain module in this codebase.
Resumability (09 §11: "the admin resumes or retries the remaining batches") falls out of
`execute_batch` silently skipping anything no longer in `source_workspace_id` — calling it
again with the same full id list is safe.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from . import config, versioning
from .dedicated_index import delete_page as delete_dedicated_page
from .frontmatter import split_frontmatter
from .ingestion import relocate
from .models import AdminActionLog, PageVersion, RawSource, VersionTrigger, WikiPage, Workspace

BATCH_SIZE = config.BULK_MOVE_BATCH_SIZE


@dataclass
class MoveItem:
    id: uuid.UUID
    label: str


@dataclass
class SkippedItem:
    id: uuid.UUID
    reason: str  # "not_found" | "wrong_workspace" | "already_at_target"


@dataclass
class BulkMovePreview:
    pages: list[MoveItem] = field(default_factory=list)
    sources: list[MoveItem] = field(default_factory=list)
    skipped_pages: list[SkippedItem] = field(default_factory=list)
    skipped_sources: list[SkippedItem] = field(default_factory=list)


@dataclass
class BatchResult:
    moved_page_ids: list[uuid.UUID] = field(default_factory=list)
    moved_source_ids: list[uuid.UUID] = field(default_factory=list)


async def _classify(
    session: AsyncSession,
    *,
    ids: list[uuid.UUID],
    model: type[WikiPage] | type[RawSource],
    label_attr: str,
    source_workspace_id: str,
    target_workspace_id: str,
) -> tuple[list[MoveItem], list[SkippedItem]]:
    move, skip = [], []
    for item_id in ids:
        row = await session.get(model, item_id)
        if row is None:
            skip.append(SkippedItem(item_id, "not_found"))
        elif row.workspace_id == target_workspace_id:
            skip.append(SkippedItem(item_id, "already_at_target"))
        elif row.workspace_id != source_workspace_id:
            skip.append(SkippedItem(item_id, "wrong_workspace"))
        else:
            move.append(MoveItem(item_id, getattr(row, label_attr)))
    return move, skip


async def preview(
    session: AsyncSession,
    *,
    source_workspace_id: str,
    target_workspace_id: str,
    page_ids: list[uuid.UUID],
    source_ids: list[uuid.UUID],
) -> BulkMovePreview:
    """No writes — 09 §11's "the admin first previews the affected page/source count and
    list"."""
    pages, skipped_pages = await _classify(
        session,
        ids=page_ids,
        model=WikiPage,
        label_attr="path",
        source_workspace_id=source_workspace_id,
        target_workspace_id=target_workspace_id,
    )
    sources, skipped_sources = await _classify(
        session,
        ids=source_ids,
        model=RawSource,
        label_attr="filename",
        source_workspace_id=source_workspace_id,
        target_workspace_id=target_workspace_id,
    )
    return BulkMovePreview(
        pages=pages, sources=sources, skipped_pages=skipped_pages, skipped_sources=skipped_sources
    )


async def execute_batch(
    session: AsyncSession,
    *,
    source_workspace_id: str,
    target_workspace_id: str,
    page_ids: list[uuid.UUID],
    source_ids: list[uuid.UUID],
    actor: str,
) -> BatchResult:
    """Moves exactly the given ids — the caller (`api.py`) slices the full request into
    `BATCH_SIZE` chunks and commits after each call. Anything not currently in
    `source_workspace_id` (already moved, or invalid) is silently skipped rather than
    raising, since a retry of a partially-completed operation legitimately resubmits
    already-moved ids (09 §11's resume/retry framing)."""
    source_workspace = await session.get(Workspace, source_workspace_id)
    was_dedicated = source_workspace is not None and source_workspace.dedicated_index

    moved_pages: list[uuid.UUID] = []
    for page_id in page_ids:
        page = await session.get(WikiPage, page_id)
        if page is None or page.workspace_id != source_workspace_id:
            continue
        current = await session.get(PageVersion, page.current_version_id)
        _, body = split_frontmatter(current.content)
        # Set before write_version: it derives the new version's diff object-store path
        # from page.workspace_id (versioning.py's _write_diff), which should land under
        # the *new* workspace's prefix going forward.
        page.workspace_id = target_workspace_id
        await versioning.write_version(
            session,
            page=page,
            body=body,
            author=actor,
            trigger=VersionTrigger.manual_edit,
            change_summary=f"Bulk move from {source_workspace_id} to {target_workspace_id}",
            frontmatter_updates={"workspace_id": target_workspace_id},
        )
        if was_dedicated:
            # write_version already marked the index stale (02 §7); the next reindex
            # sweep will pick the page up under its new workspace_id. But a page leaving
            # a dedicated workspace needs its *old* OpenSearch document removed now, not
            # lazily — index_page() only ever adds to OpenSearch for a currently-dedicated
            # workspace, never removes for a no-longer-dedicated one (09 §30).
            await delete_dedicated_page(page.page_id)
        moved_pages.append(page_id)

    moved_sources: list[uuid.UUID] = []
    for source_id in source_ids:
        source = await session.get(RawSource, source_id)
        if source is None or source.workspace_id != source_workspace_id:
            continue
        source.workspace_id = target_workspace_id
        relocate(source, target_workspace_id)
        moved_sources.append(source_id)

    if moved_pages or moved_sources:
        session.add(
            AdminActionLog(
                actor=actor,
                action="bulk_move",
                workspace_id=target_workspace_id,
                subject_ref=f"{source_workspace_id}->{target_workspace_id}",
                detail={
                    "source_workspace_id": source_workspace_id,
                    "target_workspace_id": target_workspace_id,
                    "moved_page_ids": [str(i) for i in moved_pages],
                    "moved_source_ids": [str(i) for i in moved_sources],
                },
            )
        )
    await session.flush()
    return BatchResult(moved_page_ids=moved_pages, moved_source_ids=moved_sources)
