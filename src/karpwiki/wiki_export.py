"""Wiki markdown export mirror (01 §1, 02 §2) — phase3-tasklist.md step 57.

A read-only (to external consumers), regenerated projection of a workspace's current wiki
at `/{workspace_id}/wiki/{path}` — the same `path` `wiki_page.path` already uses
(`overview.md`, `log.md`, `concepts/{slug}.md`, `entities/{slug}.md`,
`sources/{source_id}.md`), so the DB's own path convention already matches the file layout
`02` §2 specifies; no separate mapping is needed. Gives each workspace the same `raw/`,
`wiki/`, `schema` directory shape Karpathy's original pattern uses.

Unlike every other Object Store path in this codebase (raw sources, diffs — both
write-once by convention), this path is deliberately overwritten on every write: `02` §2
calls it "a read-only, regenerated mirror," not an archival artifact.

Written synchronously, in the same call as the DB write (`versioning.create_page`/
`write_version`) — the same "compute-on-write, non-transactional with the Metadata DB"
pattern `_write_diff` already uses (`09` §7). `02` §2 explicitly allows this: the export is
"not required to be transactional with the Metadata DB write, which remains the system of
record."

**`SCHEMA.md` real content, since step 59**: `schema.py` now owns real, versioned SCHEMA.md
storage (`SchemaVersion`, not a `wiki_page`, so it has no `current_version_id` to hook a
write off of the way pages do) — `schema.write` calls `write()` above directly with the
real content whenever a workspace's schema changes. `write_schema_placeholder` below is now
only for a workspace with **no** schema configured yet — `workspaces.create` writes it for
a brand-new workspace, and `export_workspace`'s backfill below writes either the real
current content or the placeholder, whichever applies.

**`export_workspace` is the rebuild-from-DB-truth backfill** (confirmed via
AskUserQuestion): `02` §3 calls the export "a regenerated projection," the same guarantee
`search.reindex_pending` gives the Full-Text Index. The write-through hooks above only fire
on a page's *next* write, so every page created before this step existed (all of Phase 1/2)
has no exported file until then — this closes that gap in one pass, and doubles as a repair
tool if the object store's mirror ever falls out of sync with the Metadata DB.

Deliberately does **not** write `index.md`: nothing creates a real `index`-type page yet
(phase3-tasklist.md step 60) — once one exists, it exports through the same write-through
hook as any other wiki page, no changes needed here.
"""

import io
import tarfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import objectstore
from .models import PageVersion, SchemaVersion, WikiPage, Workspace


def build_archive(workspace_id: str) -> bytes:
    """Bulk export (07 §5, phase3-tasklist.md step 74): a real downloadable `tar.gz` of
    everything under `/{workspace_id}/` in the object store — `wiki/`, `sources/`,
    `diffs/` alike, since all three are already namespaced by workspace and "wiki +
    sources for migration/backup" (07 §5's own wording) is naturally satisfied by the
    whole prefix, no filtering needed. Built fully in memory (see `api.py`'s
    `export_workspace_endpoint` docstring for why that's an accepted limitation here)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in objectstore.list_files(f"/{workspace_id}/"):
            payload = objectstore.read_bytes(path)
            info = tarfile.TarInfo(name=path.lstrip("/"))
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def export_path(workspace_id: str, path: str) -> str:
    return f"/{workspace_id}/wiki/{path}"


def write(*, workspace_id: str, path: str, content: str) -> str:
    """Write-through mirror of one page's current version content."""
    return objectstore.write_text(export_path(workspace_id, path), content)


def delete(*, workspace_id: str, path: str) -> None:
    """Remove a page's mirrored file — only needed when a page leaves its workspace
    (bulk-move), since the old prefix's copy would otherwise reference a page that no
    longer lives there."""
    objectstore.delete(export_path(workspace_id, path))


def _schema_placeholder() -> str:
    return (
        "# SCHEMA.md\n\n"
        "No schema has been configured for this workspace yet — write one via "
        "`POST /workspaces/{workspace_id}/schema` (09 §6, phase3-tasklist.md step 59).\n"
    )


def write_schema_placeholder(*, workspace_id: str) -> str:
    return objectstore.write_text(export_path(workspace_id, "SCHEMA.md"), _schema_placeholder())


async def export_workspace(session: AsyncSession, *, workspace_id: str) -> int:
    """Write every current page version's mirror, plus SCHEMA.md (real content if a schema
    is configured, the placeholder otherwise), from DB truth. Returns the number of pages
    exported."""
    result = await session.execute(
        select(WikiPage, PageVersion)
        .join(PageVersion, WikiPage.current_version_id == PageVersion.version_id)
        .where(WikiPage.workspace_id == workspace_id)
    )
    count = 0
    for page, version in result.all():
        write(workspace_id=page.workspace_id, path=page.path, content=version.content)
        count += 1

    workspace = await session.get(Workspace, workspace_id)
    if workspace is not None:
        if workspace.current_schema_version_id is not None:
            schema_version = await session.get(SchemaVersion, workspace.current_schema_version_id)
            if schema_version is not None:
                write(workspace_id=workspace_id, path="SCHEMA.md", content=schema_version.content)
        else:
            write_schema_placeholder(workspace_id=workspace_id)
    return count
