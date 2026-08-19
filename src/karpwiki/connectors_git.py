"""Git repo poller — the first concrete connector type (03 §2, phase2-tasklist.md step 54),
"the simplest state model (commit-SHA diffing)" per the tasklist's own wording.

Speaks the real git protocol via the `git` CLI (`asyncio.create_subprocess_exec`, explicit
argument lists — never a shell string, so there's no injection surface) rather than one
hosting provider's REST API, so it works against any remote (GitHub, GitLab, Bitbucket,
self-hosted) the way "Git repo poller" reads literally. Confirmed via AskUserQuestion over a
pure-Python git library before building.

State model: `Connector.last_sync_cursor = {"commit_sha": "<sha>"}` — one string, nothing
else. First run (no cursor yet) treats every file in the tree as new. A later run diffs the
stored SHA against the branch's current HEAD (`git diff --name-only`); only added/copied/
modified/renamed files become items — **deletions are not submitted as anything** (09 §4/
03 §2 frame a connector purely as discovering content to add, never named removing it; real
"deprecate a page whose source vanished" handling doesn't exist anywhere in this codebase
yet, connector or not, so this is a real, flagged scope boundary, not an oversight). A file
that fails to decode as UTF-8 is skipped, not submitted — this connector targets narrative
content, and every downstream pipeline stage expects text.

Credential: an HTTPS token only (`credential`, already resolved — step 53), embedded into
the clone URL as `https://<token>@host/...`. SSH remotes are out of scope for "the simplest
state model" — no known_hosts/key-format handling here.
"""

import asyncio
import os
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory

from . import connector_polling
from .connector_polling import ConnectorAuthError, DiscoveredItem
from .models import Connector

_CLONE_TIMEOUT_SECONDS = 60

_AUTH_FAILURE_MARKERS = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "permission denied",
    "403",
)


class _GitDiffUnavailable(Exception):
    """The stored `commit_sha` isn't reachable in this clone (force-push, rebase, or a
    genuinely stale cursor) — recovered by falling back to a full resync, not propagated."""


def _with_credential(repo_url: str, credential: str | None) -> str:
    if credential is None:
        return repo_url
    parsed = urllib.parse.urlsplit(repo_url)
    if parsed.scheme not in ("http", "https"):
        # SSH/git:// remotes don't take an embedded HTTPS token this way — out of scope.
        return repo_url
    host = parsed.hostname or ""
    netloc = f"{credential}@{host}" + (f":{parsed.port}" if parsed.port else "")
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


async def _run(args: list[str], *, cwd: str | None = None, timeout: float | None = None) -> str:
    # GIT_TERMINAL_PROMPT=0: a worker process has no TTY, so without this an auth failure
    # would hang waiting for a username/password prompt instead of failing fast with a
    # message `_clone` can actually classify — `_CLONE_TIMEOUT_SECONDS` is a backstop, not
    # the intended path.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout}s") from None
    if proc.returncode != 0:
        message = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
        raise RuntimeError(message)
    return stdout.decode()


async def _clone(repo_url: str, branch: str, credential: str | None, dest: str) -> None:
    url = _with_credential(repo_url, credential)
    try:
        await _run(
            ["clone", "--branch", branch, "--single-branch", url, dest],
            timeout=_CLONE_TIMEOUT_SECONDS,
        )
    except RuntimeError as exc:
        message = str(exc)
        if any(marker in message.lower() for marker in _AUTH_FAILURE_MARKERS):
            raise ConnectorAuthError(message) from exc
        raise


async def _changed_paths(clone_dir: str, old_sha: str, new_sha: str) -> list[str]:
    try:
        out = await _run(["diff", "--name-only", "--diff-filter=ACMR", old_sha, new_sha], cwd=clone_dir)
    except RuntimeError as exc:
        raise _GitDiffUnavailable(str(exc)) from exc
    return [p for p in out.splitlines() if p]


async def _all_paths(clone_dir: str) -> list[str]:
    out = await _run(["ls-tree", "-r", "--name-only", "HEAD"], cwd=clone_dir)
    return [p for p in out.splitlines() if p]


def _read_text_file(clone_dir: str, path: str) -> bytes | None:
    file_path = Path(clone_dir) / path
    if not file_path.is_file():
        return None
    raw = file_path.read_bytes()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return raw


class GitConnectorAdapter:
    """`connector.config`: `repo_url` (required), `branch` (default `"main"`)."""

    async def poll(self, connector: Connector, credential: str | None) -> tuple[list[DiscoveredItem], dict]:
        repo_url = connector.config.get("repo_url")
        if not repo_url:
            raise RuntimeError("git connector config is missing 'repo_url'")
        branch = connector.config.get("branch", "main")
        old_sha = connector.last_sync_cursor.get("commit_sha")

        with TemporaryDirectory() as clone_dir:
            await _clone(repo_url, branch, credential, clone_dir)
            new_sha = (await _run(["rev-parse", "HEAD"], cwd=clone_dir)).strip()

            if old_sha == new_sha:
                return [], {"commit_sha": new_sha}

            if old_sha is None:
                paths = await _all_paths(clone_dir)
            else:
                try:
                    paths = await _changed_paths(clone_dir, old_sha, new_sha)
                except _GitDiffUnavailable:
                    paths = await _all_paths(clone_dir)

            items = []
            for path in paths:
                content = _read_text_file(clone_dir, path)
                if content is not None:
                    items.append(DiscoveredItem(filename=path, content=content))

            return items, {"commit_sha": new_sha}


# Registers "git" the moment this module is imported — tasks.py imports it for exactly this
# side effect, mirroring how `default_authenticator()`'s providers are all defined in one
# module rather than needing a separate registration call site.
connector_polling.ADAPTERS["git"] = GitConnectorAdapter()
