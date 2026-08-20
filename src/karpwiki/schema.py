"""Real per-workspace `SCHEMA.md` storage, parsing, and validation (01 §7, 09 §6, 09 §26) —
phase3-tasklist.md step 59.

`01` §7: SCHEMA.md is "itself a versioned artifact (stored like a wiki page, `page_type`
not applicable — treated as workspace configuration)" — a plain YAML document (not
markdown+frontmatter like a wiki page), versioned through `SchemaVersion` (`models.py`),
its own table rather than `wiki_page`/`page_version`.

Every field below is **optional** (`None`/empty default) on purpose: `09` §6's own template
comment says "optional — omit to inherit the platform default," and every consuming
module (`ingestion.py`, `dedup.py`, `advisor.py`, `llm.py`) already has its own hardcoded
default constant. Mirroring those defaults into this module's Pydantic models would create
either a circular import (this module would need to import `ingestion.py`, which needs to
import this module for the confidence-gate override) or silent drift between two copies of
the same number — `load()` returns `None` fields for anything unset, and each consumer
falls back to its own existing constant exactly the way it already does for a
directly-injected `None` override (e.g. `ingestion.classify_source`'s own
`min_confidence: float | None = None` parameter).

`document_types` is deliberately **descriptive only, not authoritative** — the real
taxonomy routing table is `document_type` rows + the existing admin CRUD (phase2-tasklist.md
step 22); reconciling/syncing the two would be a materially bigger feature (validation,
sync-on-write, conflict resolution) than this step's own scope, so this field is parsed and
returned but nothing reads it to drive routing.

`retention.page_version_max_count` is parsed and stored like every other field, but nothing
in this codebase enforces a version-count cap anywhere (checked directly — no pruning
mechanism for it exists) — flagged here rather than silently implying it does something.
"""

import uuid
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from . import wiki_export
from .models import SchemaVersion, Workspace
from .pagination import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, decode_cursor, encode_cursor


class PageConventions(BaseModel):
    required_tags_min: int | None = None
    additional_required_tags: list[str] = Field(default_factory=list)


class Curator(BaseModel):
    tone: str | None = None
    concept_vs_entity: str | None = None


class LlmRoleOverride(BaseModel):
    model: str


class LlmOverrides(BaseModel):
    classifier: LlmRoleOverride | None = None
    curator: LlmRoleOverride | None = None


class StalenessThresholds(BaseModel):
    high_traffic_days: int | None = None
    low_traffic_days: int | None = None


class ClassificationThresholds(BaseModel):
    min_confidence: float | None = None


class DedupThresholds(BaseModel):
    near_duplicate_score: float | None = None


class OrphanThresholds(BaseModel):
    query_log_lookback_days: int | None = None


class FeedbackThresholds(BaseModel):
    """Search result feedback loop (07 §4, phase3-tasklist.md step 68)."""

    lookback_days: int | None = None
    min_count: int | None = None
    low_rating_threshold: float | None = None


class Thresholds(BaseModel):
    staleness: StalenessThresholds = Field(default_factory=StalenessThresholds)
    classification: ClassificationThresholds = Field(default_factory=ClassificationThresholds)
    dedup: DedupThresholds = Field(default_factory=DedupThresholds)
    orphan: OrphanThresholds = Field(default_factory=OrphanThresholds)
    feedback: FeedbackThresholds = Field(default_factory=FeedbackThresholds)


class Retention(BaseModel):
    superseded_source_days: int | None = None
    page_version_max_count: int | None = None


class WorkspaceSchema(BaseModel):
    """Validated form of a workspace's SCHEMA.md (09 §6's own template)."""

    workspace_id: str
    document_types: list[str] = Field(default_factory=list)
    page_conventions: PageConventions = Field(default_factory=PageConventions)
    curator: Curator = Field(default_factory=Curator)
    ingestion_policy: Literal["auto", "gated"] = "auto"
    llm: LlmOverrides = Field(default_factory=LlmOverrides)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    retention: Retention = Field(default_factory=Retention)


class SchemaValidationError(ValueError):
    """SCHEMA.md content failed YAML parsing or the `WorkspaceSchema` contract."""


