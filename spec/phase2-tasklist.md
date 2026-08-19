# Phase 2 Task List — Enterprise Core

Derived from [07](07-additional-features-and-roadmap.md) §6 (Phase 2: multi-workspace routing,
common gateway, full storage/indexing lifecycle, Maintenance Advisor, API+MCP, horizontal
scaling), sequenced by dependency. Unlike Phase 1's exit criteria, Phase 2's is strict: **all**
requirements in [00](00-overview.md) §7's traceability table must be met — this file's five
sub-phases are organized so each row in that table maps to a specific step, checked at the end.

Explicitly **excluded** from this phase (per the roadmap, they're Phase 3+): the Notification
Service's full delivery mechanics, the search feedback loop, content quality scoring,
multi-language support, fine-grained (per-page-type) access control, analytics dashboards, bulk
import/export, compliance erasure/legal hold, data residency, and multi-region/DR topology.

**Status (2026-08-19): steps 22–51 done, tracks 2a, 2b, 2c, and 2d all complete and closed out;
track 2e (Connector Framework) in progress.** Phase 1 (steps 1–21,
[`phase1-tasklist.md`](phase1-tasklist.md)) is complete; implementation continues in
[`src/karpwiki/`](../src/karpwiki/). Numbering continues from Phase 1 (starts at 22) so a step
number is unambiguous across both files.

## 0 — Already Decided

Most of Phase 2's hard design questions were resolved during Phase 1 planning, as
implementation-readiness notes with no Phase-1 code to attach to yet. **Don't re-litigate these**:

| Topic | Decision lives in |
|---|---|
| Connector execution model (poll → diff cursor → submit as `raw_source`) | [09](09-implementation-notes.md) §4 |
| MCP on-behalf-of delegation (dual-identity check) | [09](09-implementation-notes.md) §5 |
| `query_log` retention (90 days) and threshold lookback windows | [09](09-implementation-notes.md) §8 |
| Taxonomy bulk-move execution (dry-run + batched, no rollback of completed batches) | [09](09-implementation-notes.md) §11 |
| FUSE-mount access scope (read-only, opt-in, wiki export only) | [09](09-implementation-notes.md) §12 |
| Connector credential storage, rotation, and permission boundary | [09](09-implementation-notes.md) §13 |

**Still open — resolve when the step is reached**, not before: the `document_type` table's exact
migration path off `Workspace.document_types`' array column (step 22); dedicated-per-workspace-
index selection criteria beyond [06](06-api-mcp-and-scaling.md) §6's illustrative page-count range
(step 26). The Contradiction Detector's Curator lint-pass design (step 40) is resolved —
[09](09-implementation-notes.md) §43. The auth *library* is not open —
[08](08-implementation-stack.md) §2 already picks Authlib (OIDC/SAML) + PyJWT (API keys); step 47
is a real Authenticator implementation using that pick, not a library search.

## 2a — Multi-Workspace & Taxonomy Routing

22. Promote `document_types` into a real `document_type` table (`type_code`, `workspace_id`,
    `description` — [02](02-storage-and-indexing.md) §3) with admin CRUD
    (`document-types` resource, [06](06-api-mcp-and-scaling.md) §1) — replaces the array column on
    `Workspace` that Phase 1's [`models.py`](../src/karpwiki/models.py) marks as its simplification.
    **Done** — [`document_types.py`](../src/karpwiki/document_types.py) (CRUD),
    [`api.py`](../src/karpwiki/api.py) (`GET`/`POST /document-types`,
    `POST`/`DELETE /document-types/{type_code}`), migration `e22a1b4c3004` (backfills existing
    array data into the new table before dropping the column — verified against a throwaway
    database with real data, not just an empty one). `classify_source`/`resolve_classification`
    in [`ingestion.py`](../src/karpwiki/ingestion.py) now read the table instead of the array —
    see [09](09-implementation-notes.md) §25 for why `type_code` is the primary key and the
    auth-scope reasoning.
23. Workspace CRUD endpoints (`workspaces` create/update/archive/list/get —
    [06](06-api-mcp-and-scaling.md) §1) and archive-lifecycle semantics, plus access-policy
    grant/revoke endpoints ([05](05-admin-backend-and-maintenance.md) §7's "Access policy
    management" — `access_policy` rows are only ever written directly today, no admin surface).
    **Done** — [`workspaces.py`](../src/karpwiki/workspaces.py) (CRUD + grant/revoke/list-access),
    [`api.py`](../src/karpwiki/api.py) (`GET`/`POST /workspaces`,
    `GET`/`POST /workspaces/{id}`, `POST /workspaces/{id}/archive`,
    `GET`/`POST /workspaces/{id}/access-policy`,
    `DELETE /workspaces/{id}/access-policy/{principal}`) — see
    [09](09-implementation-notes.md) §26 for the creation bootstrap problem (no target workspace
    exists yet — reuses `09` §22's answer, plus auto-grants the creator admin so the workspace
    isn't a dead end), and for why `schema_ref` stays a pointer only (real `SCHEMA.md` storage is
    carried forward as its own future piece, not built as a CRUD side effect).
24. Classifier routing across the full active-workspace set, not one pre-selected workspace
    ([03](03-ingestion-and-review-workflows.md) §3: "routes submissions to a workspace/document
    type") — the routing this track is named for. Today `classify_source`/`resolve_classification`
    both take one `workspace` param the caller already picked.
    **Done** — [`document_types.py`](../src/karpwiki/document_types.py) (`list_active`,
    `workspace_for_type`), [`ingestion.py`](../src/karpwiki/ingestion.py) (`classify_source`/
    `resolve_classification` no longer take a `workspace` parameter; they resolve it via the
    taxonomy), [`api.py`](../src/karpwiki/api.py) (`ResolveRequest.workspace_id` removed —
    the resolve endpoint derives and authorizes the target workspace from the chosen
    `document_type`). Verified live against the real model: a taxonomy spanning two workspaces,
    correctly routed with no workspace named anywhere in the call — see
    [09](09-implementation-notes.md) §27.
25. `GET /search` endpoint (`search.py` has no HTTP endpoint yet) + a `query_log` table
    ([02](02-storage-and-indexing.md) §5, retention per [09](09-implementation-notes.md) §8) —
    workspace resolution defaults to "all accessible," with an optional taxonomy pre-filter
    ([04](04-search-and-retrieval.md) §4, [01](01-architecture-and-data-model.md) §2's Workspace
    Router).
    **Done** — [`query_log.py`](../src/karpwiki/query_log.py) (`record`, `purge_older_than`),
    [`search.py`](../src/karpwiki/search.py) (`SearchResult` — title/page_type/excerpt/
    citations, plus `page_type`/`tags`/`date_range` filters — 04 §6-7), `api.py`
    (`GET /search`: federated resolution, the taxonomy pre-filter, `query_log` writes — all
    gateway concerns per 01 §2, so they live there rather than in `search.py`). No cursor
    pagination (`limit` only) and no dedicated-index support (step 26) yet — see
    [09](09-implementation-notes.md) §28 for the full set of decisions.
26. Dedicated-index-per-workspace support for large workspaces, with cross-backend score
    normalization for the federated case ([04](04-search-and-retrieval.md) §4,
    [02](02-storage-and-indexing.md) §4) — [08](08-implementation-stack.md) §2 picks OpenSearch for
    this case (per-language analyzers, §3), package `opensearch-py` (§4).
    **Done, full integration** — a real `opensearch` service in `docker-compose.yml`, not a
    simulated second backend. [`dedicated_index.py`](../src/karpwiki/dedicated_index.py) (index/
    search against OpenSearch, one shared index across every dedicated workspace),
    [`search_result.py`](../src/karpwiki/search_result.py) (the `SearchResult` type + citation
    extraction, shared by both backends), [`search.py`](../src/karpwiki/search.py)
    (`merge_federated` — 04 §4's normalize-then-merge algorithm; `index_page` now dual-writes for a
    dedicated workspace), `api.py` (`/search` splits by backend and merges),
    [`workspaces.py`](../src/karpwiki/workspaces.py) + `POST /workspaces/{id}` (the admin toggle —
    found missing after the rest already worked, since nothing let an admin actually turn it on).
    Verified live against a real running OpenSearch throughout, including a genuine async
    client-lifecycle bug caught and fixed before it shipped — see
    [09](09-implementation-notes.md) §29 for the full set of decisions.
27. **Done.** Taxonomy bulk-move admin action — dry-run + batched execute
    ([`bulk_move.py`](../src/karpwiki/bulk_move.py), `api.py`'s
    `workspaces/{id}/bulk-move/preview` + `workspaces/{id}/bulk-move`). Takes an explicit
    `page_ids`/`source_ids` list rather than deriving a set from a document-type reassignment — the
    schema doesn't retain which type a page/source was originally classified under, so there's
    nothing to filter on after the fact. Batching and per-batch commit live in `api.py`, not the
    module (the one deliberate exception to this codebase's "modules never commit" convention,
    forced by "a failed batch halts without rolling back completed ones"); resumability falls out of
    `execute_batch` silently skipping anything no longer in the source workspace, so a bare retry of
    the same request is safe. Also fixed a real, pre-existing gap this step's design surfaced: a page
    leaving a dedicated workspace now gets its stale OpenSearch document explicitly deleted, which
    was impossible to trigger before this step (nothing could previously change a page's
    `workspace_id`). Verified live against the real dev Postgres DB and real MinIO S3 (not the test
    suite's temp-dir object store) — see [09](09-implementation-notes.md) §30.
28. **Done.** `page_link` cross-reference parsing
    ([`page_links.py`](../src/karpwiki/page_links.py), wired into
    `versioning.create_page`/`write_version`) — carried forward from Phase 1's
    accepted-simplifications note in [`phase1-tasklist.md`](phase1-tasklist.md), now load-bearing
    since cross-workspace links ([01](01-architecture-and-data-model.md) §3) are possible and it
    feeds 2c's Orphan Detector (step 39). Same-workspace targets match `wiki_page.path` directly;
    cross-workspace targets use `/{workspace_id}/{path}`, matching `objectstore.py`'s existing
    fully-qualified-path convention — the one concrete precedent for what `01` §6's
    "fully-qualified workspace-relative path" phrasing means. Parsing runs synchronously on every
    version write (cheap — no LLM call), not as an explicit-call lifecycle like reindexing. See
    [09](09-implementation-notes.md) §31.
29. **Done.** Verify: two documents of different `document_type`s route to different workspaces
    with no workspace named in the submission; one search query returns ranked results merged from
    both; a taxonomy bulk-move relocates a batch of pages/sources with per-batch progress.
    [`tests/test_end_to_end_2a.py`](../tests/test_end_to_end_2a.py) proves this through the real
    gateway with a mocked LLM; a companion live script repeated it against the real LLM (`gpt-5-
    nano`) and the real dev Postgres DB — two genuinely unrelated real documents (a Kubernetes
    runbook, an HR policy) routed correctly with no workspace named, one federated search query
    returned merged results from both, and a bulk-move relocated 3 pages in 3 real batches. No new
    application bugs found — see [09](09-implementation-notes.md) §32, including one search-
    semantics clarification the live script's own first mistake surfaced (`websearch_to_tsquery`
    ANDs bare terms; two topically unrelated documents need explicit `OR` syntax to both match one
    query). **Track 2a (Multi-Workspace & Taxonomy Routing) is complete.**

## 2b — Real Async Job Dispatch

30. Register real Celery tasks wrapping the existing pure orchestration functions: classification
    (`ingestion.classify_source`), curation (`ingestion.curate_source`), indexing
    (`search.reindex`) — the gap [09](09-implementation-notes.md) §21 named when it deliberately
    deferred this. **Done** — [`tasks.py`](../src/karpwiki/tasks.py) (`classify_source`,
    `curate_source`, `reindex` tasks on their respective queues), tested in
    [`tests/test_tasks.py`](../tests/test_tasks.py) and against real `gpt-5-nano`/dev Postgres/
    MinIO via a live script — see [09](09-implementation-notes.md) §33 for why dedup rides inside
    the `curation` task rather than getting a fourth task, and how the async/sync boundary is
    handled. Dispatch (who calls these) is still step 32; worker services are step 31.
31. `docker-compose.yml` worker services, one per queue (classification, curation, indexing,
    maintenance — [06](06-api-mcp-and-scaling.md) §4, [`tasks.py`](../src/karpwiki/tasks.py)'s
    `QUEUES`). **Done** — [`Dockerfile`](../Dockerfile) (new — this repo's first), four services
    in [`docker-compose.yml`](../docker-compose.yml) sharing one built image. Live dispatch through
    the real broker to the real containers caught and fixed a real cross-event-loop bug in
    [`tasks.py`](../src/karpwiki/tasks.py) (a second task in the same worker process crashed
    reusing `db.engine`'s pool across a fresh `asyncio.run()` loop) — see
    [09](09-implementation-notes.md) §34.
32. Wire dispatch: submission enqueues classification; acceptance enqueues dedup then curate; a
    page write enqueues reindex — [02](02-storage-and-indexing.md) §7's "always automatic"
    reindex, finally literal rather than an explicit test/admin call. **Done** —
    [`api.py`](../src/karpwiki/api.py) dispatches at submission, review-item resolution
    (classification/duplicate), rollback, and bulk-move; [`tasks.py`](../src/karpwiki/tasks.py)'s
    own tasks self-dispatch the next stage once their transaction commits. Surfaced a real gap in
    step 30's design (`_curate` needed to skip dedup when resuming from an admin-resolved
    duplicate) and a reproduced-once broker hiccup traced to this session's own pre-fixture test
    run, not the dispatch code — see [09](09-implementation-notes.md) §35. Live-verified: a
    document submitted over real HTTP reached `ingested` and became searchable with nothing
    manually driving the pipeline.
33. Task retry/idempotency semantics — [03](03-ingestion-and-review-workflows.md) §1's "transient
    failures retried inside the worker" framing has assumed a real worker since Phase 1; this is
    where one exists to do it. **Done** — [`ingestion.py`](../src/karpwiki/ingestion.py)'s
    `_retry_transient` wraps the three real LLM calls with backoff (3 attempts, doubling from 1s),
    recording the attempt count in `ingestion_log` once exhausted;
    [`tasks.py`](../src/karpwiki/tasks.py) sets `task_acks_late`/`task_reject_on_worker_lost` so a
    crashed worker's task gets redelivered rather than silently lost. A real gap found only by a
    live kill-and-restart check, not by any test: `acks_late` alone barely helps on the Redis
    transport without also tuning `visibility_timeout` down from its 3600s default — see
    [09](09-implementation-notes.md) §36 for the full story, the idempotency reasoning (the
    transition table's own `IllegalTransition` guard, not a new lock), and what's explicitly
    excluded (a stuck-job recovery sweep — Maintenance Advisor territory, track 2c).
34. Write the install/scaling docs explicitly gated on this landing since Phase 1
    ([`phase1-tasklist.md`](phase1-tasklist.md)'s accepted-simplifications note). **Done** —
    [`README.md`](../README.md)'s new "Scaling" section, grounded in
    [06](06-api-mcp-and-scaling.md) §4, states plainly what's real (per-queue worker scaling,
    live-verified with two replicas correctly splitting a burst of dispatched work) versus
    roadmap-only (Metadata DB partitioning, the cache). Also fixed two stale README claims dispatch
    wiring (step 32) had quietly made false. See [09](09-implementation-notes.md) §37.
35. **Verify**: submit a document via the API with nothing manually driving the pipeline forward —
    confirm it reaches `ingested` and becomes searchable on its own, purely via workers, within a
    bounded time. **Done** — [`tests/test_end_to_end_2b.py`](../tests/test_end_to_end_2b.py) (new,
    committed) drains the real dispatch chain with a mocked LLM/no broker; a live run over real
    HTTP/broker/workers/`gpt-5-nano` reached `ingested` and became searchable in 36.5s, well inside
    a 90s bound. No new bugs found. See [09](09-implementation-notes.md) §38. **Track 2b (Real
    Async Job Dispatch) is complete.**

## 2c — Maintenance Advisor

36. Staleness Detector → `reindex` review items
    ([05](05-admin-backend-and-maintenance.md) §2 table, §3). **Done** —
    [`advisor.py`](../src/karpwiki/advisor.py) (new module for all of track 2c's detectors):
    `find_stale_pages`/`find_pages_citing_superseded_sources` (the two 05 §2 signals),
    `run_staleness_detector` (batches findings into one `reindex` item per workspace per run,
    05 §3), `resolve_reindex` (new resolution path — `reindex now`/`dismiss`). `ReviewItem`
    gained a `detail` JSONB column (new migration `20102d0aa751`) since Maintenance Advisor
    items have no `ingestion_log` to fall back on for evidence the way ingest-time items do
    (09 §22). New `karpwiki.maintenance.detect_staleness` Celery task — the maintenance queue's
    first real task. Live-verified against real dev Postgres and the real worker containers:
    both signals detected correctly, resolved over the real gateway, both pages reindexed. See
    [09](09-implementation-notes.md) §39.
37. Superseded-Source Detector → `prune` review items, 180-day retention (already decided,
    [09](09-implementation-notes.md) §8) — [05](05-admin-backend-and-maintenance.md) §4. **Done** —
    `RawSource` gained a `superseded_at` column (migration `da3c87c7d151`, set only in
    `ingestion._resolve_supersede` — the sole place `status` flips to `superseded`), since nothing
    otherwise recorded when the retention clock starts. `advisor.find_superseded_sources_past_retention`/
    `run_superseded_source_detector` follow step 36's batching shape exactly; `resolve_prune`
    (new, extensible to steps 39-40's other `prune` reasons) implements `delete superseded source`
    as a status flip to `archived` only — 05 §4 delegates physical erasure to an external
    object-store lifecycle policy, not application code. Live-verified against real dev Postgres
    and the real worker containers: a 200-day-old superseded source was flagged, a 30-day-old one
    correctly excluded, resolved over the real gateway, archived. See
    [09](09-implementation-notes.md) §40.
38. Existing-Content Duplicate Detector
    ([05](05-admin-backend-and-maintenance.md) §5) → `duplicate` review items, reusing
    `resolve_duplicate`'s existing reject/merge/supersede/keep_both actions unchanged, tagged
    `raised_by=advisor`. **Done** — `advisor.find_similar_page_pairs`/
    `run_existing_content_duplicate_detector` (one item per similar pair, not batched like
    steps 36-37 — pair-specific resolutions don't compose with batching); new
    `advisor.resolve_existing_duplicate` reuses the four action names with page-pair semantics,
    routed via `item.detail["raised_by"] == "advisor"`, leaving `ingestion.resolve_duplicate`
    completely untouched. Forced a small refactor: the retry-with-backoff helper moved from
    `ingestion.py` to `llm.py` so `advisor.py` could reuse it for its own merge call without an
    import cycle. Live-verified against real dev Postgres/workers, including a real `gpt-5-nano`
    merge call: two near-identical pages matched (score 1.0), resolved as `merge`, primary page
    got a real new version, duplicate archived, reindexed. See
    [09](09-implementation-notes.md) §41.
39. Orphan/Low-Traffic Detector → `prune` review items — needs `page_link` inbound-reference
    counts (step 28) and `query_log` (step 25), both now available. **Done** —
    `advisor.find_orphaned_pages`/`run_orphan_detector` (batched, same shape as step 37) require
    *both* zero inbound `page_link` rows and zero `query_log` appearances over the 90-day lookback
    (09 §8), scoped to `concept`/`entity`/`comparison` pages only. Caught and fixed a real
    pre-existing bug in step 37's `_open_prune_item` before it shipped: it wasn't scoped by
    `detail["reason"]`, so an open `superseded_source_retention` item would have silently blocked
    every `orphaned` finding from ever being raised. `resolve_prune` now branches by reason
    (`archive page`/`dismiss` for `orphaned`). Live-verified against real dev Postgres/workers with
    real `page_link` parsing and a real `query_log` row from a real search call: exactly the two
    genuine orphans flagged, the linked and searched pages correctly excluded. See
    [09](09-implementation-notes.md) §42.
40. Contradiction Detector (Curator lint pass) → `reindex`/`prune` — net new LLM capability, no
    prior design to build from (flagged open in §0 above); resolve its design when this step
    starts, the same way Phase 1 resolved forks at the step they blocked. **Done** —
    `advisor.find_contradiction_candidates`/`run_contradiction_detector`: a cheap lexical
    prefilter (`search.find_similar`, same mechanism step 38 uses) narrows candidates to a
    `[0.35, 0.60)` similarity band (upper bound reuses `dedup.DEFAULT_NEAR_DUPLICATE_SCORE` so
    step 38's near-duplicate pool and this one never overlap), then a real Pydantic AI call
    (`call_contradiction_check`) judges each candidate — the first detector that spends an LLM
    call during *detection*, not just resolution, capped at 5 checks/run. Confirmed pairs raise
    a `prune` item (reason=`contradicted_by`, pair-specific like step 38's duplicates, not
    batched); `resolve_prune` gained a third reason branch (`archive page`/`dismiss`), the branch
    step 39's own docstring already predicted. `lint_log` deliberately stays unbuilt (design
    question resolved via AskUserQuestion) — steps 36-39 never wrote to any log stream either,
    relying entirely on `ReviewItem.detail`. Live-verified against real dev Postgres and a real
    `gpt-5-nano` call, both directions: a real conflicting-claim pair correctly confirmed and
    resolved, a real same-band-but-non-conflicting pair correctly raised nothing. See
    [09](09-implementation-notes.md) §43.
41. Scheduling: popularity-tiered refresh via Celery beat
    ([05](05-admin-backend-and-maintenance.md) §2's scheduling philosophy). **Done** — a
    `celery-beat` service fires two dispatcher tasks (`dispatch_daily_detectors`,
    `dispatch_contradiction_detector`) that enumerate active workspaces at fire time and
    re-enqueue each detector's existing per-workspace task; Contradiction Detection gets
    its own, less frequent interval since it spends a real LLM call per candidate (step
    40), the other four (no LLM cost at detection) share the faster one. Both intervals
    are env-overridable (`KARPWIKI_MAINTENANCE_INTERVAL_HOURS`, default 24;
    `KARPWIKI_MAINTENANCE_CONTRADICTION_INTERVAL_HOURS`, default 168) — a deployment-wide
    operational knob, unlike every detector's own content thresholds, which stay Python
    defaults per `09` §26's SCHEMA.md-deferral precedent. Popularity tiering itself
    (`advisor.find_stale_pages_tiered`) layers on top of `find_stale_pages` without
    changing it: call it once per tier's day count (also env-overridable —
    `KARPWIKI_STALENESS_HIGH_TRAFFIC_DAYS`/`_LOW_TRAFFIC_DAYS`, defaults 90/365) and keep
    a page that clears either its own tier's bar or the stricter one; "high traffic"
    reuses the orphan detector's existing query_log-presence check as the popularity
    signal. A real bug caught live: `celery-beat` running as the Dockerfile's non-root
    user couldn't write its default schedule file to `/app` (root-owned from the image
    build) — fixed with `--schedule=/tmp/celerybeat-schedule`. Live-verified against the
    real dev Postgres/broker/worker containers: both dispatcher tasks fired for real
    through the real broker, correctly enumerating every active workspace; tiered
    staleness correctly flagged only the high-traffic page. A real contradiction check
    against test-suite content the model correctly judged non-conflicting (not a bug —
    just never previously checked against the real model), re-verified with step 40's
    own proven conflicting pair, dispatched directly and confirmed/resolved correctly.
    Also surfaced that this session's accumulated throwaway live-check workspaces are all
    still `active`, so a real beat tick now sweeps all of them too — flagged to the user,
    not fixed as part of this step. See [09](09-implementation-notes.md) §44.
42. **Verify**: seed stale, orphaned, superseded, and duplicate content; run the advisor; confirm
    review items appear in the existing queue with correct evidence and resolve through the same
    endpoints ingest-time items already use. **Done** — `tests/test_end_to_end_2c.py` (new,
    committed) seeds all five signals (stale, orphaned, superseded source, near-duplicate pair,
    contradicting pair) in one workspace, runs all five detectors, and resolves every resulting
    item through the real `GET /review-items`/`POST /review-items/{id}/resolve` endpoints — mocked
    LLM, no broker, matching steps 29/35's own closing-verify convention. Found a real (non-bug)
    overlap while seeding: most pages are incidentally also orphans by the Orphan Detector's own
    definition, fixed by giving non-orphan-detector pages a `query_log` entry so each detector's
    evidence assertion stays cleanly attributable. Live-verified the one combination no prior
    step's check had exercised together: a Contradiction Detector item raised by a real
    `gpt-5-nano` call, resolved through the real running HTTP gateway. See
    [09](09-implementation-notes.md) §45. **Track 2c (Maintenance Advisor) is now fully closed.**

## 2d — Full API + MCP + Horizontal Scaling

43. Complete the REST surface: `pages` get/list (never built) including an admin raw-source
    browser view of `supersedes` chains
    ([05](05-admin-backend-and-maintenance.md) §7), `connectors` list/configure (stubbed until
    2e lands). **Done** — `GET /pages`/`GET /pages/{id}` (`versioning.list_pages`, new): reuses
    `search.search()`'s exact tag/date filter semantics against the same frontmatter, no
    tsvector/ranking; draft content requires `contributor` (mirrors `/search`'s
    `include_drafts` elevated-scope reasoning, 04 §6). `GET /sources` (new, admin-only): the
    Raw Source Browser, scoped to a "view" per the tasklist's own wording (re-ingestion/
    retention actions from 05 §7's full row not built); each item's own `supersedes` pointer
    lets a client walk a chain through the same list. Added `RawSource.created_at` (migration
    `e2bd2860a135`) — nothing recorded a source's timestamp before, and every other list
    endpoint shares one `(created_at, id)` cursor convention (09 §14); resolved via
    AskUserQuestion before implementing. `GET /connectors` does real admin auth but always
    returns empty; `POST /connectors` real-auths then 501s — a deliberate stub, not a partial
    implementation, closed out by track 2e's step 51. Live-verified against real dev Postgres
    and a real running gateway (no LLM involved). See [09](09-implementation-notes.md) §46.
44. Performance Monitoring dashboards — index health, ingestion pipeline, search performance,
    storage utilization, review queue health
    ([05](05-admin-backend-and-maintenance.md) §8) — the "performance-focused" half of
    [00](00-overview.md) §7's admin-UI requirement that 2a–2c's queue/version/workspace surfaces
    don't cover on their own. **Done** — five new `GET /metrics/*` admin endpoints
    (`monitoring.py`, new), all but queue depth taking an optional `workspace_id` (same
    optional-scope shape `document-types` established). Two real forks resolved via
    AskUserQuestion first: added `QueryLog.duration_ms` (migration `f8fb063dacdb`) and
    instrumented `/search` for real p50/p95 latency; added a direct real Redis `LLEN` per
    queue (`monitoring.queue_depths`, `redis.asyncio`) for real queue depth — the first
    place `api.py` reads Redis directly rather than only enqueuing through `tasks.*`. Two
    accepted gaps documented rather than faked: cache hit rate (no cache layer exists, `02`
    §6 is roadmap-only) and storage trend (no time-series mechanism exists anywhere in this
    codebase) both report `None`. `index_health`'s "stuck" detection reuses
    `advisor.find_stale_pages`'s exact missing-timestamp proxy (09 §39). Storage figures are
    documented content-byte approximations, not real Postgres storage accounting. Live-
    verified against real dev Postgres, gateway, and Redis — a first pass correctly showed
    `object_store_bytes: 0` for a page-only workspace (no raw source, nothing to measure;
    not a bug), re-verified with a real raw source added. See
    [09](09-implementation-notes.md) §47.
45. MCP server via the official `mcp` Python SDK
    ([08](08-implementation-stack.md) §2's pick; package name in §4; not yet in `pyproject.toml`)
    — a thin 1:1 adapter over existing gateway operations
    ([06](06-api-mcp-and-scaling.md) §2's tool table: `wiki_search`, `wiki_get_page`,
    `wiki_list_pages`, `wiki_list_workspaces`, `wiki_submit`, `wiki_get_source_status`,
    `wiki_list_review_items`, `wiki_resolve_review_item`, `wiki_get_page_versions`,
    `wiki_rollback_page`). **Done** — `mcp_server.py` (new), all ten tools, both `stdio` and
    streamable-HTTP transports. Two forks resolved via AskUserQuestion first: extracted
    `api.run_search`/`api.run_resolve_review_item` (the two operations with real gateway
    orchestration, per `01` §2's shared-Common-Gateway framing) so both REST and MCP call the
    same functions — a behavior-preserving refactor verified by the full pre-existing test
    suite passing unchanged; the other eight tools stay thin, duplicating a role check plus
    one service-layer call, matching `api.py`'s own style. stdio (no per-call headers) resolves
    one identity at startup from `KARPWIKI_MCP_USER`/`KARPWIKI_MCP_GROUPS`, reusing
    `TrustedHeaderAuthenticator` as-is; streamable HTTP reuses real per-request headers via
    `ctx.headers`. `wiki_submit` is text-only (file/URL don't map onto MCP's JSON args, not
    named in `06` §2's table). The installed SDK (`mcp` 2.0.0, pinned `>=2.0`) exposes a
    different API surface than "FastMCP" docs describe (`mcp.server.mcpserver.MCPServer`, not
    `mcp.server.fastmcp.FastMCP`) — worth knowing before assuming an older example applies.
    Live-verified both transports for real: a real `streamable_http_client` connection with a
    real header found real content and a headerless one correctly errored; a real `python -m
    karpwiki.mcp_server` subprocess (the real stdio entry point) listed all ten tools and
    resolved its env-var identity. See [09](09-implementation-notes.md) §48.
46. On-behalf-of delegation for `wiki_submit` — fully designed already
    ([09](09-implementation-notes.md) §5). **Done** — `wiki_submit` gained an `acting_as:
    "user:<id>"` argument; AuthZ requires the calling agent's and the represented user's
    contributor-workspace sets to intersect (the two-principal generalization of the
    existing "contributor anywhere" check, since — like the non-delegated path — no target
    workspace is known until classification runs). `api._store` gained `extra_detail`, used
    to record the agent's own identity in the `ingestion_log` entry (no new core field, per
    09 §5). `wiki_get_source_status` intentionally not extended — a known, flagged scope
    boundary (the represented user can always check status themselves). Live-verified via
    the real stdio subprocess entry point: a real delegated submission succeeded with the
    real audit trail, a real under-privileged delegation was correctly rejected. See
    [09](09-implementation-notes.md) §49.
47. Real OIDC/SAML `Authenticator` implementation using Authlib
    ([08](08-implementation-stack.md) §2's pick) — [09](09-implementation-notes.md) §15 named this
    as Phase 2's second provider, swapped in with no handler changes. **Done** —
    `OidcAuthenticator` (new, `auth.py`): real bearer-JWT validation against a configured
    IdP's JWKS (via `joserfc`, Authlib's own currently-recommended JWT library —
    `authlib.jose` is deprecated), with OIDC discovery, indefinite JWKS caching, and a
    refetch-once-on-unknown-`kid` retry for key rotation. `auth.default_authenticator()`
    picks it automatically once `KARPWIKI_OIDC_ISSUER`/`_AUDIENCE` are both set, otherwise
    keeps `TrustedHeaderAuthenticator` — no handler changes, confirmed live. **SAML is not
    supported**: Authlib genuinely has no SAML module (verified directly against the
    installed package) — resolved via AskUserQuestion as an explicit, documented gap
    rather than pulling in a second, much larger SAML-specific library nothing else names.
    `Authenticator.authenticate` became `async` (confirmed via AskUserQuestion first) so a
    real JWKS fetch never blocks the event loop — a small, mechanical change across three
    already-async call sites, caught immediately by the full test suite. Found and fixed a
    real interaction bug before it shipped: stdio's env-var identity synthesis only
    produced trusted-header-shaped headers, which would have silently broken stdio MCP
    auth entirely once real OIDC was configured (`OidcAuthenticator` only reads
    `Authorization: Bearer`) — fixed by adding `KARPWIKI_MCP_TOKEN`, tried first, falling
    back to the existing `KARPWIKI_MCP_USER`/`_GROUPS` behavior unchanged. Live-verified
    against a real local HTTP server acting as the IdP (real discovery + JWKS, not just
    the committed tests' `MockTransport`): a real `uvicorn` subprocess with only the two
    OIDC env vars set correctly accepted a real signed token and correctly rejected no
    token/wrong audience/expired token; the `KARPWIKI_MCP_TOKEN` fix verified the same way
    via a real stdio subprocess. See [09](09-implementation-notes.md) §50.
48. Rate limiting ([07](07-additional-features-and-roadmap.md) §3) — the `RateLimit-*` headers
    were already specified in [09](09-implementation-notes.md) §14 but never implemented, since no
    limiter existed to emit them. **Done** — `ratelimit.py` (new): a Redis-backed fixed-window
    counter behind the existing header contract, wired into `api.py` as an `enforce_rate_limit`
    middleware, REST only (MCP has no HTTP header concept, especially on `stdio`). Per-principal
    is always checked (a coarse, unverified hashed identity key, so an unauthenticated caller is
    still throttled without running a real `Authenticator`); per-workspace is opportunistic —
    checked only when `workspace_id` is already a plain request parameter — confirmed via
    AskUserQuestion before building. Three categories mirror `07` §3's own three (submissions,
    search calls, general API requests), each independently configurable via new
    `KARPWIKI_RATE_LIMIT_*` env vars. Middleware ordering verified empirically (Starlette wraps
    `@app.middleware` registrations in reverse order) so a breached request's 429 body still
    carries a real `request_id`. Running the full suite surfaced a real test-isolation gap: the
    shared dev Redis instance carries rate-limit counters across the whole pytest run, unlike the
    per-test Postgres DB, so 4 unrelated tests started failing once accumulated counters from
    earlier tests tripped the real default limits. Fixed by moving the category→limit lookup into
    `create_app()` (read fresh per app instance, not frozen at import) plus a new autouse
    `generous_rate_limits` fixture (mirroring step 32's `dispatched` fixture) that raises the
    limits for the suite by default; `test_ratelimit.py` (new) exercises real enforcement directly
    with a per-test-unique principal. Live-verified against a real `uvicorn` subprocess: real 429s
    with the standard error envelope and `Retry-After`/`RateLimit-*` headers once a real, tightly
    configured limit was exhausted, and a real reset once the window elapsed. See
    [09](09-implementation-notes.md) §51.
49. Horizontal scaling: multiple gateway instances behind a load balancer, worker pools scaling
    independently now that 2b makes them real
    ([06](06-api-mcp-and-scaling.md) §5 deployment topology). **Done** — worker-pool independent
    scaling was already live-verified in step 34; the Gateway itself had never been
    containerized. Confirmed the build-it-for-real scope via AskUserQuestion first (over a
    documentation-only alternative), since step 50's own verify needs real LB infra to run
    against. New `gateway` docker-compose service reuses the existing worker image (same
    `Dockerfile`, `command: uvicorn karpwiki.api:app` instead of a Celery worker) — `expose`,
    not `ports`, since it's meant to be scaled. New `nginx` service + `nginx.conf` round-robin
    across replicas, using the standard Docker Compose + nginx dynamic-DNS-resolution pattern
    (`resolver 127.0.0.11` + a `set $upstream` variable) rather than a bare `proxy_pass`, which
    would cache one container's IP for nginx's lifetime and never see new replicas. New `GET
    /healthz` route for Docker's per-replica healthcheck, deliberately exempted from step 48's
    rate limiter entirely — otherwise every replica's own healthcheck (no auth header) would
    share the same "anon" Redis counter and could throttle each other's liveness probes at
    enough replicas. Live-verified against real containers: built the image, scaled to 3
    replicas (independently Docker-healthy), fired 15 requests at nginx and confirmed via each
    container's own logs a real 7/5/3 split (not all on one instance); ran one full submit →
    real `gpt-5-nano` classify/curate → index → search round trip through the load balancer,
    not just a bare-GET smoke test. Scaled back to 1 replica and deleted the throwaway
    workspace afterward (celery-beat is live in this stack and would otherwise eventually sweep
    it with a real paid LLM call). See [09](09-implementation-notes.md) §52.
50. **Verify**: an MCP client can search, submit, and (as admin) resolve a review item end-to-end
    through the protocol adapter, not only through the REST surface; a second gateway instance
    behind a load balancer serves traffic with no session-affinity requirement. **Done** —
    `tests/test_end_to_end_2d.py` (new, committed): the same flow entirely through
    `mcp.client.client.Client` in-process, mocked LLM/no broker, draining the real dispatch chain
    (matching `test_end_to_end_2b.py`'s convention) so `wiki_search` finds a genuinely
    curated-and-indexed page. Live-verified against real infra: a real stdio MCP subprocess per
    identity ran submit → poll → search → admin-resolve end to end (real Postgres, real
    Redis-dispatched workers, real `gpt-5-nano`) — two non-bug surprises caught along the way
    (a real below-threshold classification needing admin resolution, and a real near-duplicate
    flag on a same-wording retry), both legitimate review paths `03`/`05` already design for, not
    failures. Separately, reusing step 49's real `gateway`/`nginx` infra scaled to 2 replicas: one
    persistent client connection ran a submit → poll → search sequence through nginx, and
    container logs confirmed the individual requests within that *one* logical session landed on
    different replicas (submit on `gateway-2`, polls interleaved across both, search on
    `gateway-1`) — the literal "no session-affinity requirement" claim, not just multiple
    containers coincidentally running. Cleaned up both live checks' throwaway workspaces and
    scaled back to 1 replica afterward. See [09](09-implementation-notes.md) §53.

## 2e — Connector Framework

51. `Connector` model (schema already named in [09](09-implementation-notes.md) §13,
    [02](02-storage-and-indexing.md) §3) + `connectors` API
    ([06](06-api-mcp-and-scaling.md) §1). **Done** — `models.Connector` (new table, migration
    `7c8a26059991`) + `connectors.py` (create/list/update) + real `GET`/`POST /connectors` and
    new `POST /connectors/{id}`, replacing step 43's deliberate stub. Storage and admin CRUD
    only — the polling worker pool (step 52), credential resolution via a secrets manager (step
    53), and the first concrete connector type (step 54) are separate. `workspace_id` fixed at
    creation, never reassigned (09 §13's "exactly one workspace" boundary); `type`/`config`/
    `schedule`/`last_sync_cursor` stay open-ended (no connector type exists yet to interpret
    them); `credential_ref` accepts only a caller-supplied secrets-manager pointer, never a raw
    secret — step 53's own scope, kept out of this step's code path entirely, since storing a
    pasted-in raw secret would violate 09 §13's "never stored in the Metadata DB" rule.
    `connectors.create` auto-grants the new `connector:<id>` principal `contributor` on its one
    workspace in the same transaction, since nothing else could ever establish that grant. A
    migration round-trip bug caught by this project's own discipline, not assumed clean:
    `drop_table` alone doesn't drop the new `connector_state` enum type, so downgrade-then-
    upgrade failed until an explicit `DROP TYPE` was added to `downgrade()`; re-verified clean
    against real dev Postgres and a genuinely empty database. Live-verified against the real,
    load-balanced gateway (step 49's infra, rebuilt): real create → list → disable, a real
    unauthorized 403, and the real `access_policy` grant confirmed by querying Postgres
    directly. See [09](09-implementation-notes.md) §54.
52. Connector polling worker pool — fetch, diff against a per-connector cursor, create a
    `raw_source` — fully designed already ([09](09-implementation-notes.md) §4).
53. Credential resolution via the secrets-manager interface
    ([09](09-implementation-notes.md) §13), mirroring the pluggable `Authenticator` pattern
    already in [`auth.py`](../src/karpwiki/auth.py).
54. First concrete connector type — a Git repo poller, the simplest state model (commit-SHA
    diffing) — [03](03-ingestion-and-review-workflows.md) §2.
55. `disabled_auth` state + Notification Service hook for connector auth failures
    ([09](09-implementation-notes.md) §13).
56. **Verify**: configure a connector, run one poll cycle, confirm it creates `raw_source`s
    indistinguishable from a manual upload and that they flow through the normal pipeline
    (classification, dedup, curation) unchanged.

## Exit Criteria

Matches [00](00-overview.md) §7's requirements traceability table in full — every row, not just
the Phase 1 subset:

| Requirement | Closed by |
|---|---|
| Karpathy's general pattern | Phase 1 |
| Enterprise-scale schema changes | Steps 22, 26 (2a as a whole) |
| Robust storage mechanism | Phase 1 + step 26 |
| Horizontal scaling | Steps 30–35, 49 |
| Multiple storages/indices/logs behind a common gateway | Steps 26, 43–49 |
| Backend management: ingestion, pruning, versioning, repo management | Phase 1 (ingestion, versioning) + steps 23, 27, 36–41 (pruning), 43, 51–56 (repo management) |
| Admin-only backend UI, performance-focused | Phase 1 (queue, version browser) + step 44 |
| Exposure via API and MCP | Steps 43, 45–48 |
| Robust search mechanism | Phase 1 (single-workspace) + steps 25–26 (federated) |
| End-user document submission → review item | Phase 1 |
| Identify indexing/reindexing needs, propose actions | Step 36 |
| Identify pruning needs, propose actions | Steps 37, 39 |
| Identify potential duplicates, raise review item | Phase 1 (ingest-time) + step 38 (existing content) |
| Versioning with rollback | Phase 1 |
| Propose additional completeness features | [07](07-additional-features-and-roadmap.md) itself — ongoing, not a step |

---
Previous: [phase1-tasklist.md](phase1-tasklist.md) · Back to: [00-overview.md](00-overview.md)
