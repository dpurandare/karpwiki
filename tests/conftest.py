import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from karpwiki.models import Base, Workspace, WorkspaceStatus

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


async def _workspace(session, prefix: str, document_types: list[str]) -> Workspace:
    workspace = Workspace(
        workspace_id=f"{prefix}-{uuid.uuid4().hex[:8]}",
        name=prefix.replace("-", " ").title(),
        document_types=document_types,
        status=WorkspaceStatus.active,
        storage_bindings={"object_store": "file://./var/objectstore"},
    )
    session.add(workspace)
    await session.flush()
    return workspace


@pytest_asyncio.fixture
async def workspace(session):
    return await _workspace(session, "eng-docs", ["eng.design-doc", "eng.runbook"])


@pytest_asyncio.fixture
async def other_workspace(session):
    """A second workspace, for asserting that queries never cross the boundary."""
    return await _workspace(session, "policies", ["policy.hr"])
