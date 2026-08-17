"""Versioning model (01 §5).

Wiki pages are append-only at the version level: every write creates a `page_version`
and moves `wiki_page.current_version_id`. Rollback is non-destructive — it creates a
*new* version holding the restored content rather than deleting history.
"""

import difflib
import uuid
from collections.abc import Sequence

import yaml
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from . import objectstore
from .frontmatter import DEFAULT_REQUIRED_TAGS_MIN, validate_frontmatter
from .models import (
    AdminActionLog,
    IndexState,
    IndexStatus,
    IndexType,
    PageStatus,
    PageType,
    PageVersion,
    VersionTrigger,
    WikiPage,
)
from .pagination import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, decode_cursor, encode_cursor


def render_document(frontmatter: dict, body: str) -> str:
    """Serialize frontmatter + body back into a single markdown document."""
    block = yaml.safe_dump(frontmatter, sort_keys=False, default_flow_style=False).strip()
    return f"---\n{block}\n---\n{body}"


async def create_page(
    session: AsyncSession,
    *,
    workspace_id: str,
    path: str,
    page_type: PageType,
    title: str,
    description: str,
    date,
    tags: Sequence[str],
    body: str,
    author: str,
    trigger: VersionTrigger = VersionTrigger.ingest,
    status: PageStatus = PageStatus.draft,
    change_summary: str | None = None,
    required_tags_min: int = DEFAULT_REQUIRED_TAGS_MIN,
    additional_required_tags: Sequence[str] = (),
) -> WikiPage:
    """Create a page and its first version in one transaction."""
    page = WikiPage(
        page_id=uuid.uuid4(),
        workspace_id=workspace_id,
        path=path,
        page_type=page_type,
        status=status,
    )
    session.add(page)
    # The page row must exist before its first version: page_version.page_id is an FK
    # to it, while wiki_page.current_version_id points back the other way (01 §5).
    await session.flush()

    version_id = uuid.uuid4()
    frontmatter = {
        "title": title,
        "description": description,
        "date": date,
        "tags": list(tags),
        "page_type": page_type.value,
        "workspace_id": workspace_id,
        "status": status.value,
        "current_version": str(version_id),
    }
    validate_frontmatter(
        frontmatter,
        required_tags_min=required_tags_min,
        additional_required_tags=additional_required_tags,
    )

    version = PageVersion(
        version_id=version_id,
        page_id=page.page_id,
        content=render_document(frontmatter, body),
        frontmatter=_jsonable(frontmatter),
        author=author,
        change_summary=change_summary,
        trigger=trigger,
        diff_ref=None,  # no previous version to diff against
    )
    session.add(version)
    await session.flush()

    page.current_version_id = version.version_id
    session.add(IndexStatus(page_id=page.page_id, index_type=IndexType.fts, state=IndexState.pending))
    await session.flush()
    return page


async def write_version(
    session: AsyncSession,
    *,
    page: WikiPage,
    body: str,
    author: str,
    trigger: VersionTrigger,
    change_summary: str | None = None,
    frontmatter_updates: dict | None = None,
    status: PageStatus | None = None,
    required_tags_min: int = DEFAULT_REQUIRED_TAGS_MIN,
    additional_required_tags: Sequence[str] = (),
    restored_from_version_id: uuid.UUID | None = None,
) -> PageVersion:
    """Append a new version to an existing page and move the current pointer."""
    previous = await session.get(PageVersion, page.current_version_id)
    if previous is None:
        raise ValueError(f"page {page.page_id} has no current version to build on")

    version_id = uuid.uuid4()
    frontmatter = dict(previous.frontmatter)
    frontmatter.update(frontmatter_updates or {})
    if status is not None:
        page.status = status
        frontmatter["status"] = status.value
    frontmatter["current_version"] = str(version_id)
    validate_frontmatter(
        frontmatter,
        required_tags_min=required_tags_min,
        additional_required_tags=additional_required_tags,
    )

    content = render_document(frontmatter, body)
    diff_ref = _write_diff(page.workspace_id, version_id, previous.content, content)

    version = PageVersion(
        version_id=version_id,
        page_id=page.page_id,
        content=content,
        frontmatter=_jsonable(frontmatter),
        author=author,
        change_summary=change_summary,
        trigger=trigger,
        diff_ref=diff_ref,
        restored_from_version_id=restored_from_version_id,
    )
    session.add(version)
    await session.flush()

    page.current_version_id = version.version_id
    await _mark_stale(session, page.page_id)
    await session.flush()
    return version


