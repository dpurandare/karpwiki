"""Metadata DB schema — the system of record (02 §3).

Field lists follow 02 §3's conceptual table definitions; enum values follow the states
defined in 01 §5, 02 §7, and 03 §1. Phase 1 covers the seven core tables plus
`access_policy` (09 §15); `document_type` arrives with multi-workspace routing (phase2-
tasklist.md step 22); `query_log` arrives with the search endpoint (step 25); `connector`
arrives with the connector framework in Phase 2.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class WorkspaceStatus(enum.Enum):
    active = "active"
    archived = "archived"


class ContentShape(enum.Enum):
    narrative = "narrative"
    structured_data = "structured_data"


class RawSourceStatus(enum.Enum):
    """Lifecycle/retention axis (02 §3) — distinct from PipelineState (09 §3)."""

    active = "active"
    superseded = "superseded"
    archived = "archived"
    rejected = "rejected"


class PipelineState(enum.Enum):
    """Ingestion-progress axis (03 §1); denormalized pointer per 09 §3."""

    submitted = "submitted"
    classifying = "classifying"
    classified = "classified"
    duplicate_check = "duplicate_check"
    pending_review = "pending_review"
    ingesting = "ingesting"
    ingested = "ingested"
    error = "error"
    rejected = "rejected"


# `pipeline_state` is referenced by raw_source and twice by ingestion_log. create_all
# dedupes the CREATE TYPE; Alembic does not, so a migration that adds a *later* table
# using this type must reference it with postgresql.ENUM(..., create_type=False).
PIPELINE_STATE_ENUM = Enum(PipelineState, name="pipeline_state")


class PageType(enum.Enum):
    overview = "overview"
    index = "index"
    log = "log"
    concept = "concept"
    entity = "entity"
    source = "source"
    comparison = "comparison"


class PageStatus(enum.Enum):
    draft = "draft"
    published = "published"
    archived = "archived"


class VersionTrigger(enum.Enum):
    ingest = "ingest"
    manual_edit = "manual_edit"
    rollback = "rollback"
    lint_fix = "lint_fix"
    prune = "prune"


class LinkType(enum.Enum):
    cross_reference = "cross_reference"
    cross_workspace = "cross_workspace"


class IndexType(enum.Enum):
    fts = "fts"


class IndexState(enum.Enum):
    pending = "pending"
    indexing = "indexing"
    indexed = "indexed"
    stale = "stale"
    error = "error"


class ReviewKind(enum.Enum):
    submission = "submission"
    classification = "classification"
    duplicate = "duplicate"
    reindex = "reindex"
    prune = "prune"
    stuck = "stuck"


class ReviewStatus(enum.Enum):
    open = "open"
    resolved = "resolved"


class Role(enum.Enum):
    """The three baseline roles (06 §3); finer-grained permissions are a roadmap item."""

    reader = "reader"
    contributor = "contributor"
    admin = "admin"


class ConnectorState(enum.Enum):
    """09 §13: an auth failure disables a connector rather than retrying, distinctly from
    an admin-initiated disable — so the Notification Service / operational-health surface
    (05 §8, phase2-tasklist.md step 55) can tell the two apart."""

    enabled = "enabled"
    disabled = "disabled"
    disabled_auth = "disabled_auth"


class Workspace(Base):
    __tablename__ = "workspace"

    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    # Real SCHEMA.md storage (01 §7, 09 §26, phase3-tasklist.md step 59) — replaces the old
    # free-text `schema_ref` pointer string (nothing ever loaded/parsed/versioned real
    # content behind it) with a real FK to this workspace's current, versioned
    # `schema_version` row; `use_alter` since `schema_version.workspace_id` FKs back here
    # (same circular-FK shape `wiki_page.current_version_id` already uses).
    current_schema_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "schema_version.version_id", use_alter=True, name="fk_workspace_current_schema_version"
        )
    )
    status: Mapped[WorkspaceStatus] = mapped_column(
        Enum(WorkspaceStatus, name="workspace_status"), default=WorkspaceStatus.active
    )
    storage_bindings: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 02 §4: "workspaces with very large corpora or stricter isolation requirements may get
    # a dedicated index instance" — OpenSearch (08 §2) instead of the shared Postgres FTS
    # (phase2-tasklist.md step 26). False for every workspace by default.
    dedicated_index: Mapped[bool] = mapped_column(default=False)


class DocumentType(Base):
    """A workspace's taxonomy slice (02 §3), promoted from Phase 1's `Workspace.document_types`
    array column (phase2-tasklist.md step 22) — 09 §25 explains why `type_code` is the primary
    key rather than part of a composite `(workspace_id, type_code)` key: classification produces
    a bare `type_code` with no workspace to disambiguate it against, so a code must already
    determine its one owning workspace for routing (03 §3) to mean anything.
    """

    __tablename__ = "document_type"

    type_code: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspace.workspace_id"), index=True
    )
    description: Mapped[str | None] = mapped_column(Text)


class RawSource(Base):
    __tablename__ = "raw_source"

    source_id: Mapped[uuid.UUID] = _uuid_pk()
    # Nullable until `classifying` resolves it: the raw_source row is created at
    # `submitted`, before any workspace is known (03 §1).
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace.workspace_id"), index=True
    )
    object_key: Mapped[str] = mapped_column(String(1024))
    filename: Mapped[str] = mapped_column(String(512))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_shape: Mapped[ContentShape | None] = mapped_column(
        Enum(ContentShape, name="content_shape")
    )
    submitted_by: Mapped[str] = mapped_column(String(255))
    # structured_data only (03 §3-4)
    artifact_identity: Mapped[str | None] = mapped_column(String(512))
    source_version: Mapped[str | None] = mapped_column(String(128))
    source_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_source.source_id"))
    status: Mapped[RawSourceStatus] = mapped_column(
        Enum(RawSourceStatus, name="raw_source_status"), default=RawSourceStatus.active
    )
    # Set only where `status` flips to `superseded` (`ingestion._resolve_supersede`) — the
    # Superseded-Source Detector's retention-window check (05 §4, phase2-tasklist.md step
    # 37) needs "how long has this been superseded," which nothing else records.
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pipeline_state: Mapped[PipelineState] = mapped_column(
        PIPELINE_STATE_ENUM, default=PipelineState.submitted
    )
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Added phase2-tasklist.md step 43: the admin Raw Source Browser (05 §7) needs a
    # chronological, cursor-paginable order (09 §14's shared (created_at, id) convention),
    # and nothing else on this row records "when was this source submitted."
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class WikiPage(Base):
    __tablename__ = "wiki_page"

    page_id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.workspace_id"), index=True)
    path: Mapped[str] = mapped_column(String(1024))
    page_type: Mapped[PageType] = mapped_column(Enum(PageType, name="page_type"))
    # Nullable only between INSERT and the first version write; versioning.create_page
    # sets it in the same transaction (01 §5).
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("page_version.version_id", use_alter=True, name="fk_wiki_page_current_version")
    )
    status: Mapped[PageStatus] = mapped_column(
        Enum(PageStatus, name="page_status"), default=PageStatus.draft
    )


class PageVersion(Base):
    __tablename__ = "page_version"

    version_id: Mapped[uuid.UUID] = _uuid_pk()
    page_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wiki_page.page_id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    frontmatter: Mapped[dict] = mapped_column(JSONB)
    author: Mapped[str] = mapped_column(String(255))
    # clock_timestamp(), not now(): now() is transaction-scoped, so several versions
    # written in one transaction would share a timestamp and history ordering (05 §6)
    # would be nondeterministic.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    change_summary: Mapped[str | None] = mapped_column(Text)
    # Object-store path, /{workspace_id}/diffs/{version_id}.diff (09 §7)
    diff_ref: Mapped[str | None] = mapped_column(String(1024))
    trigger: Mapped[VersionTrigger] = mapped_column(Enum(VersionTrigger, name="version_trigger"))
    restored_from_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("page_version.version_id")
    )


class SchemaVersion(Base):
    """A workspace's `SCHEMA.md` content, versioned like a wiki page (01 §7: "auditable and
    reversible") but not one — "`page_type` not applicable — treated as workspace
    configuration" (01 §7's own words), so it gets its own table rather than living in
    `wiki_page`/`page_version` (phase3-tasklist.md step 59). `content` is the raw YAML text;
    `schema.py` parses/validates it into `WorkspaceSchema` on read, mirroring how
    `page_version.content` is the raw markdown+frontmatter document `versioning.py` renders
    and `frontmatter.py` parses back.
    """

    __tablename__ = "schema_version"

    version_id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.workspace_id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    change_summary: Mapped[str | None] = mapped_column(Text)
    restored_from_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schema_version.version_id")
    )


class PageLink(Base):
    __tablename__ = "page_link"

    from_page_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wiki_page.page_id"), primary_key=True
    )
    to_page_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wiki_page.page_id"), primary_key=True)
    link_type: Mapped[LinkType] = mapped_column(Enum(LinkType, name="link_type"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IndexStatus(Base):
    __tablename__ = "index_status"

    page_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wiki_page.page_id"), primary_key=True)
    index_type: Mapped[IndexType] = mapped_column(
        Enum(IndexType, name="index_type"), primary_key=True
    )
    state: Mapped[IndexState] = mapped_column(
        Enum(IndexState, name="index_state"), default=IndexState.pending
    )
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_content_version: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("page_version.version_id")
    )


class ReviewItem(Base):
    __tablename__ = "review_item"

    review_id: Mapped[uuid.UUID] = _uuid_pk()
    # Nullable: a submission or classification review item (03 §5, §3) can exist before a
    # workspace is resolved — for classification specifically, resolving it may be exactly
    # what assigns the workspace, so there may never be an automatic value to backfill.
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace.workspace_id"), index=True
    )
    kind: Mapped[ReviewKind] = mapped_column(Enum(ReviewKind, name="review_kind"))
    severity: Mapped[str | None] = mapped_column(String(32))
    subject_ref: Mapped[str] = mapped_column(String(512))
    proposed_action: Mapped[str | None] = mapped_column(Text)
    # Nullable, no default: ingest-time items (submission/classification/duplicate) still
    # have nowhere they *need* structured evidence — ingestion_log already carries their
    # history (09 §22). Maintenance Advisor items (reindex/prune, phase2-tasklist.md step
    # 36+) have no equivalent log to fall back on, so this is where their evidence
    # (reason, scope, page list, `raised_by`) lives.
    detail: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status"), default=ReviewStatus.open
    )
    resolved_action: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_by: Mapped[str | None] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestionLog(Base):
    """Append-only history of every pipeline transition (02 §5).

    `raw_source.pipeline_state` is the denormalized current pointer; this is the system of
    record for how a source got there (09 §3), and `log.md` is materialized from it.
    """

    __tablename__ = "ingestion_log"

    entry_id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_source.source_id"), index=True)
    # Null before `classifying` resolves the workspace, and on the initial transition.
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace.workspace_id"), index=True
    )
    from_state: Mapped[PipelineState | None] = mapped_column(PIPELINE_STATE_ENUM)
    to_state: Mapped[PipelineState] = mapped_column(PIPELINE_STATE_ENUM)
    actor: Mapped[str] = mapped_column(String(255))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    # clock_timestamp, not now(): several transitions can share one transaction, and
    # now() would make their order indeterminate (same reasoning as page_version).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class AdminActionLog(Base):
    """Audit trail of admin actions (02 §5): review item resolutions, rollbacks, manual
    edits, workspace/schema changes.

    02 §3's table gives every other log table an explicit field list except this one — one
    line of purpose, no schema (09 §22). Modeled on `IngestionLog`, the one other
    append-only actor/action/detail history in the schema, since nothing else here calls
    for a different shape.
    """

    __tablename__ = "admin_action_log"

    entry_id: Mapped[uuid.UUID] = _uuid_pk()
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))
    # Null when the action predates/has no workspace (e.g. resolving a still-unassigned
    # classification review item) — same reasoning as ingestion_log.workspace_id above.
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace.workspace_id"), index=True
    )
    subject_ref: Mapped[str] = mapped_column(String(512))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class PageIndex(Base):
    """The Full-Text Index (02 §4) — the Platform's only query-time index.

    Derived, not authoritative: rebuildable from `page_version` at any time (02 §3). Each
    entry is tagged with `workspace_id`, `page_id`, and `version_id`, mirroring
    `index_status`, and `workspace_id` is a mandatory filter on every query so a federated
    search touches one index rather than fanning out (02 §4).
    """

    __tablename__ = "page_index"
    __table_args__ = (
        Index("ix_page_index_tsv", "tsv", postgresql_using="gin"),
        Index("ix_page_index_workspace", "workspace_id"),
    )

    page_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wiki_page.page_id"), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.workspace_id"))
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("page_version.version_id"))
    tsv: Mapped[str] = mapped_column(TSVECTOR)


class QueryLog(Base):
    """Every `search` call (04 §8, 02 §5): query text, principal, resolved workspaces, and
    returned page IDs/scores. Feeds the Maintenance Advisor's orphan/low-traffic detector
    (05 §2) once that exists. Retained 90 days then purged (09 §8) — `query_log.purge_older_than`
    exists for that; nothing schedules it automatically yet, same as every other still-manual
    maintenance job before the async layer (phase2-tasklist.md step 30+) is real.
    """

    __tablename__ = "query_log"

    query_id: Mapped[uuid.UUID] = _uuid_pk()
    principal: Mapped[str] = mapped_column(String(255), index=True)
    query_text: Mapped[str] = mapped_column(Text)
    resolved_workspaces: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    # [{"page_id": "<uuid>", "score": <float>}, ...] — a list, not a page_id[] + score[]
    # pair, so each hit's fields stay together rather than relying on parallel-array indices.
    results: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), index=True
    )
    # Added phase2-tasklist.md step 44: the Search Performance dashboard (05 §8) needs
    # p50/p95 latency, and nothing recorded a call's duration before this. Nullable — a
    # row from before this column existed has no value to backfill.
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class FeedbackRating(enum.Enum):
    up = "up"
    down = "down"


class QueryFeedback(Base):
    """Search result feedback loop (07 §4, phase3-tasklist.md step 68) — thumbs-up/down on
    one result of one search call, recorded alongside `query_log` (02 §5). Pure append, no
    uniqueness constraint: same shape as every other log stream here (`ingestion_log`,
    `admin_action_log`) — a principal can submit more than one rating over time for the
    same (query, page) pair, and the Maintenance Advisor's low-feedback signal aggregates
    across all of them rather than only ever keeping "the latest."
    """

    __tablename__ = "query_feedback"

    feedback_id: Mapped[uuid.UUID] = _uuid_pk()
    query_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("query_log.query_id"), index=True)
    page_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wiki_page.page_id"), index=True)
    principal: Mapped[str] = mapped_column(String(255), index=True)
    rating: Mapped[FeedbackRating] = mapped_column(Enum(FeedbackRating, name="feedback_rating"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), index=True
    )


class IdempotencyRecord(Base):
    """Stored response for an `Idempotency-Key` replay (09 §14).

    In Postgres rather than Redis on purpose: this is what stops a client retry after a
    timeout from creating a second raw_source and a second ingestion run, so losing it to a
    cache eviction would reintroduce exactly the defect it exists to prevent.
    """

    __tablename__ = "idempotency_record"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    principal: Mapped[str] = mapped_column(String(255), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(255), primary_key=True)
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccessPolicy(Base):
    """Who may do what in a workspace (06 §3) — the single table gateway AuthZ consults.

    Phase 1 ships authorization; authentication resolves a principal through a pluggable
    provider (09 §15). Principals are `user:<id>`, `group:<id>`, `client:<id>`, or
    `connector:<connector_id>` (Phase 2, 09 §13).
    """

    __tablename__ = "access_policy"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspace.workspace_id"), primary_key=True
    )
    principal: Mapped[str] = mapped_column(String(255), primary_key=True)
    # Fine-grained access control (07 §2, phase3-tasklist.md step 70). `""` (the default,
    # every grant before this step) means workspace-wide, unchanged in meaning. A non-empty
    # scope (`page_type:<value>`) narrows this row to just that page_type — a page_type
    # becomes restricted the moment any such row exists for it in this workspace, at which
    # point a plain workspace-wide role alone stops being sufficient to see it (`auth.py`'s
    # `has_role_for_page`/`visible_page_types`); workspace `admin` always bypasses.
    scope: Mapped[str] = mapped_column(String(64), primary_key=True, default="")
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"))
    # Read-only FUSE-mount access to the wiki export (09 §12, phase3-tasklist.md step 58) —
    # opt-in, orthogonal to `role`: granting it never widens what `role` itself permits, and
    # holding a role (even `admin`) never implies it. False by default for every existing
    # grant, matching 09 §12's "not automatic for every existing reader/contributor."
    fuse_access: Mapped[bool] = mapped_column(default=False)


class Connector(Base):
    """An ingestion connector (02 §3, phase2-tasklist.md step 51) — "just another
    submission source" (03 §2): its polling worker pool (§4/step 52, not yet built) creates
    a normal `raw_source` per discovered item, `submitted_by=connector:<connector_id>`.

    `workspace_id` is fixed at creation and never reassigned (unlike `document_type`) — 09
    §13's permission boundary is "contributor on exactly one workspace, never several,"
    and 05 §7 never lists reassignment as a configurable connector property the way it
    does for document types.

    `type` is a plain string, not a closed enum: 05 §7's own list ("Git repos, websites,
    Confluence, Notion, OpenAPI, etc.") is explicitly open-ended, and no concrete connector
    type is implemented until step 54.

    `config`/`schedule`/`last_sync_cursor` are opaque JSONB, connector-type-specific (09
    §4's "connector-type-specific" cursor shape; `schedule`'s own internal shape — interval
    vs. cron vs. webhook-only — is left to whichever step actually builds the poller, step
    52).

    `credential_ref` is a pointer into the deployment's secrets manager, never a raw secret
    (09 §13: "Connector secrets are never stored in the Metadata DB... any log stream") —
    callers must already hold a ref from their own secrets manager; nothing in this table
    or the `connectors` API ever accepts or stores a raw credential value. Resolving a ref
    into the real secret value happens only at poll time, in-memory, for one run
    (`secrets_manager.py`, phase2-tasklist.md step 53) — never here.
    """

    __tablename__ = "connector"

    connector_id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.workspace_id"), index=True)
    type: Mapped[str] = mapped_column(String(128))
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    credential_ref: Mapped[str | None] = mapped_column(String(512))
    schedule: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 03 §7's auto|gated, same convention as ingestion.check_duplicates' own parameter —
    # a plain string, not an enum, matching that existing precedent.
    ingestion_policy: Mapped[str] = mapped_column(String(16), default="auto")
    state: Mapped[ConnectorState] = mapped_column(
        Enum(ConnectorState, name="connector_state"), default=ConnectorState.enabled
    )
    last_sync_cursor: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Added phase2-tasklist.md step 52: a poll run's outcome (auth ok/failed, items
    # discovered, error message) — 09 §13 frames this as an `ingestion_log` entry, but that
    # table's `source_id`/`to_state` are NOT NULL and shaped for one raw_source's pipeline
    # transitions, which a zero-item or pre-fetch-failure run has neither of. Kept on the
    # connector row instead, alongside `last_run_at`, rather than stretching `ingestion_log`
    # to also mean "connector run diagnostics" — confirmed via AskUserQuestion. Per-item
    # audit is unaffected: a raw_source a connector run *does* create still gets a normal
    # `ingestion_log` entry through the same `_store()` path any submission uses.
    last_run_detail: Mapped[dict] = mapped_column(JSONB, default=dict)
