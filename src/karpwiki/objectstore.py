"""Object Store adapter (02 §2, 08 §3).

One fsspec URL per deployment; `file://` locally, `s3://` and friends in a deployment.
Objects are write-once (02 §2).
"""

import fsspec

from .config import OBJECT_STORE_URL


def _fs_and_root(url: str | None = None):
    return fsspec.core.url_to_fs(url or OBJECT_STORE_URL)


def diff_path(workspace_id: str, version_id) -> str:
    """Path scheme for page-version diffs (02 §2, 09 §7)."""
    return f"/{workspace_id}/diffs/{version_id}.diff"


def write_text(path: str, text: str, *, url: str | None = None) -> str:
    fs, root = _fs_and_root(url)
    full = f"{root.rstrip('/')}{path}"
    fs.makedirs(full.rsplit("/", 1)[0], exist_ok=True)
    with fs.open(full, "w") as handle:
        handle.write(text)
    return path


def write_bytes(path: str, payload: bytes, *, url: str | None = None) -> str:
    """Write-once binary object (02 §2) — raw sources are stored verbatim, not decoded."""
    fs, root = _fs_and_root(url)
    full = f"{root.rstrip('/')}{path}"
    fs.makedirs(full.rsplit("/", 1)[0], exist_ok=True)
    with fs.open(full, "wb") as handle:
        handle.write(payload)
    return path


def read_bytes(path: str, *, url: str | None = None) -> bytes:
    fs, root = _fs_and_root(url)
    with fs.open(f"{root.rstrip('/')}{path}", "rb") as handle:
        return handle.read()


def read_text(path: str, *, url: str | None = None) -> str:
    fs, root = _fs_and_root(url)
    with fs.open(f"{root.rstrip('/')}{path}", "r") as handle:
        return handle.read()


def delete(path: str, *, url: str | None = None) -> None:
    """Remove an object. Only for staging objects that have been copied to their final
    key — objects at their final key are write-once and are aged out by lifecycle rules
    (02 §2), never deleted inline."""
    fs, root = _fs_and_root(url)
    full = f"{root.rstrip('/')}{path}"
    if fs.exists(full):
        fs.rm(full)


def exists(path: str, *, url: str | None = None) -> bool:
    fs, root = _fs_and_root(url)
    return fs.exists(f"{root.rstrip('/')}{path}")


def list_files(prefix: str, *, url: str | None = None) -> list[str]:
    """Every object's own `/`-rooted path (this module's own scheme, not the backend's raw
    key) under a prefix — the enumeration primitive `wiki_export.build_archive` (07 §5,
    phase3-tasklist.md step 74) needs to package a whole workspace's object-store content.
    `fs.find()` is one of fsspec's generic operations, same backend-agnostic reasoning
    `size_bytes` below already uses for `fs.du()`."""
    fs, root = _fs_and_root(url)
    full = f"{root.rstrip('/')}{prefix}"
    if not fs.exists(full):
        return []
    root_len = len(root.rstrip("/"))
    return [f[root_len:] for f in fs.find(full)]


def size_bytes(prefix: str, *, url: str | None = None) -> int:
    """Total bytes stored under a path prefix — the Storage Utilization dashboard's
    object-store metric (05 §8, phase2-tasklist.md step 44). `fs.du()` is one of fsspec's
    generic operations, implemented across every backend this module supports (local,
    s3, ...), so this needs no backend-specific branch."""
    fs, root = _fs_and_root(url)
    full = f"{root.rstrip('/')}{prefix}"
    if not fs.exists(full):
        return 0
    return fs.du(full)
