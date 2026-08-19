# Phase 3 Task List — Completeness

Derived from [07](07-additional-features-and-roadmap.md) §6 (Phase 3: notification service,
feedback loop, content quality scoring, fine-grained access control, analytics, bulk
import/export), sequenced by dependency. Numbering continues from Phase 2 (starts at 57) so a
step number is unambiguous across all three files.

**Also folds in real gaps carried forward from Phase 1 and Phase 2** — track 3a below, found by a
fresh, complete re-read of both prior tasklists and every "flagged," "accepted gap," "deferred,"
and "carried forward" note in `09-implementation-notes.md` (not just the ones each step's own
closing summary already restated). Every one was flagged explicitly at the time as a deliberate,
accepted simplification or scope boundary — never silently dropped — but several were never
revisited once their own step closed, including three genuinely foundational ones a first pass at
this file missed entirely (the real wiki markdown export, FUSE-mount access, and real `SCHEMA.md`
storage — see 3a below). Track 3a is sequenced first, and roughly by dependency within itself,
since several later items build cleaner on top of these being real than on top of the workarounds.

Explicitly **excluded** from this phase (per the roadmap, they're Phase 4 — "pursued based on
actual organizational need, not a fixed timeline," [07](07-additional-features-and-roadmap.md)
§6): the compliance erasure workflow, legal hold, data residency controls, multi-region/DR
topology, and multi-language support.

**Status (2026-08-19): not started.** Phase 1 (steps 1–21) and Phase 2 (steps 22–56) are both
complete — see [`phase1-tasklist.md`](phase1-tasklist.md) and
[`phase2-tasklist.md`](phase2-tasklist.md). This file is a plan, not yet implementation; nothing
below has a "Done" marker yet.

## 3a — Carried-Forward Foundational Gaps (Phase 1/2 debt)

57. **Real wiki markdown export to the Object Store** ([01](01-architecture-and-data-model.md)
    §1, [02](02-storage-and-indexing.md) §2). `01` §1's own architecture diagram lists "wiki
    markdown export" as one of exactly three things the Object Store holds (alongside raw sources
    and assets), and `02` §2 specifies it precisely: a read-only, regenerated mirror at
    `/{workspace_id}/wiki/...` — `overview.md`, `index.md`, `log.md`, `concepts/*.md`,
    `entities/*.md`, `sources/*.md`, `comparisons/*.md`, `SCHEMA.md` — written whenever
    `wiki_page.current_version_id` changes, giving each workspace the same `raw/`, `wiki/`,
    `schema` directory shape Karpathy's original pattern uses. **No code writes any of this
    today** — every `objectstore.write_bytes` call in the codebase is for raw-source staging or
    page-version diffs, never a `/{workspace_id}/wiki/` path. This was never flagged as an
    accepted gap anywhere in `phase1-tasklist.md`/`phase2-tasklist.md`, unlike the smaller,
    already-tracked ones below — it's the literal file-based form of the platform's own name
    ("Karpathy Wiki for Agentic AI": an agent reading `index.md` off a real filesystem) and was
    simply never built, DB-backed wiki pages standing in for it throughout Phase 1 and 2. This
    step is the write-through (or short-delay async) exporter itself; step 58 depends on it.

