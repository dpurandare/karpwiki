"""Workspace templates (07 §5, phase3-tasklist.md step 75)."""

import pytest

from karpwiki import schema, workspace_templates


def test_list_templates_returns_both_named_examples():
    names = {t["name"] for t in workspace_templates.list_templates()}
    assert names == {"policy", "engineering-docs"}


@pytest.mark.parametrize("name", ["policy", "engineering-docs"])
def test_render_produces_valid_schema_content(name):
    content = workspace_templates.render(name, workspace_id="acme-ws")
    parsed = schema.parse(content)
    assert parsed.workspace_id == "acme-ws"
    assert parsed.document_types


def test_render_fills_in_the_given_workspace_id():
    a = workspace_templates.render("policy", workspace_id="ws-a")
    b = workspace_templates.render("policy", workspace_id="ws-b")
    assert schema.parse(a).workspace_id == "ws-a"
    assert schema.parse(b).workspace_id == "ws-b"


def test_render_rejects_an_unknown_template():
    with pytest.raises(workspace_templates.UnknownTemplateError):
        workspace_templates.render("nonexistent", workspace_id="acme-ws")


def test_policy_template_overrides_reflect_a_slower_review_cadence():
    parsed = schema.parse(workspace_templates.render("policy", workspace_id="acme-ws"))
    assert parsed.ingestion_policy == "gated"
    assert parsed.thresholds.classification.min_confidence == 0.85


def test_engineering_docs_template_overrides_reflect_a_faster_churn_cadence():
    parsed = schema.parse(workspace_templates.render("engineering-docs", workspace_id="acme-ws"))
    assert parsed.thresholds.staleness.high_traffic_days == 30
    assert parsed.thresholds.dedup.near_duplicate_score == 0.70
