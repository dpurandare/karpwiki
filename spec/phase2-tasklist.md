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

**Status (2026-08-17): steps 22–23 done**, 2a in progress. Phase 1 (steps 1–21,
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
(step 26); and the Contradiction Detector's Curator lint-pass design (net new — nothing in `09`
covers a lint pass yet, step 40). The auth *library* is not open —
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
25. `GET /search` endpoint (`search.py` has no HTTP endpoint yet) + a `query_log` table
    ([02](02-storage-and-indexing.md) §5, retention per [09](09-implementation-notes.md) §8) —
    workspace resolution defaults to "all accessible," with an optional taxonomy pre-filter
    ([04](04-search-and-retrieval.md) §4, [01](01-architecture-and-data-model.md) §2's Workspace
    Router).
26. Dedicated-index-per-workspace support for large workspaces, with cross-backend score
    normalization for the federated case ([04](04-search-and-retrieval.md) §4,
    [02](02-storage-and-indexing.md) §4) — [08](08-implementation-stack.md) §2 picks OpenSearch for
    this case (per-language analyzers, §3), package `opensearch-py` (§4).
27. Taxonomy bulk-move admin action — dry-run + batched execute, fully designed already
    ([09](09-implementation-notes.md) §11).
28. `page_link` cross-reference parsing (carried forward from Phase 1's accepted-simplifications
    note in [`phase1-tasklist.md`](phase1-tasklist.md)) — load-bearing now that cross-workspace
    links ([01](01-architecture-and-data-model.md) §3) are possible, and feeds 2c's Orphan
    Detector (step 39).
29. **Verify**: two documents of different `document_type`s route to different workspaces with no
    workspace named in the submission; one search query returns ranked results merged from both;
    a taxonomy bulk-move relocates a batch of pages/sources with per-batch progress.

## 2b — Real Async Job Dispatch

30. Register real Celery tasks wrapping the existing pure orchestration functions: classification
    (`ingestion.classify_source`), curation (`ingestion.curate_source`), indexing
    (`search.reindex`) — the gap [09](09-implementation-notes.md) §21 named when it deliberately
    deferred this.
31. `docker-compose.yml` worker services, one per queue (classification, curation, indexing,
    maintenance — [06](06-api-mcp-and-scaling.md) §4, [`tasks.py`](../src/karpwiki/tasks.py)'s
    `QUEUES`).
32. Wire dispatch: submission enqueues classification; acceptance enqueues dedup then curate; a
    page write enqueues reindex — [02](02-storage-and-indexing.md) §7's "always automatic"
    reindex, finally literal rather than an explicit test/admin call.
33. Task retry/idempotency semantics — [03](03-ingestion-and-review-workflows.md) §1's "transient
    failures retried inside the worker" framing has assumed a real worker since Phase 1; this is
    where one exists to do it.
34. Write the install/scaling docs explicitly gated on this landing since Phase 1
    ([`phase1-tasklist.md`](phase1-tasklist.md)'s accepted-simplifications note).
35. **Verify**: submit a document via the API with nothing manually driving the pipeline forward —
    confirm it reaches `ingested` and becomes searchable on its own, purely via workers, within a
    bounded time.

## 2c — Maintenance Advisor

36. Staleness Detector → `reindex` review items
    ([05](05-admin-backend-and-maintenance.md) §2 table, §3).
37. Superseded-Source Detector → `prune` review items, 180-day retention (already decided,
    [09](09-implementation-notes.md) §8) — [05](05-admin-backend-and-maintenance.md) §4.
38. Existing-Content Duplicate Detector
    ([05](05-admin-backend-and-maintenance.md) §5) → `duplicate` review items, reusing
    `resolve_duplicate`'s existing reject/merge/supersede/keep_both actions unchanged, tagged
    `raised_by=advisor`.
39. Orphan/Low-Traffic Detector → `prune` review items — needs `page_link` inbound-reference
    counts (step 28) and `query_log` (step 25), both now available.
40. Contradiction Detector (Curator lint pass) → `reindex`/`prune` — net new LLM capability, no
    prior design to build from (flagged open in §0 above); resolve its design when this step
    starts, the same way Phase 1 resolved forks at the step they blocked.
41. Scheduling: popularity-tiered refresh via Celery beat
    ([05](05-admin-backend-and-maintenance.md) §2's scheduling philosophy).
42. **Verify**: seed stale, orphaned, superseded, and duplicate content; run the advisor; confirm
    review items appear in the existing queue with correct evidence and resolve through the same
    endpoints ingest-time items already use.

## 2d — Full API + MCP + Horizontal Scaling

43. Complete the REST surface: `pages` get/list (never built) including an admin raw-source
    browser view of `supersedes` chains
    ([05](05-admin-backend-and-maintenance.md) §7), `connectors` list/configure (stubbed until
    2e lands).
44. Performance Monitoring dashboards — index health, ingestion pipeline, search performance,
    storage utilization, review queue health
    ([05](05-admin-backend-and-maintenance.md) §8) — the "performance-focused" half of
    [00](00-overview.md) §7's admin-UI requirement that 2a–2c's queue/version/workspace surfaces
    don't cover on their own.
45. MCP server via the official `mcp` Python SDK
    ([08](08-implementation-stack.md) §2's pick; package name in §4; not yet in `pyproject.toml`)
    — a thin 1:1 adapter over existing gateway operations
    ([06](06-api-mcp-and-scaling.md) §2's tool table: `wiki_search`, `wiki_get_page`,
    `wiki_list_pages`, `wiki_list_workspaces`, `wiki_submit`, `wiki_get_source_status`,
    `wiki_list_review_items`, `wiki_resolve_review_item`, `wiki_get_page_versions`,
    `wiki_rollback_page`).
46. On-behalf-of delegation for `wiki_submit` — fully designed already
    ([09](09-implementation-notes.md) §5).
47. Real OIDC/SAML `Authenticator` implementation using Authlib
    ([08](08-implementation-stack.md) §2's pick) — [09](09-implementation-notes.md) §15 named this
    as Phase 2's second provider, swapped in with no handler changes.
48. Rate limiting ([07](07-additional-features-and-roadmap.md) §3) — the `RateLimit-*` headers
    were already specified in [09](09-implementation-notes.md) §14 but never implemented, since no
    limiter existed to emit them.
49. Horizontal scaling: multiple gateway instances behind a load balancer, worker pools scaling
    independently now that 2b makes them real
    ([06](06-api-mcp-and-scaling.md) §5 deployment topology).
50. **Verify**: an MCP client can search, submit, and (as admin) resolve a review item end-to-end
    through the protocol adapter, not only through the REST surface; a second gateway instance
    behind a load balancer serves traffic with no session-affinity requirement.

## 2e — Connector Framework

51. `Connector` model (schema already named in [09](09-implementation-notes.md) §13,
    [02](02-storage-and-indexing.md) §3) + `connectors` API
    ([06](06-api-mcp-and-scaling.md) §1).
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
