"""Versioning model (01 §5).

Wiki pages are append-only at the version level: every write creates a `page_version`
and moves `wiki_page.current_version_id`. Rollback is non-destructive — it creates a
*new* version holding the restored content rather than deleting history.
"""

import difflib
import uuid
from collections.abc import Sequence

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import objectstore
from .frontmatter import DEFAULT_REQUIRED_TAGS_MIN, validate_frontmatter
from .models import (
    IndexState,
    IndexStatus,
    IndexType,
    PageStatus,
    PageType,
    PageVersion,
    VersionTrigger,
    WikiPage,
)


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
    """Restore a prior version's content as a new version (01 §5)."""
    target = await session.get(PageVersion, target_version_id)
    if target is None or target.page_id != page.page_id:
        raise ValueError(f"version {target_version_id} does not belong to page {page.page_id}")

    _, body = _split(target.content)
    return await write_version(
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


async def history(session: AsyncSession, page_id: uuid.UUID) -> list[PageVersion]:
    result = await session.execute(
        select(PageVersion)
        .where(PageVersion.page_id == page_id)
        .order_by(PageVersion.created_at, PageVersion.version_id)
    )
    return list(result.scalars())


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