58. **FUSE-mount access** ([09](09-implementation-notes.md) §12, [08](08-implementation-stack.md)
    §3). Read-only, opt-in per workspace via `access_policy`, mounting only the wiki export
    step 57 builds (never `sources/`, `diffs/`, or `assets/`) — the design was fully decided
    during Phase 1 planning (`phase2-tasklist.md`'s own "0 — Already Decided" table cites it) but
    no tasklist step ever actually built it, and it cannot exist before step 57 does — there is
    nothing to mount otherwise.

59. **Real `SCHEMA.md` storage and parsing** ([09](09-implementation-notes.md) §26). `05` §7
    lists "edit SCHEMA.md" as a workspace-lifecycle action, and `01` §7 frames it as "versioned
    like a wiki page," but `schema_ref` has only ever been a plain pointer string — no code loads,
    parses, validates, or versions real `SCHEMA.md` content, and `llm.resolve_model`'s own
    `schema: dict | None` parameter has never been passed anything but `None`. Every per-workspace
    threshold this was supposed to source (classification confidence, near-duplicate score,
    staleness/orphan lookback windows, LLM model overrides) has instead stayed a hardcoded Python
    default throughout Phase 1 and 2, `09` §26's own words: "a self-contained feature on the scale
    of a track of its own, not a side effect." This step is that: parsing, validation, versioning
    through the existing page-version machinery, and rewiring `classify.py`/`dedup.py`/
    `advisor.py`/`llm.py` to read live overrides. **Closes a second, smaller flagged gap as a
    direct consequence**: `09` §13 says a connector's own `ingestion_policy` "may only tighten"
    its target workspace's policy, "never relax" it — but no workspace-level `ingestion_policy`
    has ever been a real, stored value to compare against (it only ever appears as a `SCHEMA.md`
    template field, `09` §6), so the rule has been unenforceable since connectors shipped in
    Phase 2. Once a workspace's `SCHEMA.md` is real, wire that comparison into
    `connector_polling.poll_connector` as part of this same step.

60. **Real `index.md` catalog page** ([01](01-architecture-and-data-model.md) §4,
    [04](04-search-and-retrieval.md) §3). No code has ever materialized an actual per-workspace
    catalog wiki page (one-line summary per page) — `search.py`'s own comment has flagged this as
    an accepted gap since Phase 1 (`phase1-tasklist.md`'s "no `index.md` catalog... carried
    forward" note), and the catalog-match boost `04` §3 specifies is approximated today as a
    `tsvector` weight tier on the `description` frontmatter field instead of a real join against a
    real catalog page. Builds on step 57 — once a real markdown export exists, this is what
    populates its `index.md` file, and this step's own real join-based boost replaces the
    weight-tier approximation.

61. **Structured-data Curator treatment** ([07](07-additional-features-and-roadmap.md) §1). Every
    source is curated as `narrative` today — `curate.py`'s own module docstring has deferred §1.3's
    structure-table + intent-statement + provenance treatment since Phase 1 ("the Curator's
    structured-data treatment is out of scope"). `content_shape`/`artifact_identity`/
    `source_version` are already captured at classification time (`03` §3) and already drive
    duplicate-version detection (`03` §4) — this step is purely the Curator Agent's differentiated
    ingest treatment for a source already tagged `structured_data`.

62. **Search partial-failure / degraded-result contract** ([09](09-implementation-notes.md) §14).
    `09` §14 specifies that a federated search spanning an unavailable backend "returns 200 with
    the results it has plus `\"partial\": true` and `\"unavailable\": [<workspace_id>, ...]`" —
    but neither `search.py`'s single-backend path nor `dedicated_index.py`'s OpenSearch path is
    wrapped in anything that catches a backend failure; a down dedicated-index workspace during a
    federated query fails the whole request today instead of degrading gracefully. Never flagged
    as an accepted gap when steps 25/26 built federated search — found on a fresh read of `09`
    §14 against the current code, not previously known. This step adds the missing exception
    handling and the `partial`/`unavailable` response fields the contract already specifies.

63. **Read-time link resolution + cross-workspace AuthZ re-check** ([01](01-architecture-and-data-model.md)
    §3). `GET /pages/{id}` (Phase 2 step 43) returns a page's raw content with embedded
    `page_link` targets unresolved — `01` §3 requires re-checking a reader's access against each
    link's *target* workspace before exposing it as resolved/clickable, which nothing does today.
    Flagged as "no caller exists yet" when `page_link` parsing itself landed (step 28); the caller
    now exists (step 43), so this step closes that flag rather than leaving it stale.

64. **Stuck-pipeline sweep detector** ([05](05-admin-backend-and-maintenance.md) §2-3). A source
    stuck mid-pipeline with genuinely no task ever redelivered for it (a broker message lost
    before it was ever recorded unacked, or a permanent single-attempt exception that isn't a
    transient LLM-call failure) has no detector today. Explicitly named "Maintenance Advisor
    territory (track 2c), not this step's" when step 33 flagged it, but never built among the five
    real track-2c detectors (steps 36–40) — the sixth one this step adds, same `reindex`-review-item
    shape the existing five already establish.

65. **Real cross-workspace / global-admin grant primitive, or an explicit decision not to build
    one** ([06](06-api-mcp-and-scaling.md) §3). `06` §3's own principal table names "global admin
    across all workspaces" as distinct from per-workspace admin, but no `access_policy` row shape
    represents it — every workspace-less admin check (submission/classification review items,
    `POST /workspaces`'s own bootstrap check) still uses the "admin in at least one workspace"
    workaround `09` §22 built deliberately rather than invent the primitive speculatively. Resolve
    alongside step 69 (fine-grained access control) rather than before it, since that's the first
    feature that might actually need a real answer — but resolve it, one way or the other, rather
    than leaving the flag open a third phase running.

## 3b — Notification Service, Feedback Loop, Content Quality ([07](07-additional-features-and-roadmap.md) §3-4)

66. **Real Notification Service delivery.** Step 55 (Phase 2) already built the pluggable
    `NotificationSink` interface and its one real hook (connector auth failure); this step adds a
    second, real implementation (email and/or chat-platform webhook) swapped in via
    `default_notification_sink()` with no change to any caller — the same swap-with-no-handler-
    changes property `Authenticator`/`SecretResolver` already proved out. Two new trigger points
    beyond the connector hook: admin notification on new/aging review items and SLA breaches
    (`monitoring.py`'s already-computed `open_items_past_sla`/`p95_breaches_sla`, step 44 —
    currently dashboard-only/pull-based; this step makes them push-based for the first time), and
    submitter notification when their own document is ingested, rejected, or merged as a
    duplicate.

67. **Search result feedback loop** ([04](04-search-and-retrieval.md) §3-4,
    [09](09-implementation-notes.md) §10). Thumbs-up/down (or similar) per search result, recorded
    alongside `query_log` (`02` §5). `09` §10 already designates this as the platform's
    relevance-regression signal — the catalog-match boost's own magnitude (step 17, Phase 1) was
    "tuned blind... an unspecified constant. Acceptable at this scale," per `phase1-tasklist.md`'s
    own accepted-gap note, precisely because this signal didn't exist yet. Persistently low-rated
    pages for a topic also feed the Maintenance Advisor's staleness/contradiction detectors
    (`05` §2) with a real "this isn't serving readers" signal neither currently has.

68. **Content quality scoring.** Curator Agent lint-pass scoring (citation density,
    cross-reference completeness, freshness) on ingest; surfaced as a sortable Admin Console
    column; used by the Maintenance Advisor to prioritize lint/reindex work ahead of the
    recency-only signal it uses today (step 36). **Closes another named-but-unbuilt gap as a
    direct consequence**: `02` §5 names `lint_log` as one of `log.md`'s four source streams
    (alongside `ingestion_log`, `query_log`, `admin_action_log`), but it was deliberately left
    unbuilt through all of track 2c "for a stream nothing currently reads" — `09`'s own words,
    resolved via AskUserQuestion at the time as correct *because* no lint pass existed yet. This
    step is that lint pass; write to `lint_log` as part of it rather than leaving the stream
    permanently named-but-empty.

## 3c — Fine-Grained Access Control ([07](07-additional-features-and-roadmap.md) §2)

69. **Per-page-type / per-tag permissions within a workspace** ([06](06-api-mcp-and-scaling.md)
    §3). Extends the baseline `reader`/`contributor`/`admin` roles to scope by `page_type` or tag
    (e.g. a "Legal" sub-area of a workspace visible only to a subset of readers). Resolve step 65's
    global-admin question as part of this design, not before it — this is the first feature where
    the answer might actually matter.

70. **PII detection at ingestion.** Classifier (or a dedicated scanner) flags sources containing
    PII; a new `pii_review` review-item kind blocks ingestion until an admin clears it, mirroring
    the existing `duplicate`/`classification` review-item shapes (`03` §3-4) rather than inventing
    a new resolution model.

## 3d — Platform Operations: Analytics, Bulk Import/Export, Templates ([07](07-additional-features-and-roadmap.md) §5)

71. **Storage/usage trend data** ([05](05-admin-backend-and-maintenance.md) §8). A time-series
    mechanism for the "with trend" half of the Storage Utilization dashboard `monitoring.py`
    already built (step 44) but left `None` for lack of one — "no time-series mechanism exists
    anywhere in this codebase," `09` §47's own accepted-gap note, documented rather than faked at
    the time. The minimal real prerequisite for step 72 to have actual historical data to show,
    not just a point-in-time snapshot re-labeled as a trend.

72. **Analytics dashboards.** Usage trends over time (search volume, submission volume, active
    workspaces) — building on step 71's trend data and the feedback signal from step 67.

73. **Bulk import/export.** Admin tooling to seed a new workspace from an existing document
    repository (bulk submission, bypassing per-document review-item noise but still subject to
    real classification/dedup — not a side channel around them), and to export a workspace's wiki
    + sources for migration/backup. The export half should reuse step 57's real wiki markdown
    mirror rather than build a second, parallel export mechanism — `02` §2 already names
    "backup/migration/export" as that mirror's own first purpose.

74. **Workspace templates.** Predefined `SCHEMA.md` templates for common document-type categories
    (e.g. "Policy workspace," "Engineering docs workspace") to bootstrap a new workspace with
    sensible taxonomy/thresholds instead of a blank one. Depends on step 59 — there is no real
    `SCHEMA.md` to template until then.

## 3e — Operational Hardening

Lighter-weight than the tracks above, and explicitly optional per the spec's own framing for both
items — included so they're planned rather than perpetually deferred, not because Phase 3 can't
close without them.

75. **Optional read-through cache layer** ([02](02-storage-and-indexing.md) §6). Page/search-result
    caching, keyed by `(workspace_id, page_id, version_id)` or `(workspace_id, query_hash)` so a
    new page version or reindex naturally invalidates stale entries with no explicit cache-busting
    logic. "Not required for correctness — purely a latency optimization," per `02` §6's own
    wording; closes the "cache hit rate: `None`, no cache layer exists" accepted gap `monitoring.py`
    (step 44) documented rather than faked.

76. **Backup & disaster recovery procedures.** Periodic snapshots of the Metadata DB and object
    store, with a documented point-in-time restore; scoped per-workspace given the storage
    partitioning Phase 2 already made real (`06` §4, steps 30–35). For wiki *content* specifically,
    this can lean on step 57's real markdown export (already framed as a backup/migration
    mechanism, `02` §2) rather than a from-scratch procedure; the Metadata DB and object store
    still need their own snapshot/restore story beyond that. Lighter-weight than, and explicitly
    distinct from, Phase 4's full multi-region/DR topology — this is backup/restore procedure, not
    a second active region.

77. **Verify**: Phase 3 exit criteria, matching `07` §6's own stated goal — "Admin staff can run
    the Platform without manual intervention outside the review queue." Demonstrate end to end: a
    real threshold breach fires a real notification (step 66) with no admin polling a dashboard
    for it; a real low-feedback page surfaces to the Maintenance Advisor (step 67) with no manual
    sweep; a per-tag-scoped reader (step 69) is correctly restricted through both the real REST and
    MCP surfaces, not just one of them; a real FUSE mount (step 58) shows a real, current
    `index.md` (step 60) an agent can read directly, no gateway round trip — the platform's own
    Karpathy-pattern premise, demonstrated for real for the first time.

## Exit Criteria

Matches [07](07-additional-features-and-roadmap.md) §6's own stated Phase 3 goal: "Admin staff can
run the Platform without manual intervention outside the review queue."

| Requirement | Closed by |
|---|---|
| Foundational gaps carried forward from Phase 1/2, closed rather than left as permanent surprises | Steps 57–65 |
| Notification Service, real delivery | Step 66 |
| Search feedback loop / relevance-regression signal | Step 67 |
| Content quality scoring | Step 68 |
| Fine-grained access control | Steps 69–70 |
| Analytics dashboards | Steps 71–72 |
| Bulk import/export, workspace templates | Steps 73–74 |
| Operational hardening (cache, backup/DR) | Steps 75–76 |

---
Previous: [phase2-tasklist.md](phase2-tasklist.md) · Back to: [00-overview.md](00-overview.md)
