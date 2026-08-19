"""Read-only FUSE-mount access to the wiki export (02 §2, 08 §3, 09 §12) —
phase3-tasklist.md step 58.

Exposes only the regenerated markdown mirror `wiki_export.py` (step 57) writes,
`/{workspace_id}/wiki/...` — never `sources/`, `diffs/`, or `assets/` — and never write
access; a workspace admin must explicitly grant a principal `AccessPolicy.fuse_access`
first (09 §12: "opt-in per workspace... not automatic for every existing
reader/contributor").

The mount itself uses fsspec's own generic FUSE helper (`fsspec.fuse.run`, 08 §3: "fsspec
backends can be FUSE-mounted... regardless of the underlying object store") rather than a
custom driver — this module's own job is authorizing the caller and scoping/read-only-
wrapping the filesystem view before handing it to that helper, not implementing FUSE
semantics itself. `fsspec.fuse` has **no read-only option of its own** — its `FUSEr` calls
straight through to whatever filesystem object it's given for `write`/`create`/`mkdir`/
`rmdir`/`unlink`/`chmod` — so `_ReadOnlyFileSystem` below is what actually makes "never
write access" true, not the FUSE layer.

**Actually calling `fsspec.fuse.run` requires a kernel-level FUSE driver installed on the
host** (macFUSE on macOS, `fuse3` on Linux) — confirmed via AskUserQuestion as out of scope
to install/exercise live in this session. Importing `fsspec.fuse` itself fails at import
time without one (`fusepy`'s `ctypes.util.find_library("fuse")` raises `OSError` if it's
missing), so that import is deferred to inside `run_mount` only — everything else in this
module (the AuthZ check, the read-only view-scoping, both the actual app logic) is real,
importable, and independently testable without any FUSE driver present.

Identity resolution mirrors `mcp_server._resolve_stdio_principal` exactly: a FUSE-mount
process is the same shape as the MCP stdio transport — one local process, one caller, no
per-request headers — so it reuses the identical `KARPWIKI_MCP_TOKEN`/`KARPWIKI_MCP_USER`/
`KARPWIKI_MCP_GROUPS` env-var convention rather than inventing a second one.
"""

import argparse
import asyncio

import fsspec
from fsspec.implementations.dirfs import DirFileSystem
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config
from .models import AccessPolicy

# Methods `fsspec.fuse.FUSEr` calls straight through to the wrapped filesystem for a
# mutating FUSE operation (write/create/mkdir/rmdir/unlink/chmod, per its own source) —
# blocked here since fsspec's FUSE helper enforces none of this itself.
_WRITE_METHODS = ("mkdir", "makedirs", "rmdir", "rm", "rm_file", "touch", "chmod", "pipe", "pipe_file")


class FuseAccessDenied(PermissionError):
    """The principal has no `fuse_access` grant for this workspace (09 §12) — a workspace
    admin must grant it first, same as any other `access_policy` grant."""


class _ReadOnlyFileSystem:
    """Wraps any fsspec filesystem so every mutating call raises `PermissionError` instead
    of reaching the real backend. Delegates everything else (`ls`, `info`, `cat`, `open` in
    a read mode, ...) straight through to the wrapped filesystem."""

    def __init__(self, fs: fsspec.AbstractFileSystem) -> None:
        self._fs = fs

    def __getattr__(self, name: str):
        if name in _WRITE_METHODS:
            def _blocked(*args, **kwargs):
                raise PermissionError(
                    f"{name!r} is not permitted on the wiki export mount (09 §12: read-only)"
                )
            return _blocked
        return getattr(self._fs, name)

    def open(self, path: str, mode: str = "rb", *args, **kwargs):
        if any(c in mode for c in "wax+"):
            raise PermissionError(
                "write access is not permitted on the wiki export mount (09 §12: read-only)"
            )
        return self._fs.open(path, mode, *args, **kwargs)


async def check_fuse_access(
    session: AsyncSession, *, principal_keys: tuple[str, ...], workspace_id: str
) -> None:
    """Raises `FuseAccessDenied` unless at least one of the caller's `policy_keys` holds a
    `fuse_access` grant for this workspace."""
    result = await session.execute(
        select(AccessPolicy.principal).where(
            AccessPolicy.workspace_id == workspace_id,
            AccessPolicy.principal.in_(principal_keys),
            AccessPolicy.fuse_access.is_(True),
        )
    )
    if result.first() is None:
        raise FuseAccessDenied(f"no fuse_access grant for workspace {workspace_id!r}")


def scoped_filesystem(workspace_id: str) -> _ReadOnlyFileSystem:
    """A read-only view of exactly this workspace's wiki export — never
    `sources/`/`diffs/`/`assets/` (09 §12's own scope boundary)."""
    fs, root = fsspec.core.url_to_fs(config.OBJECT_STORE_URL)
    prefix = f"{root.rstrip('/')}/{workspace_id}/wiki"
    return _ReadOnlyFileSystem(DirFileSystem(path=prefix, fs=fs))


async def run_mount(*, workspace_id: str, mount_point: str) -> None:
    """Real entry point: `python -m karpwiki.wiki_mount --workspace-id ... --mount-point
    ...`. Resolves one local identity, checks `fuse_access`, then hands the scoped
    read-only filesystem to fsspec's own FUSE helper."""
    from . import mcp_server
    from .auth import default_authenticator
    from .db import session_scope

    authenticator = default_authenticator()
    principal = await mcp_server._resolve_stdio_principal(authenticator)

    async with session_scope() as session:
        await check_fuse_access(
            session, principal_keys=principal.policy_keys, workspace_id=workspace_id
        )

    fs = scoped_filesystem(workspace_id)
    import fsspec.fuse  # deferred — needs a real FUSE driver installed to even import

    fsspec.fuse.run(fs, "/", mount_point)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mount a workspace's wiki export read-only (09 §12)."
    )
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--mount-point", required=True)
    args = parser.parse_args()
    asyncio.run(run_mount(workspace_id=args.workspace_id, mount_point=args.mount_point))


if __name__ == "__main__":
    main()
