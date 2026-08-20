# Phase 3 Task List — Completeness

Derived from [07](07-additional-features-and-roadmap.md) §6 (Phase 3: notification service,
feedback loop, content quality scoring, fine-grained access control, analytics, bulk
import/export), sequenced by dependency. Numbering continues from Phase 2 (starts at 57) so a
step number is unambiguous across all three files.

**Also folds in real gaps carried forward from Phase 1 and Phase 2** — track 3a below, found
across two audit passes (2026-08-19): the first a fresh, complete re-read of both prior tasklists
and every "flagged," "accepted gap," "deferred," and "carried forward" note in
`09-implementation-notes.md`; the second a direct implementation-vs-spec review (source code
against `01`-`09`, not just each step's own closing summary against itself) plus a dead-code/
redundancy sweep of `src/karpwiki/`. Every item was flagged explicitly at the time (or found
freshly by the second pass) as a real, accepted simplification, scope boundary, or contract gap —
never silently dropped, but several were never revisited once their own step closed, including
three genuinely foundational ones the first audit pass missed entirely (the real wiki markdown
export, FUSE-mount access, and real `SCHEMA.md` storage — see 3a below) and one contract-shape gap
the second pass found (step 66's pagination gap). Track 3a is sequenced first, and roughly by
dependency within itself, since several later items build cleaner on top of these being real than
on top of the workarounds.

Explicitly **excluded** from this phase (per the roadmap, they're Phase 4 — "pursued based on
actual organizational need, not a fixed timeline," [07](07-additional-features-and-roadmap.md)
§6): the compliance erasure workflow, legal hold, data residency controls, multi-region/DR
topology, and multi-language support.

**Status (2026-08-20): steps 57-66, 70, and 78 done, steps 67-69, 71-77, and 79 not started.**
Step 65 was resolved (not built as a standalone primitive) alongside step 70, both done together
out of numeric sequence, per step 65's own text. Step 78 (track
3f) was found and closed out of numeric sequence — a real gap surfaced live during step 62 prep,
not by either completeness audit pass; see its own entry for why. Phase 1 (steps 1–21) and Phase 2
(steps 22–56) are both complete — see [`phase1-tasklist.md`](phase1-tasklist.md) and
[`phase2-tasklist.md`](phase2-tasklist.md). See [`implementation-audit.md`](implementation-audit.md)
for the full write-up of the two audit passes that shaped track 3a, including redundancy/dead-code
findings that don't need a roadmap step (tracked there instead).

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

    **Done** — [`wiki_export.py`](../src/karpwiki/wiki_export.py) (new): `write`/`delete` mirror
    one page's `/{workspace_id}/wiki/{path}` object using `wiki_page.path` directly (it already
    matches the export layout — no separate mapping needed), called synchronously from
    [`versioning.py`](../src/karpwiki/versioning.py)'s `create_page`/`write_version` (the same
    "compute-on-write, non-transactional" pattern `_write_diff` already uses, `09` §7).
    [`bulk_move.py`](../src/karpwiki/bulk_move.py) also deletes the stale mirror at the old
    workspace prefix after a page move. `SCHEMA.md` is a **placeholder** (confirmed via
    AskUserQuestion) — `schema_ref` is still a bare pointer, not real content, until step 59 — and
    `export_workspace` is a **rebuild-from-DB-truth backfill** (also confirmed via
    AskUserQuestion) for every page written before this step existed, mirroring
    `search.reindex_pending`'s own precedent for the Full-Text Index. `index.md` is deliberately
    not specially handled — nothing creates one yet (step 60); it will export automatically once
    something does. Live-verified against real dev Postgres and the real MinIO-backed S3 object
    store (independently confirmed via a raw `fsspec.find()`, bypassing this codebase's own
    `objectstore.py` wrapper): create/edit/backfill/bulk-move all landed the exact expected files
    at the exact expected paths. See [09](09-implementation-notes.md) §61 for the full writeup.