async def rollback(
    session: AsyncSession,
    *,
    page: WikiPage,
    target_version_id: uuid.UUID,
    author: str,
    change_summary: str | None = None,
) -> PageVersion:
    """Restore a prior version's content as a new version (01 §5, 05 §6)."""
    target = await session.get(PageVersion, target_version_id)
    if target is None or target.page_id != page.page_id:
        raise ValueError(f"version {target_version_id} does not belong to page {page.page_id}")

    _, body = _split(target.content)
    version = await write_version(
        session,
        page=page,
        body=body,
        author=author,
        trigger=VersionTrigger.rollback,
        change_summary=change_summary or f"rollback to {target_version_id}",
        frontmatter_updates={
            key: value
            for key, value in target.frontmatter.items()
            if key not in {"current_version"}
        },
        restored_from_version_id=target_version_id,
    )

    # 05 §6: "logged to admin_action_log and log.md" — the latter is a rendering concern
    # (curate.render_log_body / ingestion.refresh_log, 09 §23), this is the audit write.
    session.add(
        AdminActionLog(
            actor=author,
            action="rollback_page",
            workspace_id=page.workspace_id,
            subject_ref=page.path,
            detail={
                "page_id": str(page.page_id),
                "restored_from_version_id": str(target_version_id),
                "new_version_id": str(version.version_id),
            },
        )
    )
    await session.flush()
    return version


async def history(session: AsyncSession, page_id: uuid.UUID) -> list[PageVersion]:
    result = await session.execute(
        select(PageVersion)
        .where(PageVersion.page_id == page_id)
        .order_by(PageVersion.created_at, PageVersion.version_id)
    )
    return list(result.scalars())


async def list_versions(
    session: AsyncSession,
    *,
    page_id: uuid.UUID,
    limit: int = DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> tuple[list[PageVersion], str | None]:
    """05 §6's Version Browser list, newest first, cursor-paginated per 09 §14 — unlike
    `history` above (oldest-first, unpaginated, kept as-is for its existing callers)."""
    limit = min(limit, MAX_LIST_LIMIT)
    stmt = select(PageVersion).where(PageVersion.page_id == page_id)
    if cursor is not None:
        created_at, version_id = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(PageVersion.created_at, PageVersion.version_id) < tuple_(created_at, version_id)
        )
    stmt = stmt.order_by(PageVersion.created_at.desc(), PageVersion.version_id.desc()).limit(
        limit + 1
    )
    versions = list((await session.execute(stmt)).scalars())

    next_cursor = None
    if len(versions) > limit:
        versions = versions[:limit]
        last = versions[-1]
        next_cursor = encode_cursor(last.created_at, last.version_id)
    return versions, next_cursor


async def diff(
    session: AsyncSession,
    *,
    page_id: uuid.UUID,
    from_version_id: uuid.UUID,
    to_version_id: uuid.UUID,
) -> str:
    """A diff between any two versions of one page (05 §6), recomputed directly from their
    stored `content` rather than composing `diff_ref` (09 §23) — that cache only ever holds
    the diff against the *immediately previous* version, not an arbitrary pair."""
    from_version = await session.get(PageVersion, from_version_id)
    to_version = await session.get(PageVersion, to_version_id)
    for version, version_id in ((from_version, from_version_id), (to_version, to_version_id)):
        if version is None or version.page_id != page_id:
            raise ValueError(f"version {version_id} does not belong to page {page_id}")

    return "".join(
        difflib.unified_diff(
            from_version.content.splitlines(keepends=True),
            to_version.content.splitlines(keepends=True),
            fromfile=str(from_version_id),
            tofile=str(to_version_id),
        )
    )


def _split(document: str) -> tuple[dict, str]:
    from .frontmatter import split_frontmatter

    return split_frontmatter(document)


def _jsonable(frontmatter: dict) -> dict:
    """JSONB cannot hold date objects; store the ISO form."""
    return yaml.safe_load(yaml.safe_dump(frontmatter, default_flow_style=False)) | {
        "date": str(frontmatter["date"])
    }


def _write_diff(workspace_id: str, version_id: uuid.UUID, before: str, after: str) -> str:
    """Compute-on-write unified diff, stored in the object store (09 §7)."""
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="previous",
            tofile="current",
        )
    )
    return objectstore.write_text(objectstore.diff_path(workspace_id, version_id), diff)


async def _mark_stale(session: AsyncSession, page_id: uuid.UUID) -> None:
    """A new version makes an indexed page stale (02 §7); pending stays pending."""
    status = await session.get(IndexStatus, (page_id, IndexType.fts))
    if status is not None and status.state is IndexState.indexed:
        status.state = IndexState.stale
