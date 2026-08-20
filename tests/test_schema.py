"""Real per-workspace SCHEMA.md storage, parsing, and validation (01 §7, 09 §6, 09 §26) —
phase3-tasklist.md step 59."""

import pytest

from karpwiki import schema


def test_parse_rejects_invalid_yaml():
    with pytest.raises(schema.SchemaValidationError, match="invalid YAML"):
        schema.parse("workspace_id: [unclosed")


def test_parse_rejects_a_non_mapping_document():
    with pytest.raises(schema.SchemaValidationError, match="mapping"):
        schema.parse("- just\n- a\n- list\n")


def test_parse_requires_workspace_id():
    with pytest.raises(schema.SchemaValidationError):
        schema.parse("ingestion_policy: gated\n")


def test_parse_fills_defaults_for_everything_optional():
    parsed = schema.parse("workspace_id: eng-docs\n")
    assert parsed.workspace_id == "eng-docs"
    assert parsed.document_types == []
    assert parsed.ingestion_policy == "auto"
    assert parsed.thresholds.classification.min_confidence is None
    assert parsed.llm.classifier is None


def test_parse_full_template():
    content = """
workspace_id: eng-docs
document_types:
  - eng.design-doc
  - eng.runbook
page_conventions:
  required_tags_min: 3
  additional_required_tags: [team]
curator:
  tone: concise
  concept_vs_entity: "entity = a named thing"
ingestion_policy: gated
llm:
  classifier:
    model: openai:gpt-5-nano
  curator:
    model: openai:gpt-5
thresholds:
  staleness:
    high_traffic_days: 30
    low_traffic_days: 200
  classification:
    min_confidence: 0.9
  dedup:
    near_duplicate_score: 0.5
  orphan:
    query_log_lookback_days: 60
retention:
  superseded_source_days: 90
  page_version_max_count: 20
"""
    parsed = schema.parse(content)
    assert parsed.document_types == ["eng.design-doc", "eng.runbook"]
    assert parsed.page_conventions.required_tags_min == 3
    assert parsed.page_conventions.additional_required_tags == ["team"]
    assert parsed.curator.tone == "concise"
    assert parsed.ingestion_policy == "gated"
    assert parsed.llm.classifier.model == "openai:gpt-5-nano"
    assert parsed.llm.curator.model == "openai:gpt-5"
    assert parsed.thresholds.staleness.high_traffic_days == 30
    assert parsed.thresholds.staleness.low_traffic_days == 200
    assert parsed.thresholds.classification.min_confidence == 0.9
    assert parsed.thresholds.dedup.near_duplicate_score == 0.5
    assert parsed.thresholds.orphan.query_log_lookback_days == 60
    assert parsed.retention.superseded_source_days == 90
    assert parsed.retention.page_version_max_count == 20


def test_parse_rejects_an_invalid_ingestion_policy():
    with pytest.raises(schema.SchemaValidationError):
        schema.parse("workspace_id: eng-docs\ningestion_policy: sometimes\n")


def test_as_dict_round_trips_for_resolve_model():
    parsed = schema.parse(
        "workspace_id: eng-docs\nllm:\n  curator:\n    model: openai:gpt-5\n"
    )
    as_dict = schema.as_dict(parsed)
    assert as_dict["llm"]["curator"]["model"] == "openai:gpt-5"


def test_as_dict_of_none_is_none():
    assert schema.as_dict(None) is None


async def test_write_creates_a_version_and_moves_the_pointer(session, workspace):
    version = await schema.write(
        session,
        workspace=workspace,
        content=f"workspace_id: {workspace.workspace_id}\n",
        author="user:deepak",
    )
    assert workspace.current_schema_version_id == version.version_id
    assert version.workspace_id == workspace.workspace_id


async def test_write_rejects_a_mismatched_workspace_id(session, workspace):
    with pytest.raises(schema.SchemaValidationError, match="does not match"):
        await schema.write(
            session, workspace=workspace, content="workspace_id: some-other-ws\n", author="user:deepak"
        )


async def test_write_rejects_invalid_content_without_touching_the_pointer(session, workspace):
    with pytest.raises(schema.SchemaValidationError):
        await schema.write(session, workspace=workspace, content="not: [valid", author="user:deepak")
    assert workspace.current_schema_version_id is None


async def test_load_returns_none_with_no_schema_configured(session, workspace):
    assert await schema.load(session, workspace_id=workspace.workspace_id) is None


async def test_load_returns_the_current_parsed_schema(session, workspace):
    await schema.write(
        session,
        workspace=workspace,
        content=f"workspace_id: {workspace.workspace_id}\ningestion_policy: gated\n",
        author="user:deepak",
    )
    loaded = await schema.load(session, workspace_id=workspace.workspace_id)
    assert loaded is not None
    assert loaded.ingestion_policy == "gated"


async def test_write_again_moves_the_pointer_to_the_new_version(session, workspace):
    v1 = await schema.write(
        session, workspace=workspace, content=f"workspace_id: {workspace.workspace_id}\n", author="user:deepak"
    )
    v2 = await schema.write(
        session,
        workspace=workspace,
        content=f"workspace_id: {workspace.workspace_id}\ningestion_policy: gated\n",
        author="user:deepak",
    )
    assert v1.version_id != v2.version_id
    assert workspace.current_schema_version_id == v2.version_id


async def test_rollback_restores_prior_content_as_a_new_version(session, workspace):
    v1 = await schema.write(
        session, workspace=workspace, content=f"workspace_id: {workspace.workspace_id}\n", author="user:deepak"
    )
    await schema.write(
        session,
        workspace=workspace,
        content=f"workspace_id: {workspace.workspace_id}\ningestion_policy: gated\n",
        author="user:deepak",
    )
    restored = await schema.rollback(
        session, workspace=workspace, target_version_id=v1.version_id, author="user:admin"
    )
    assert restored.content == v1.content
    assert restored.restored_from_version_id == v1.version_id
    assert restored.version_id != v1.version_id
    assert workspace.current_schema_version_id == restored.version_id


async def test_rollback_rejects_a_version_from_another_workspace(session, workspace, other_workspace):
    other_version = await schema.write(
        session,
        workspace=other_workspace,
        content=f"workspace_id: {other_workspace.workspace_id}\n",
        author="user:deepak",
    )
    with pytest.raises(ValueError, match="does not belong"):
        await schema.rollback(
            session, workspace=workspace, target_version_id=other_version.version_id, author="user:admin"
        )


async def test_history_lists_newest_first(session, workspace):
    v1 = await schema.write(
        session, workspace=workspace, content=f"workspace_id: {workspace.workspace_id}\n", author="user:deepak"
    )
    v2 = await schema.write(
        session,
        workspace=workspace,
        content=f"workspace_id: {workspace.workspace_id}\ningestion_policy: gated\n",
        author="user:deepak",
    )
    versions, next_cursor = await schema.history(session, workspace_id=workspace.workspace_id)
    assert [v.version_id for v in versions] == [v2.version_id, v1.version_id]
    assert next_cursor is None


async def test_history_paginates(session, workspace):
    for i in range(3):
        await schema.write(
            session,
            workspace=workspace,
            content=f"workspace_id: {workspace.workspace_id}\nretention:\n  superseded_source_days: {i}\n",
            author="user:deepak",
        )
    page1, cursor1 = await schema.history(session, workspace_id=workspace.workspace_id, limit=2)
    assert len(page1) == 2
    assert cursor1 is not None
    page2, cursor2 = await schema.history(
        session, workspace_id=workspace.workspace_id, limit=2, cursor=cursor1
    )
    assert len(page2) == 1
    assert cursor2 is None
