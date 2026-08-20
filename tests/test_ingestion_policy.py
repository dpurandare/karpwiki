"""Effective ingestion_policy resolution (03 §7, 09 §6, 09 §13, phase3-tasklist.md step 59):
a workspace's own SCHEMA.md-configured policy, tightened (never relaxed) by a connector's
own policy — the "may only tighten" rule closed as a real, enforceable comparison now that a
workspace's own policy is real content."""

import hashlib
import uuid

from karpwiki import connectors, ingestion, objectstore, schema
from karpwiki.models import PipelineState, RawSource


async def _source(session, workspace_id, *, submitted_by="user:deepak"):
    source_id = uuid.uuid4()
    payload = b"content"
    key = f"/{workspace_id}/sources/{source_id}/doc.md"
    objectstore.write_bytes(key, payload)
    source = RawSource(
        source_id=source_id,
        workspace_id=workspace_id,
        object_key=key,
        filename="doc.md",
        content_hash=hashlib.sha256(payload).hexdigest(),
        submitted_by=submitted_by,
        pipeline_state=PipelineState.classified,
    )
    session.add(source)
    await session.flush()
    return source


async def test_defaults_to_auto_with_no_schema_and_no_connector(session, workspace):
    source = await _source(session, workspace.workspace_id)
    policy = await ingestion.resolve_ingestion_policy(session, source=source, workspace_schema=None)
    assert policy == "auto"


async def test_uses_the_workspace_schemas_policy(session, workspace):
    parsed = schema.parse(f"workspace_id: {workspace.workspace_id}\ningestion_policy: gated\n")
    source = await _source(session, workspace.workspace_id)
    policy = await ingestion.resolve_ingestion_policy(session, source=source, workspace_schema=parsed)
    assert policy == "gated"


async def test_a_gated_connector_tightens_an_auto_workspace(session, workspace):
    connector = await connectors.create(
        session, workspace_id=workspace.workspace_id, type="git", ingestion_policy="gated"
    )
    await session.flush()
    source = await _source(
        session, workspace.workspace_id, submitted_by=f"connector:{connector.connector_id}"
    )
    policy = await ingestion.resolve_ingestion_policy(session, source=source, workspace_schema=None)
    assert policy == "gated"


async def test_an_auto_connector_never_relaxes_a_gated_workspace(session, workspace):
    parsed = schema.parse(f"workspace_id: {workspace.workspace_id}\ningestion_policy: gated\n")
    connector = await connectors.create(
        session, workspace_id=workspace.workspace_id, type="git", ingestion_policy="auto"
    )
    await session.flush()
    source = await _source(
        session, workspace.workspace_id, submitted_by=f"connector:{connector.connector_id}"
    )
    policy = await ingestion.resolve_ingestion_policy(session, source=source, workspace_schema=parsed)
    assert policy == "gated"  # connector's "auto" cannot relax the workspace's "gated"


async def test_an_auto_connector_on_an_auto_workspace_stays_auto(session, workspace):
    connector = await connectors.create(
        session, workspace_id=workspace.workspace_id, type="git", ingestion_policy="auto"
    )
    await session.flush()
    source = await _source(
        session, workspace.workspace_id, submitted_by=f"connector:{connector.connector_id}"
    )
    policy = await ingestion.resolve_ingestion_policy(session, source=source, workspace_schema=None)
    assert policy == "auto"
