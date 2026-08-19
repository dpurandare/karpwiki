import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from karpwiki import config, tasks
from karpwiki.models import AccessPolicy, Base, DocumentType, Role, Workspace, WorkspaceStatus

TEST_DATABASE_URL = os.environ.get(
    "KARPWIKI_TEST_DATABASE_URL",
    "postgresql+asyncpg://karpwiki:karpwiki@localhost:5432/karpwiki_test",
)


@pytest.fixture(scope="session", autouse=True)
def object_store(tmp_path_factory):
    """Point the object store at a temp dir so diffs don't touch the repo."""
    root = tmp_path_factory.mktemp("objectstore")
    os.environ["KARPWIKI_OBJECT_STORE_URL"] = f"file://{root}"
    import karpwiki.config
    import karpwiki.objectstore

    karpwiki.config.OBJECT_STORE_URL = f"file://{root}"
    karpwiki.objectstore.OBJECT_STORE_URL = f"file://{root}"
    return root


@pytest.fixture(autouse=True)
def dispatched(monkeypatch):
    """Neutralizes every `.delay()` call for the duration of a test (phase2-tasklist.md
    step 32) — without this, api.py's real dispatch calls would publish to the real Redis
    broker this session's docker-compose infra runs, and this repo's own real worker
    containers (if up) would pick them up and try the dev DB, where a test's ids never
    exist (harmless no-ops, but noisy and an unnecessary hard dependency on a live broker
    for tests that don't care about dispatch). Autouse so every test gets this by default;
    a test that DOES care about dispatch takes `dispatched` as a fixture and reads the
    recorded calls straight off it."""
    task_names = (
        "classify_source",
        "curate_source",
        "reindex",
        "detect_staleness",
        "detect_superseded_sources",
        "detect_existing_duplicates",
        "detect_orphans",
        "detect_contradictions",
        "detect_staleness_tiered",
    )
    calls = {name: [] for name in task_names}
    for name in task_names:
        monkeypatch.setattr(
            getattr(tasks, name), "delay", lambda arg, name=name: calls[name].append(arg)
        )
    return calls


@pytest.fixture(autouse=True)
def generous_rate_limits(monkeypatch):
    """Rate limiting (phase2-tasklist.md step 48) uses a real, shared Redis instance whose
    fixed-window counters — unlike the Postgres `session` fixture — are never reset between
    tests, and dozens of tests submit/search/etc. as the same `deepak` principal within the
    same 60s window. Raising the configured limits (not mocking `ratelimit.check` itself)
    keeps the real code path exercised while making the default limits a non-issue at test
    volume; `test_ratelimit.py` overrides these back down to exercise real enforcement."""
    for name in (
        "RATE_LIMIT_SUBMIT_PER_PRINCIPAL",
        "RATE_LIMIT_SUBMIT_PER_WORKSPACE",
        "RATE_LIMIT_SEARCH_PER_PRINCIPAL",
        "RATE_LIMIT_SEARCH_PER_WORKSPACE",
        "RATE_LIMIT_GENERAL_PER_PRINCIPAL",
        "RATE_LIMIT_GENERAL_PER_WORKSPACE",
    ):
        monkeypatch.setattr(config, name, 1_000_000)


# No OpenSearch reset fixture (phase2-tasklist.md step 26): every `workspace`/
# `other_workspace`/`dedicated_workspace` fixture already mints a random `workspace_id`,
# so cross-test pollution within the shared `karpwiki-pages` index is impossible by
# construction — every query is workspace_id-filtered to that one test's own value. The
# index accumulates documents across local test runs (harmless, since old runs' random
# workspace_ids never match a new test's filter); `docker compose down -v` clears it along
# with every other named volume if that ever matters.


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _workspace(
    session, prefix: str, document_types: list[str], *, dedicated: bool = False
) -> Workspace:
    workspace = Workspace(
        workspace_id=f"{prefix}-{uuid.uuid4().hex[:8]}",
        name=prefix.replace("-", " ").title(),
        status=WorkspaceStatus.active,
        storage_bindings={"object_store": "file://./var/objectstore"},
        dedicated_index=dedicated,
    )
    session.add(workspace)
    await session.flush()
    session.add_all(
        DocumentType(type_code=code, workspace_id=workspace.workspace_id)
        for code in document_types
    )
    await session.flush()
    return workspace


@pytest_asyncio.fixture
async def workspace(session):
    return await _workspace(session, "eng-docs", ["eng.design-doc", "eng.runbook"])


@pytest_asyncio.fixture
async def other_workspace(session):
    """A second workspace, for asserting that queries never cross the boundary."""
    return await _workspace(session, "policies", ["policy.hr"])


@pytest_asyncio.fixture
async def dedicated_workspace(session):
    """A workspace on the OpenSearch backend (phase2-tasklist.md step 26), for tests
    exercising `dedicated_index.py` or the federated merge — never the same object as
    `workspace`/`other_workspace`, since not every test needs a live OpenSearch round trip."""
    return await _workspace(session, "large-corp", ["legal.contract"], dedicated=True)


@pytest_asyncio.fixture
async def client(session, workspace):
    """The app shares the test's session so assertions see uncommitted writes.

    Grants `deepak` (contributor), `casey` (reader), and `group:eng` (contributor) on
    `workspace` by default — the set every existing caller of this fixture relies on.
    """
    import karpwiki.api as api_module
    from karpwiki.api import create_app

    async def _one_session():
        yield session

    app = create_app()
    app.dependency_overrides[api_module._session] = _one_session

    session.add_all(
        [
            AccessPolicy(
                workspace_id=workspace.workspace_id, principal="deepak", role=Role.contributor
            ),
            AccessPolicy(workspace_id=workspace.workspace_id, principal="casey", role=Role.reader),
            AccessPolicy(
                workspace_id=workspace.workspace_id, principal="group:eng", role=Role.contributor
            ),
        ]
    )
    await session.flush()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gateway"
    ) as http:
        yield http


@pytest_asyncio.fixture
async def task_db(monkeypatch, session):
    """Points `karpwiki.db.SessionLocal` at the same test database, via its own
    connection pool — the same relationship a real Celery worker's session has to the
    Wiki Service's (phase2-tasklist.md step 30). Depends on `session` only for fixture
    ordering, so the schema already exists; task code must go through `session.commit()`
    on the `session` fixture's own session to see anything, same as a real worker only
    sees committed writes."""
    import karpwiki.db as db_module

    engine = create_async_engine(TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    yield
    await engine.dispose()
