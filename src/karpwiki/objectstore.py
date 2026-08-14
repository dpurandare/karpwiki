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


def read_text(path: str, *, url: str | None = None) -> str:
    fs, root = _fs_and_root(url)
    with fs.open(f"{root.rstrip('/')}{path}", "r") as handle:
        return handle.read()