58. **FUSE-mount access** ([09](09-implementation-notes.md) §12, [08](08-implementation-stack.md)
    §3). Read-only, opt-in per workspace via `access_policy`, mounting only the wiki export
    step 57 builds (never `sources/`, `diffs/`, or `assets/`) — the design was fully decided
    during Phase 1 planning (`phase2-tasklist.md`'s own "0 — Already Decided" table cites it) but
    no tasklist step ever actually built it, and it cannot exist before step 57 does — there is
    nothing to mount otherwise.

    **Done** — `AccessPolicy.fuse_access` (migration `7ab85057a869`), settable via
    [`workspaces.grant`](../src/karpwiki/workspaces.py) and the real
    `POST /workspaces/{id}/access-policy` endpoint. New
    [`wiki_mount.py`](../src/karpwiki/wiki_mount.py): `check_fuse_access` (AuthZ),
    `scoped_filesystem` (a read-only view rooted at exactly `/{workspace_id}/wiki/`, via
    fsspec's `DirFileSystem` wrapped in a `_ReadOnlyFileSystem` that blocks every mutating call —
    `fsspec.fuse`'s own FUSE helper enforces no read-only option itself), and `run_mount`/`main`,
    a real CLI entry point (`python -m karpwiki.wiki_mount`) using `fsspec.fuse.run` and the same
    stdio identity convention `mcp_server.py` already uses. Confirmed via AskUserQuestion:
    actually performing an OS-level mount needs a kernel FUSE driver installed on the host
    (macFUSE/`fuse3`) — out of scope to install here, so `fsspec.fuse` is imported lazily inside
    `run_mount` only, keeping the rest of the module (the real app logic — AuthZ + read-only
    view-scoping) importable and testable without one. Live-verified against real dev Postgres
    (grant/deny both through `workspaces.grant` and the real REST API via nginx) and the real
    MinIO-backed S3 object store (`scoped_filesystem` reading real content, and blocking a real
    write/`rm` attempt before it ever reached the real backend — confirmed the file was
    unchanged after). See [09](09-implementation-notes.md) §62 for the full writeup.

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

    **Done** — new [`schema.py`](../src/karpwiki/schema.py): a `WorkspaceSchema` Pydantic model
    (every field optional — each consumer falls back to its own existing constant when unset,
    same as an injected `None` always has), `parse`/`write`/`rollback`/`history`/`load`, backed
    by a new `SchemaVersion` table (migration `7eb53cee0b95`) versioned like a wiki page but not
    one ("`page_type` not applicable," `01` §7). `Workspace.schema_ref` (a free-text pointer with
    no real content behind it) is replaced by `current_schema_version_id`, a real FK — confirmed
    via AskUserQuestion as a deliberate, small breaking API change; the JSON key stays
    `schema_ref` for API stability, now derived rather than caller-settable. New
    `GET`/`POST /workspaces/{id}/schema`, `GET .../schema/versions`, `POST .../schema/rollback`
    (also confirmed via AskUserQuestion), admin-only like the access-policy endpoints. Rewired
    `ingestion.classify_source` (09 §27's own flagged "needs revisiting" ordering — resolves
    `result.document_type`'s workspace *before* the gate, confirmed via AskUserQuestion),
    `ingestion.check_duplicates`'s `near_duplicate_score`, four `llm.resolve_model` call sites
    (`ingestion.py`×2, `advisor.py`×2 — the classifier call site stays platform-default only,
    since classification is what determines the workspace in the first place), and all five
    `tasks.py` detector wrappers' thresholds. New `ingestion.resolve_ingestion_policy` closes the
    connector-tightening gap — wired at curate time (`tasks.py`'s `_curate`), not inside
    `poll_connector` as the tasklist text above literally says, since that function only ever
    creates a `raw_source` unconditionally and has no gating decision of its own to make; the
    real `auto`/`gated` decision already lives where `check_duplicates` reads it. **A real bug
    caught live, not by the test suite**: `llm.resolve_model`'s `.get(role, {})` only applies its
    default when the key is *absent* — a real parsed schema dict (`schema.as_dict`) sets
    `llm.<role>: None` *explicitly* for every unset role, so the very first real curator call
    against a real per-workspace schema raised `AttributeError` on `None.get("model")`. Fixed in
    `llm.py` (`.get(role) or {}`) and added a regression test using the same explicit-`None`
    shape — none of this module's existing tests had ever exercised it, since they all
    hand-built dicts with keys either fully absent or fully present. Live-verified end to end
    against real dev Postgres, real MinIO, and real `gpt-5-nano` through the rebuilt
    `gateway`/`nginx`/worker containers: a real classification at confidence 0.55 (well under
    the 0.75 platform default) was correctly *accepted* under a workspace-configured
    `min_confidence: 0.01`, and — after the bug fix — a real curator call completed the full
    pipeline to `ingested` and searchable using the fixed `resolve_model` path; a real
    Superseded-Source Detector run correctly flagged a 10-day-old source under a
    workspace-configured 5-day retention window (would not have triggered under the old 180-day
    platform default). Cleaned up the throwaway `live59-*` workspace afterward. See
    [09](09-implementation-notes.md) §63 for the full writeup.

60. **Real `index.md` catalog page** ([01](01-architecture-and-data-model.md) §4,
    [04](04-search-and-retrieval.md) §3). No code has ever materialized an actual per-workspace
    catalog wiki page (one-line summary per page) — `search.py`'s own comment has flagged this as
    an accepted gap since Phase 1 (`phase1-tasklist.md`'s "no `index.md` catalog... carried
    forward" note), and the catalog-match boost `04` §3 specifies is approximated today as a
    `tsvector` weight tier on the `description` frontmatter field instead of a real join against a
    real catalog page. Builds on step 57 — once a real markdown export exists, this is what
    populates its `index.md` file, and this step's own real join-based boost replaces the
    weight-tier approximation.

    **Done** — new [`curate.render_index_body`](../src/karpwiki/curate.py) (pure renderer,
    concepts/entities/sources/comparisons sections) and
    [`ingestion.refresh_index`](../src/karpwiki/ingestion.py), called at the same three points
    `refresh_log` already is (`curate_source`, rollback, bulk-move — anything that can change a
    page's title/description/workspace). `index.md`'s real markdown links make
    `page_links.sync` create real `page_link` rows automatically — the structural fact
    [`search.search`](../src/karpwiki/search.py)'s new catalog-match boost joins against.
    A candidate gets `CATALOG_MATCH_BOOST` (1.3x, this implementation's default — no magnitude is
    specified anywhere in spec/) only when BOTH hold: a real `page_link` from that workspace's
    index.md exists, AND the query matches *that specific candidate's* own title+description text
    — real per-page precision, not a coarse "matches index.md anywhere" check (which would
    indiscriminately boost every catalogued page whenever any one catalog entry matched).
    Scoped to the shared Postgres index only; `dedicated_index.py`'s OpenSearch path keeps its
    existing weight-tier-only ranking, matching 04 §4's own precedent that a dedicated
    workspace's scoring is already an accepted approximation. Live-verified against real dev
    Postgres, real MinIO, and real `gpt-5-nano` through the rebuilt containers: a real ingest
    produced a real `index.md` with real catalog entries organized by category, real `page_link`
    rows from it to every cataloged page (confirmed directly against dev Postgres), and a real
    search query ranked the catalogued, matching page highest. See
    [09](09-implementation-notes.md) §64 for the full writeup.

61. **Structured-data Curator treatment** ([07](07-additional-features-and-roadmap.md) §1). Every
    source is curated as `narrative` today — `curate.py`'s own module docstring has deferred §1.3's
    structure-table + intent-statement + provenance treatment since Phase 1 ("the Curator's
    structured-data treatment is out of scope"). `content_shape`/`artifact_identity`/
    `source_version` are already captured at classification time (`03` §3) and already drive
    duplicate-version detection (`03` §4) — this step is purely the Curator Agent's differentiated
    ingest treatment for a source already tagged `structured_data`.

    **Done** — new [`curate.StructuredCuratedContent`](../src/karpwiki/curate.py)/`StructuredField`/
    `render_structured_source_body` (structure table + intent statement + provenance, reusing
    `CuratedPage` for defined-entity pages so the existing create-or-update-by-exact-title
    machinery applies unchanged) and a new
    [`ingestion.call_structured_curator_model`](../src/karpwiki/ingestion.py)/
    `_write_structured_source_page`. `curate_source` branches on `source.content_shape`: a
    `structured_data` source gets this treatment instead of the narrative
    summary+citations one, both still landing on the same `page_type: source` page and feeding
    the same downstream `pages` create-or-update loop, `overview.md`/`log.md`/`index.md`
    refresh, and reindex dispatch. The intent statement doubles as the page's `description` —
    the exact field step 60's `index.md` already draws every catalog entry from — so 07 §1.1's
    "index.md catalog entry: one-line intent statement... not the filename" falls out for free,
    no new plumbing needed. Live-verified against real dev Postgres, real MinIO, and real
    `gpt-5-nano`: a real JSON config produced a real 7-row structure table with correct
    types/descriptions, a real intent statement, real provenance showing the real
    deterministically-extracted `artifact_identity`/`source_version`, a real defined-entity
    page, and the intent statement correctly appearing as `index.md`'s catalog entry for that
    source. See [09](09-implementation-notes.md) §65 for the full writeup.

62. **Search partial-failure / degraded-result contract** ([09](09-implementation-notes.md) §14).
    `09` §14 specifies that a federated search spanning an unavailable backend "returns 200 with
    the results it has plus `\"partial\": true` and `\"unavailable\": [<workspace_id>, ...]`" —
    but neither `search.py`'s single-backend path nor `dedicated_index.py`'s OpenSearch path is
    wrapped in anything that catches a backend failure; a down dedicated-index workspace during a
    federated query fails the whole request today instead of degrading gracefully. Never flagged
    as an accepted gap when steps 25/26 built federated search — found on a fresh read of `09`
    §14 against the current code, not previously known. This step adds the missing exception
    handling and the `partial`/`unavailable` response fields the contract already specifies.

    **Done** — `api.run_search` (the shared Common Gateway logic both `GET /search` and MCP
    `wiki_search` call) now wraps the shared-Postgres and dedicated-OpenSearch calls in separate
    `try`/`except`: either backend failing degrades that pool to an empty result and records its
    workspace ids as `unavailable`, rather than failing the whole request. A shared-index failure
    also rolls the session back before the `query_log` write that follows — the failed raw-SQL
    statement can otherwise leave the transaction unusable, the same recovery `bulk_move`'s own
    halt-without-rollback batching already used. `partial`/`unavailable` are present in the
    response only when a degradation actually happened, matching `09` §14's "single-workspace
    operations... never carry the field" read as the non-degraded case never carrying it either.
    Live-verified for real, not mocked: stopped the real `opensearch` container mid-session and
    confirmed a real `GET /search` through the real gateway returned a real `200` with the shared
    index's result still present, `"partial": true`, and the exact down dedicated workspace named
    in `"unavailable"` — then restarted `opensearch` and confirmed the very same query fully
    recovered with no gateway restart needed. See [09](09-implementation-notes.md) §67 for the
    full writeup.

63. **Read-time link resolution + cross-workspace AuthZ re-check** ([01](01-architecture-and-data-model.md)
    §3). `GET /pages/{id}` (Phase 2 step 43) returns a page's raw content with embedded
    `page_link` targets unresolved — `01` §3 requires re-checking a reader's access against each
    link's *target* workspace before exposing it as resolved/clickable, which nothing does today.
    Flagged as "no caller exists yet" when `page_link` parsing itself landed (step 28); the caller
    now exists (step 43), so this step closes that flag rather than leaving it stale.

    **Done** — new `api._resolve_page_links`: reads the already-parsed `page_link` rows for a page
    (no markdown re-parsing at read time) and applies the exact same access check
    `_reader_page` itself uses for a direct fetch of each target — `contributor` if the target is
    still `draft`, not just a workspace-level check. A link that fails the check is simply
    **omitted**, not included-but-flagged-inaccessible — `01` §3 says AuthZ is re-checked "before
    resolving," so an unauthorized target's existence is never confirmed to the caller. Wired into
    both `GET /pages/{id}` and the MCP `wiki_get_page` tool (new `"links"` response field on
    both — an agent following a citation via MCP sees the same resolved, AuthZ-checked list a REST
    client does), leaving the raw `content` field's embedded markdown link syntax untouched (the
    stored document verbatim). Live-verified against real dev Postgres through the rebuilt
    gateway: seeded two real workspaces with a real cross-workspace link where the caller
    initially had no access to the target — confirmed a real `GET /pages/{id}` returned
    `"links": []` while the raw content still showed the written link text, then granted real
    `reader` access and confirmed the identical link resolved on the next real request, no
    restart needed. See [09](09-implementation-notes.md) §68 for the full writeup.

64. **Stuck-pipeline sweep detector** ([05](05-admin-backend-and-maintenance.md) §2-3). **Done.**
    Only `submitted`/`classified`/`ingesting` can ever be observed persisted in a stuck resting
    state — traced from the real transaction boundaries (`tasks._classify`/`_curate` each wrap
    their whole unit of work in one `session_scope()`, so `classifying`/`duplicate_check` only
    ever exist mid-transaction and a crash there always rolls back, confirming step 33's own live
    kill-test finding). Retry needs zero new pipeline-transition edges — re-dispatching
    `classify_source`/`curate_source` per source's own recorded state is always safe from these
    three resting points. Abort needs exactly one new, precedented edge set: `pipeline.py`'s
    `ABORTABLE_IF_STUCK` gives each of the three a direct `-> rejected` transition, folded in the
    same loop pattern already used for `FAILABLE -> error`. New `ReviewKind.stuck` (migration
    `7cc67060951f`); a global sweep, not per-workspace like the other five detectors — a
    `submitted`-stuck source has no workspace yet, so `find_stuck_sources`/
    `run_stuck_pipeline_detector`/`tasks.detect_stuck_pipelines` (own hourly beat entry,
    `KARPWIKI_MAINTENANCE_STUCK_PIPELINE_INTERVAL_HOURS`) scan and raise one workspace-less item
    per run, the same shape `submission`/`classification` items already use. Detection threshold
    (`KARPWIKI_STUCK_PIPELINE_THRESHOLD_HOURS`, default 1h) sits ~6x above
    `CELERY_VISIBILITY_TIMEOUT_SECONDS` so a genuine crash's own automatic redelivery gets first
    chance to self-heal. `retry`/`abort`/`dismiss` resolution: `advisor.resolve_stuck` stays
    bookkeeping-only (same circular-import reasoning as `resolve_reindex`/`resolve_prune`);
    retry's re-dispatch happens in `api.run_resolve_review_item` post-commit; abort's real
    transition happens in `ingestion.resolve_review_item`'s new branch, by reusing the existing
    `reject_source` per stuck source rather than re-deriving its logic. Live-verified against the
    real dev stack: applied the migration, rebuilt/restarted the gateway and all three affected
    workers plus celery-beat, confirmed the new beat entry registered live; seeded a real
    `submitted` source backdated 3 hours, ran the detector task in the live maintenance worker
    (raised a real workspace-less review item), resolved it `retry` through the real gateway —
    `worker-classification`'s own logs confirmed it genuinely picked up the re-dispatched task off
    the real broker; seeded a second `classified` source in a real throwaway workspace, resolved
    it `abort` through the same live endpoint — confirmed both status axes flipped to `rejected`
    and the placeholder page was rewritten. See [09](09-implementation-notes.md) §69 for the full
    writeup, including the widened `pipeline.py` transition and the rewritten
    `test_placeholder.py` case it required.

65. **Real cross-workspace / global-admin grant primitive, or an explicit decision not to build
    one** ([06](06-api-mcp-and-scaling.md) §3). **Done — resolved: not needed.** Built alongside
    step 70 as planned; building the real fine-grained feature confirmed rather than just
    re-asserted `09` §22's original reasoning — page_type scoping is a pure narrowing of what a
    workspace-level role already covers, every grant (scoped or not) stays
    `(workspace_id, principal, scope)`, never crossing a workspace boundary. "Global admin across
    all workspaces" still has no concrete caller three phases in; `any_workspace_with_role`'s
    "admin in at least one workspace" workaround remains sufficient for every workspace-less case
    that exists. Formally closed rather than carried forward again. See
    [09](09-implementation-notes.md) §70 for the full writeup.

66. **API pagination-contract gap** ([09](09-implementation-notes.md) §14). **Done — resolved:
    documented as deliberately unpaginated, not extended to real cursor pagination.** Checked
    directly with Deepak before choosing: none of the four affected tables (`DocumentType`,
    `Connector`, `Workspace`, `AccessPolicy`) has a `created_at` column at all, so "extend cursor
    pagination" would have meant four new migrations, not just reusing the existing pattern —
    weighed against confirmation that none of these lists is expected to reach hundreds of rows
    (deployment-configuration cardinality — workspace/connector/taxonomy/grant counts — not
    append-heavy content), the capped-limit-only path matching `/search`'s own precedent won.
    Each underlying list function gained a `limit: int = DEFAULT_LIST_LIMIT` parameter (same
    `pagination.py` constants every cursor-paginated endpoint already clamps against), with no
    `next_cursor` in the response, ever — `GET /document-types`, `GET /connectors`,
    `GET /workspaces`, `GET /workspaces/{id}/access-policy`, and the MCP `wiki_list_workspaces`
    tool. `09` §14's own pagination-contract paragraph now names this exception explicitly
    (alongside `/search`'s pre-existing one) rather than silently falling short of it. 696 tests
    green (10 new). Live-verified against the real dev stack: seeded a real workspace with 2
    document types, 2 connectors, and 3 access-policy grants — confirmed `?limit=1` capped all
    four live endpoints to exactly 1 item with no `next_cursor` key, and the unlimited default
    call returned every row. See [09](09-implementation-notes.md) §71 for the full writeup.

## 3b — Notification Service, Feedback Loop, Content Quality ([07](07-additional-features-and-roadmap.md) §3-4)

67. **Real Notification Service delivery.** Step 55 (Phase 2) already built the pluggable
    `NotificationSink` interface and its one real hook (connector auth failure); this step adds a
    second, real implementation (email and/or chat-platform webhook) swapped in via
    `default_notification_sink()` with no change to any caller — the same swap-with-no-handler-
    changes property `Authenticator`/`SecretResolver` already proved out. Two new trigger points
    beyond the connector hook: admin notification on new/aging review items and SLA breaches
    (`monitoring.py`'s already-computed `open_items_past_sla`/`p95_breaches_sla`, step 44 —
    currently dashboard-only/pull-based; this step makes them push-based for the first time), and
    submitter notification when their own document is ingested, rejected, or merged as a
    duplicate.

68. **Search result feedback loop** ([04](04-search-and-retrieval.md) §3-4,
    [09](09-implementation-notes.md) §10). Thumbs-up/down (or similar) per search result, recorded
    alongside `query_log` (`02` §5). `09` §10 already designates this as the platform's
    relevance-regression signal — the catalog-match boost's own magnitude (step 17, Phase 1) was
    "tuned blind... an unspecified constant. Acceptable at this scale," per `phase1-tasklist.md`'s
    own accepted-gap note, precisely because this signal didn't exist yet. Persistently low-rated
    pages for a topic also feed the Maintenance Advisor's staleness/contradiction detectors
    (`05` §2) with a real "this isn't serving readers" signal neither currently has.

69. **Content quality scoring.** Curator Agent lint-pass scoring (citation density,
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

70. **Per-page-type / per-tag permissions within a workspace** ([06](06-api-mcp-and-scaling.md)
    §3). **Done.** `page_type` scoping built for real; `tag` scoping named as a deliberate,
    documented deferred gap (confirmed via AskUserQuestion) — `page_type` is a stable column
    already on `WikiPage`, free at every call site; `tags` live in per-version frontmatter JSONB
    and would force a join onto several currently-simple endpoints for a dimension `07` §2 itself
    treats as optional between the two. `access_policy` gained a `scope` column (migration
    `88ee7671b581`, PK widened to `(workspace_id, principal, scope)`) rather than a new table —
    `scope=""` means exactly what every grant meant before this step, backward-compatible by
    construction. Enforcement is opt-in: a `page_type` becomes restricted the moment any scoped
    grant exists for it in a workspace, at which point the plain workspace role alone is no longer
    enough — the principal needs its own matching scoped grant; workspace `admin` always bypasses.
    New `auth.has_role_for_page` (single-page checks: `GET /pages/{id}`, link resolution) and
    `auth.visible_page_types` (list/search checks, batched to two queries regardless of result-set
    size — no N+1). `GET /pages`/`wiki_list_pages` intersect the filter at the query level
    (preserves exact cursor-pagination correctness); `GET /search` post-filters results per
    distinct workspace instead, since one federated call spans many workspaces each with
    independent restriction config. Grant/revoke gained an optional `page_type`, orthogonal to
    `fuse_access` the same way `fuse_access` is orthogonal to `role`. 686 tests green (28 new: a
    new `test_auth.py` unit-testing the two new pure functions directly, plus REST/MCP enforcement
    coverage across four existing test files). Live-verified against the real dev stack: seeded a
    real workspace with a real `concept` and a real `entity` page, a plain admin, a plain reader —
    granted a real scoped grant to a *different* principal through the live API and watched the
    reader immediately lose both list and direct-fetch access to the entity page while the admin's
    access stayed intact (bypass); granted the reader the same scope and watched access return, no
    gateway restart anywhere in the cycle; revoked just the scoped grant and confirmed an explicit
    `?page_type=entity` filter correctly returned empty (not "no filter") while the workspace-wide
    grant stayed untouched. Also resolved step 65's global-admin question as part of this design,
    per this step's own instruction — see that step's own entry. See
    [09](09-implementation-notes.md) §70 for the full writeup.

71. **PII detection at ingestion.** Classifier (or a dedicated scanner) flags sources containing
    PII; a new `pii_review` review-item kind blocks ingestion until an admin clears it, mirroring
    the existing `duplicate`/`classification` review-item shapes (`03` §3-4) rather than inventing
    a new resolution model.

## 3d — Platform Operations: Analytics, Bulk Import/Export, Templates ([07](07-additional-features-and-roadmap.md) §5)

72. **Storage/usage trend data** ([05](05-admin-backend-and-maintenance.md) §8). A time-series
    mechanism for the "with trend" half of the Storage Utilization dashboard `monitoring.py`
    already built (step 44) but left `None` for lack of one — "no time-series mechanism exists
    anywhere in this codebase," `09` §47's own accepted-gap note, documented rather than faked at
    the time. The minimal real prerequisite for step 73 to have actual historical data to show,
    not just a point-in-time snapshot re-labeled as a trend.

73. **Analytics dashboards.** Usage trends over time (search volume, submission volume, active
    workspaces) — building on step 72's trend data and the feedback signal from step 68.

74. **Bulk import/export.** Admin tooling to seed a new workspace from an existing document
    repository (bulk submission, bypassing per-document review-item noise but still subject to
    real classification/dedup — not a side channel around them), and to export a workspace's wiki
    + sources for migration/backup. The export half should reuse step 57's real wiki markdown
    mirror rather than build a second, parallel export mechanism — `02` §2 already names
    "backup/migration/export" as that mirror's own first purpose.

75. **Workspace templates.** Predefined `SCHEMA.md` templates for common document-type categories
    (e.g. "Policy workspace," "Engineering docs workspace") to bootstrap a new workspace with
    sensible taxonomy/thresholds instead of a blank one. Depends on step 59 — there is no real
    `SCHEMA.md` to template until then.

## 3e — Operational Hardening

Lighter-weight than the tracks above, and explicitly optional per the spec's own framing for both
items — included so they're planned rather than perpetually deferred, not because Phase 3 can't
close without them.

76. **Optional read-through cache layer** ([02](02-storage-and-indexing.md) §6). Page/search-result
    caching, keyed by `(workspace_id, page_id, version_id)` or `(workspace_id, query_hash)` so a
    new page version or reindex naturally invalidates stale entries with no explicit cache-busting
    logic. "Not required for correctness — purely a latency optimization," per `02` §6's own
    wording; closes the "cache hit rate: `None`, no cache layer exists" accepted gap `monitoring.py`
    (step 44) documented rather than faked.

77. **Backup & disaster recovery procedures.** Periodic snapshots of the Metadata DB and object
    store, with a documented point-in-time restore; scoped per-workspace given the storage
    partitioning Phase 2 already made real (`06` §4, steps 30–35). For wiki *content* specifically,
    this can lean on step 57's real markdown export (already framed as a backup/migration
    mechanism, `02` §2) rather than a from-scratch procedure; the Metadata DB and object store
    still need their own snapshot/restore story beyond that. Lighter-weight than, and explicitly
    distinct from, Phase 4's full multi-region/DR topology — this is backup/restore procedure, not
    a second active region.

## 3f — Ingestion Format Coverage

78. **Binary document format ingestion (PDF, DOCX)** ([03](03-ingestion-and-review-workflows.md)
    §3, [01](01-architecture-and-data-model.md) §6's PDF citation-page-number convention). Found
    live during step 62 prep, in response to a direct question about the most common enterprise
    document formats (DOCX, PDF, CSV, TXT) — not part of either completeness audit pass, since
    nothing in `phase1-tasklist.md`/`phase2-tasklist.md`/`09-implementation-notes.md` had ever
    flagged it as an accepted gap. CSV and TXT already worked (plain text, decode cleanly), but no
    code anywhere extracted real text from PDF or DOCX: every text-producing call in the ingest
    path used a bare `payload.decode("utf-8", errors="replace")`, which for a binary PDF/DOCX
    never raises — it silently substitutes most of the file with placeholder characters and feeds
    the garbled result to the Classifier/Curator LLM calls, rather than extracting real text or
    failing loudly. `connectors_git.py` already had the right instinct for this exact class of
    problem (skips a file that fails UTF-8 decode rather than submitting it) but that guard was
    never applied to the direct `POST /sources` upload path.

    **Done** — new [`doc_extract.py`](../src/karpwiki/doc_extract.py): content-based (magic-byte,
    not just extension) PDF/DOCX detection and real text extraction (`pypdf`/`python-docx`),
    explicitly scoped to modern DOCX only (legacy `.doc`'s OLE2/CFB binary format needs a
    different library entirely and is not "the most common" format today — a documented
    boundary, not a silent gap). `classify.detect_content_shape` and all three of
    `ingestion.py`'s `payload.decode(...)` call sites now route through
    `doc_extract.extract_text`. `ingestion.store` — the one shared entry point every submission
    goes through — rejects content this can't read at all (`UnsupportedContentError`) before a
    `raw_source` ever exists, extended to a real `400` in `api.py`'s `POST /sources` handler and a
    skip-this-item-not-the-whole-run guard in `connector_polling.poll_connector` (defense in depth
    for a future adapter type that, unlike `connectors_git.py`, doesn't already filter these out).
    Live-verified against real dev Postgres and real `gpt-5-nano` through the rebuilt containers: a
    real DOCX runbook and a real PDF both produced genuinely coherent, accurate classifier
    summaries (proving clean extraction, not garbled placeholder text) and the DOCX ran the full
    pipeline through to `ingested`; a real unsupported binary (PNG magic bytes) was correctly
    rejected with a real `400` at the real REST API. See [09](09-implementation-notes.md) §66 for
    the full writeup.

79. **Verify**: Phase 3 exit criteria, matching `07` §6's own stated goal — "Admin staff can run
    the Platform without manual intervention outside the review queue." Demonstrate end to end: a
    real threshold breach fires a real notification (step 67) with no admin polling a dashboard
    for it; a real low-feedback page surfaces to the Maintenance Advisor (step 68) with no manual
    sweep; a per-tag-scoped reader (step 70) is correctly restricted through both the real REST and
    MCP surfaces, not just one of them; a real FUSE mount (step 58) shows a real, current
    `index.md` (step 60) an agent can read directly, no gateway round trip — the platform's own
    Karpathy-pattern premise, demonstrated for real for the first time.

## Exit Criteria

Matches [07](07-additional-features-and-roadmap.md) §6's own stated Phase 3 goal: "Admin staff can
run the Platform without manual intervention outside the review queue."

| Requirement | Closed by |
|---|---|
| Foundational gaps carried forward from Phase 1/2, closed rather than left as permanent surprises | Steps 57–66 |
| Notification Service, real delivery | Step 67 |
| Search feedback loop / relevance-regression signal | Step 68 |
| Content quality scoring | Step 69 |
| Fine-grained access control | Steps 70–71 |
| Analytics dashboards | Steps 72–73 |
| Bulk import/export, workspace templates | Steps 74–75 |
| Operational hardening (cache, backup/DR) | Steps 76–77 |
| Binary document format ingestion (PDF, DOCX) | Step 78 |

---
Previous: [phase2-tasklist.md](phase2-tasklist.md) · Back to: [00-overview.md](00-overview.md)