def parse(content: str) -> WorkspaceSchema:
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise SchemaValidationError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SchemaValidationError("SCHEMA.md must be a YAML mapping at the top level")
    try:
        return WorkspaceSchema.model_validate(raw)
    except ValidationError as exc:
        raise SchemaValidationError(str(exc)) from exc


def as_dict(parsed: WorkspaceSchema | None) -> dict | None:
    """The shape `llm.resolve_model`'s own `schema: dict | None` parameter expects —
    `None` in, `None` out, so every call site can pass this straight through."""
    return parsed.model_dump() if parsed is not None else None


async def load(session: AsyncSession, *, workspace_id: str) -> WorkspaceSchema | None:
    """The current parsed schema for a workspace, or `None` if none has been configured
    yet — every consumer treats `None` the same as "no overrides, use my own default."""
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None or workspace.current_schema_version_id is None:
        return None
    version = await session.get(SchemaVersion, workspace.current_schema_version_id)
    if version is None:
        return None
    return parse(version.content)


async def write(
    session: AsyncSession,
    *,
    workspace: Workspace,
    content: str,
    author: str,
    change_summary: str | None = None,
    restored_from_version_id: uuid.UUID | None = None,
) -> SchemaVersion:
    """Parse+validate, then append a new version and move the current pointer — mirrors
    `versioning.create_page`/`write_version`'s own shape for wiki pages (01 §5), applied to
    workspace configuration instead (01 §7: "page_type not applicable"). Write-through to
    the wiki export mirror (step 57's `wiki_export.write`), replacing the placeholder
    `workspaces.create` wrote for a brand-new, not-yet-configured workspace."""
    parsed = parse(content)
    if parsed.workspace_id != workspace.workspace_id:
        raise SchemaValidationError(
            f"SCHEMA.md workspace_id {parsed.workspace_id!r} does not match "
            f"{workspace.workspace_id!r}"
        )
    version = SchemaVersion(
        version_id=uuid.uuid4(),
        workspace_id=workspace.workspace_id,
        content=content,
        author=author,
        change_summary=change_summary,
        restored_from_version_id=restored_from_version_id,
    )
    session.add(version)
    await session.flush()
    workspace.current_schema_version_id = version.version_id
    await session.flush()
    wiki_export.write(workspace_id=workspace.workspace_id, path="SCHEMA.md", content=content)
    return version


async def rollback(
    session: AsyncSession,
    *,
    workspace: Workspace,
    target_version_id: uuid.UUID,
    author: str,
    change_summary: str | None = None,
) -> SchemaVersion:
    """Restore a prior version's content as a new version (01 §7: "auditable and
    reversible") — mirrors `versioning.rollback` exactly, applied to `SchemaVersion`."""
    target = await session.get(SchemaVersion, target_version_id)
    if target is None or target.workspace_id != workspace.workspace_id:
        raise ValueError(
            f"schema version {target_version_id} does not belong to workspace "
            f"{workspace.workspace_id!r}"
        )
    return await write(
        session,
        workspace=workspace,
        content=target.content,
        author=author,
        change_summary=change_summary or f"rollback to {target_version_id}",
        restored_from_version_id=target_version_id,
    )


async def history(
    session: AsyncSession,
    *,
    workspace_id: str,
    limit: int = DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> tuple[list[SchemaVersion], str | None]:
    """Newest-first, cursor-paginated per 09 §14 — this is new code, so it gets the real
    contract from the start rather than joining phase3-tasklist.md step 66's own list of
    endpoints that never did (`versioning.list_versions` is the direct precedent)."""
    limit = min(limit, MAX_LIST_LIMIT)
    stmt = select(SchemaVersion).where(SchemaVersion.workspace_id == workspace_id)
    if cursor is not None:
        created_at, version_id = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(SchemaVersion.created_at, SchemaVersion.version_id) < tuple_(created_at, version_id)
        )
    stmt = stmt.order_by(SchemaVersion.created_at.desc(), SchemaVersion.version_id.desc()).limit(
        limit + 1
    )
    versions = list((await session.execute(stmt)).scalars())

    next_cursor = None
    if len(versions) > limit:
        versions = versions[:limit]
        last = versions[-1]
        next_cursor = encode_cursor(last.created_at, last.version_id)
    return versions, next_cursor
