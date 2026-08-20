"""Workspace templates (07 §5, phase3-tasklist.md step 75) — predefined SCHEMA.md content for
common document-type categories, to bootstrap a new workspace with sensible taxonomy/thresholds
instead of the placeholder `wiki_export.write_schema_placeholder` every new workspace starts with
(01 §7's own "SCHEMA.md" concept; depends on step 59's real schema storage/validation).

**Content library only, confirmed via `AskUserQuestion`**: a template is fetched as ready-to-apply
YAML text via `GET /workspace-templates/{name}?workspace_id=...` and applied through the EXISTING
`POST /workspaces/{workspace_id}/schema` endpoint (step 59) — no change to workspace creation
itself, full reuse of the already-tested write/validate path. `07` §5's own wording ("bootstrap A
NEW workspace... instead of a blank one") scopes this to workspace creation, not overwriting an
already-configured workspace's schema later — applying one is just a normal `schema.write` call,
so nothing here prevents that either, it's simply not a distinct "apply to an existing workspace"
feature.

Exactly the two named examples from `07` §5's own text ("Policy workspace", "Engineering docs
workspace") — no others invented. Each renders a real `schema.WorkspaceSchema`-shaped dict (a
malformed template fails loudly at render time via `model_validate`, not silently at `schema.write`
time) and touches only the fields where a real, defensible domain reason exists to differ from the
platform default (`09` §6's own "optional — omit to inherit the platform default" spirit) — not
every field.
"""

import yaml

from .schema import WorkspaceSchema

_POLICY_SPEC = {
    "document_types": ["policy.hr", "policy.security", "policy.compliance"],
    # Policy content should be reviewed before going live — misrouting or auto-publishing a
    # compliance-sensitive document is more consequential than an engineering runbook.
    "ingestion_policy": "gated",
    "curator": {
        "tone": "Formal and precise; cites the governing policy document explicitly.",
        "concept_vs_entity": (
            "A policy topic (e.g. 'Remote Work') is a concept; a specific named policy "
            "document or role (e.g. 'Data Retention Policy v3') is an entity."
        ),
    },
    "page_conventions": {"required_tags_min": 2, "additional_required_tags": ["policy"]},
    "thresholds": {
        # Policies are reviewed on a longer cadence than technical docs (platform default:
        # 365) — low query traffic alone shouldn't flag one stale.
        "staleness": {"low_traffic_days": 545},
        # Higher bar than the platform default (0.75) before auto-classifying into a
        # compliance-sensitive workspace.
        "classification": {"min_confidence": 0.85},
    },
    # Audit-trail value in keeping superseded originals around longer than the platform
    # default (180 days).
    "retention": {"superseded_source_days": 365},
}

_ENGINEERING_DOCS_SPEC = {
    "document_types": ["eng.design-doc", "eng.runbook", "eng.api-reference"],
    "curator": {
        "tone": "Concise and technical; assumes familiarity with the codebase.",
        "concept_vs_entity": (
            "A pattern or technique (e.g. 'Retry with Backoff') is a concept; a specific "
            "named service, API, or component (e.g. 'Order Service', 'POST /checkout') is "
            "an entity."
        ),
    },
    "thresholds": {
        # Code changes quickly — a heavily-referenced doc should be checked for freshness
        # sooner than the platform default (90 days).
        "staleness": {"high_traffic_days": 30},
        # Looser than the platform default (0.60): engineering docs legitimately share a lot
        # of templated structure (many runbooks/design docs follow the same skeleton) without
        # being duplicates, so a higher bar avoids false-positive duplicate flags.
        "dedup": {"near_duplicate_score": 0.70},
    },
    # Engineering docs churn fast — no audit-trail reason to keep superseded sources as long
    # as the platform default (180 days).
    "retention": {"superseded_source_days": 90},
}

TEMPLATES: dict[str, dict] = {
    "policy": {"title": "Policy workspace", "spec": _POLICY_SPEC},
    "engineering-docs": {"title": "Engineering docs workspace", "spec": _ENGINEERING_DOCS_SPEC},
}


class UnknownTemplateError(KeyError):
    pass


def list_templates() -> list[dict]:
    return [{"name": name, "title": t["title"]} for name, t in TEMPLATES.items()]


def render(name: str, *, workspace_id: str) -> str:
    """The real, ready-to-POST YAML content for a template, `workspace_id` filled in — the
    only per-call substitution a template needs, since `schema.write` validates the content's
    own `workspace_id` field matches the target workspace."""
    if name not in TEMPLATES:
        raise UnknownTemplateError(name)
    spec = {"workspace_id": workspace_id, **TEMPLATES[name]["spec"]}
    WorkspaceSchema.model_validate(spec)
    return yaml.dump(spec, sort_keys=False)
