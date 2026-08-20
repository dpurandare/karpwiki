"""Maintenance Advisor task wrappers read live per-workspace SCHEMA.md overrides instead of
always the platform default (phase3-tasklist.md step 59) — `tests/test_advisor.py` already
covers each detector's own default-threshold behavior; this file covers only the new wiring
in `tasks.py`'s `_detect_*` functions, via a monkeypatched `advisor.run_*` capturing the
kwargs it was actually called with.
"""

from karpwiki import advisor, schema, tasks


def _capture(monkeypatch, target: str):
    calls = []

    async def _fake(session, *, workspace_id, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(advisor, target, _fake)
    return calls


async def test_detect_superseded_sources_reads_the_schemas_retention_days(
    session, workspace, task_db, monkeypatch
):
    await schema.write(
        session,
        workspace=workspace,
        content=f"workspace_id: {workspace.workspace_id}\nretention:\n  superseded_source_days: 30\n",
        author="user:deepak",
    )
    await session.commit()
    calls = _capture(monkeypatch, "run_superseded_source_detector")

    await tasks._detect_superseded_sources(workspace.workspace_id)

    assert calls == [{"retention_days": 30}]


async def test_detect_superseded_sources_uses_the_default_with_no_schema(
    session, workspace, task_db, monkeypatch
):
    calls = _capture(monkeypatch, "run_superseded_source_detector")
    await tasks._detect_superseded_sources(workspace.workspace_id)
    assert calls == [{}]


async def test_detect_existing_duplicates_reads_the_schemas_near_duplicate_score(
    session, workspace, task_db, monkeypatch
):
    await schema.write(
        session,
        workspace=workspace,
        content=f"workspace_id: {workspace.workspace_id}\nthresholds:\n  dedup:\n    near_duplicate_score: 0.42\n",
        author="user:deepak",
    )
    await session.commit()
    calls = _capture(monkeypatch, "run_existing_content_duplicate_detector")

    await tasks._detect_existing_duplicates(workspace.workspace_id)

    assert calls == [{"threshold": 0.42}]


async def test_detect_orphans_reads_the_schemas_lookback_days(session, workspace, task_db, monkeypatch):
    await schema.write(
        session,
        workspace=workspace,
        content=f"workspace_id: {workspace.workspace_id}\nthresholds:\n  orphan:\n    query_log_lookback_days: 15\n",
        author="user:deepak",
    )
    await session.commit()
    calls = _capture(monkeypatch, "run_orphan_detector")

    await tasks._detect_orphans(workspace.workspace_id)

    assert calls == [{"lookback_days": 15}]


async def test_detect_contradictions_reads_the_schemas_near_duplicate_score_as_max_similarity(
    session, workspace, task_db, monkeypatch
):
    await schema.write(
        session,
        workspace=workspace,
        content=f"workspace_id: {workspace.workspace_id}\nthresholds:\n  dedup:\n    near_duplicate_score: 0.42\n",
        author="user:deepak",
    )
    await session.commit()
    calls = _capture(monkeypatch, "run_contradiction_detector")

    await tasks._detect_contradictions(workspace.workspace_id)

    assert calls == [{"call": None, "max_similarity": 0.42}]


async def test_detect_staleness_tiered_reads_all_three_schema_fields(
    session, workspace, task_db, monkeypatch
):
    await schema.write(
        session,
        workspace=workspace,
        content=(
            f"workspace_id: {workspace.workspace_id}\n"
            "thresholds:\n"
            "  staleness:\n"
            "    high_traffic_days: 10\n"
            "    low_traffic_days: 400\n"
            "  orphan:\n"
            "    query_log_lookback_days: 20\n"
        ),
        author="user:deepak",
    )
    await session.commit()
    calls = _capture(monkeypatch, "run_staleness_detector")

    await tasks._detect_staleness_tiered(workspace.workspace_id)

    assert calls == [
        {"tiered": True, "high_traffic_days": 10, "low_traffic_days": 400, "traffic_lookback_days": 20}
    ]


async def test_detect_staleness_plain_is_never_schema_wired(session, workspace, task_db, monkeypatch):
    """09 §6's template has no flat `threshold_days` field — only tiered
    high/low_traffic_days — so the plain, non-tiered path has nothing to read."""
    await schema.write(
        session,
        workspace=workspace,
        content=f"workspace_id: {workspace.workspace_id}\nthresholds:\n  staleness:\n    high_traffic_days: 5\n",
        author="user:deepak",
    )
    await session.commit()
    calls = _capture(monkeypatch, "run_staleness_detector")

    await tasks._detect_staleness(workspace.workspace_id)

    assert calls == [{}]
