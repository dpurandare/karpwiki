"""Git connector adapter (03 §2, phase2-tasklist.md step 54) — exercised against a real
local git repository (`file://`), not mocked. Git operations are fast and fully hermetic
against a local repo, so there's no reason to fake them.
"""

import subprocess

import pytest

from karpwiki.connector_polling import ConnectorAuthError
from karpwiki.connectors_git import GitConnectorAdapter, _with_credential
from karpwiki.models import Connector


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin(tmp_path):
    repo = tmp_path / "origin"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("# Hello\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    return repo


def _connector(*, repo_url, branch=None, cursor=None):
    config = {"repo_url": repo_url}
    if branch is not None:
        config["branch"] = branch
    return Connector(config=config, last_sync_cursor=cursor or {})


def _commit(repo, message="update"):
    _git("add", ".", cwd=repo)
    _git("commit", "-m", message, cwd=repo)


def _head_sha(repo) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


async def test_first_poll_discovers_every_file(origin):
    connector = _connector(repo_url=f"file://{origin}")
    adapter = GitConnectorAdapter()

    items, cursor = await adapter.poll(connector, None)

    assert {i.filename: i.content for i in items} == {"README.md": b"# Hello\n"}
    assert cursor == {"commit_sha": _head_sha(origin)}


async def test_second_poll_discovers_only_added_file(origin):
    connector = _connector(repo_url=f"file://{origin}", cursor={"commit_sha": _head_sha(origin)})
    (origin / "new.md").write_text("New content.")
    _commit(origin, "add new.md")
    adapter = GitConnectorAdapter()

    items, cursor = await adapter.poll(connector, None)

    assert {i.filename: i.content for i in items} == {"new.md": b"New content."}
    assert cursor == {"commit_sha": _head_sha(origin)}


async def test_second_poll_discovers_modified_file_with_new_content(origin):
    connector = _connector(repo_url=f"file://{origin}", cursor={"commit_sha": _head_sha(origin)})
    (origin / "README.md").write_text("# Changed\n")
    _commit(origin, "edit readme")
    adapter = GitConnectorAdapter()

    items, _cursor = await adapter.poll(connector, None)

    assert {i.filename: i.content for i in items} == {"README.md": b"# Changed\n"}


async def test_deleted_files_are_not_submitted(origin):
    (origin / "to-delete.md").write_text("Bye.")
    _commit(origin, "add to-delete.md")
    connector = _connector(repo_url=f"file://{origin}", cursor={"commit_sha": _head_sha(origin)})
    (origin / "to-delete.md").unlink()
    _commit(origin, "remove to-delete.md")
    adapter = GitConnectorAdapter()

    items, _cursor = await adapter.poll(connector, None)

    assert items == []


async def test_unchanged_sha_discovers_nothing(origin):
    connector = _connector(repo_url=f"file://{origin}", cursor={"commit_sha": _head_sha(origin)})
    adapter = GitConnectorAdapter()

    items, cursor = await adapter.poll(connector, None)

    assert items == []
    assert cursor == {"commit_sha": _head_sha(origin)}


async def test_binary_file_is_skipped(origin):
    (origin / "image.bin").write_bytes(b"\xff\xd8\xff\xe0\x00\x10not-really-a-jpeg\xfe\xff")
    _commit(origin, "add binary")
    connector = _connector(repo_url=f"file://{origin}")
    adapter = GitConnectorAdapter()

    items, _cursor = await adapter.poll(connector, None)

    assert "image.bin" not in {i.filename for i in items}
    assert "README.md" in {i.filename for i in items}


async def test_missing_repo_url_raises(origin):
    connector = Connector(config={}, last_sync_cursor={})
    adapter = GitConnectorAdapter()

    with pytest.raises(RuntimeError, match="repo_url"):
        await adapter.poll(connector, None)


async def test_branch_override(tmp_path):
    repo = tmp_path / "origin"
    repo.mkdir()
    _git("init", "-b", "trunk", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "a.md").write_text("A")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)

    connector = _connector(repo_url=f"file://{repo}", branch="trunk")
    adapter = GitConnectorAdapter()

    items, _cursor = await adapter.poll(connector, None)

    assert {i.filename for i in items} == {"a.md"}


async def test_stale_cursor_falls_back_to_full_resync(origin):
    """A commit_sha not reachable in this clone (rewritten history, or a genuinely stale
    cursor from a different repo state) recovers as a full resync rather than failing the
    whole run."""
    connector = _connector(repo_url=f"file://{origin}", cursor={"commit_sha": "0" * 40})
    (origin / "new.md").write_text("New.")
    _commit(origin, "add new.md")
    adapter = GitConnectorAdapter()

    items, cursor = await adapter.poll(connector, None)

    assert {i.filename for i in items} == {"README.md", "new.md"}
    assert cursor == {"commit_sha": _head_sha(origin)}


def test_with_credential_embeds_token_in_https_url():
    assert (
        _with_credential("https://github.com/org/repo.git", "tok123")
        == "https://tok123@github.com/org/repo.git"
    )


def test_with_credential_no_op_without_a_credential():
    assert _with_credential("https://github.com/org/repo.git", None) == "https://github.com/org/repo.git"


def test_with_credential_leaves_ssh_urls_untouched():
    assert _with_credential("git@github.com:org/repo.git", "tok123") == "git@github.com:org/repo.git"


async def test_clone_classifies_an_auth_looking_failure_as_connector_auth_error(monkeypatch):
    """Hermetic classification test — no real network. A real network-dependent auth
    failure is exercised separately as a live check (spec/09-implementation-notes.md), not
    committed, matching this project's no-network-in-committed-tests convention."""
    import karpwiki.connectors_git as connectors_git

    async def _fails(*_args, **_kwargs):
        raise RuntimeError("remote: Invalid username or password.\nfatal: Authentication failed for 'https://example.com/repo.git/'")

    monkeypatch.setattr(connectors_git, "_run", _fails)

    with pytest.raises(ConnectorAuthError):
        await connectors_git._clone("https://example.com/repo.git", "main", None, "/tmp/unused")


async def test_clone_leaves_a_non_auth_failure_as_a_plain_error(monkeypatch):
    import karpwiki.connectors_git as connectors_git

    async def _fails(*_args, **_kwargs):
        raise RuntimeError("fatal: repository 'https://example.com/repo.git/' not found")

    monkeypatch.setattr(connectors_git, "_run", _fails)

    with pytest.raises(RuntimeError) as excinfo:
        await connectors_git._clone("https://example.com/repo.git", "main", None, "/tmp/unused")
    assert not isinstance(excinfo.value, ConnectorAuthError)
