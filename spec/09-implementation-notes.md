# 09 — Implementation Notes (Design Decisions)

## 1. Purpose and Scope

`techfeasibility.md` (working doc, not part of this spec) flagged a set of implementation-
readiness gaps in `00`–`08`. The first pass (§3–7) covered questions concrete enough to answer as
design decisions, in the same spirit as `08`'s reference-implementation choices. A second pass
(§8–12) covers the remaining org/runtime decisions — retention defaults, classifier confidence
calibration, relevance-test ownership, taxonomy bulk-move safeguards, and FUSE access scope —
using values supplied by the organization adopting this spec. A third pass (§13) closes the
connector credential/security gap left open by §4, which covered connector *execution* only. A
fourth pass (§14–19) settles what Phase 1's endpoints need as they're actually built — API
conventions, the baseline auth scope, the LLM model default, the near-duplicate metric, and the
creation timing of the placeholder page and of review items. NFR targets specifically are recorded
in [06](06-api-mcp-and-scaling.md) §6, the placeholder table designated for that purpose.

Unlike `08` (which only picks libraries for roles `00`–`07` already define), several of the
decisions below imply a small addition to `00`–`07`'s conceptual data model or prose. Each item
below notes its **spec touch-point** where relevant — these have since been applied to
`02`/`03`/`05`/`06`/`07` (see each section).

## 2. Decisions at a Glance

| Concern | Spec reference | Decision |
|---|---|---|
| Pipeline-state storage | [03](03-ingestion-and-review-workflows.md) §1, [06](06-api-mcp-and-scaling.md) §1 | New `raw_source.pipeline_state` field — denormalized current-state pointer, mirrors `wiki_page.current_version_id` |
| Connector execution model | [03](03-ingestion-and-review-workflows.md) §2, [05](05-admin-backend-and-maintenance.md) §7, [01](01-architecture-and-data-model.md) §1 | Dedicated lightweight "connector polling" worker pool; default = scheduled poll + diff-against-last-sync, creates a normal `raw_source` on change |
| MCP on-behalf-of delegation | [06](06-api-mcp-and-scaling.md) §2–3 | Dual-identity request: agent's own AuthN + `acting_as: user:<id>` claim, AuthZ-checked against both principals |
| `SCHEMA.md` example | [01](01-architecture-and-data-model.md) §7 | Concrete template with starting threshold defaults (§6 below) |
| `diff_ref` format | [01](01-architecture-and-data-model.md) §5, [02](02-storage-and-indexing.md) §3 | Computed-on-write unified diff, stored in Object Store at `/{workspace_id}/diffs/{version_id}.diff` |
| Query-log & threshold retention defaults | [02](02-storage-and-indexing.md) §5, [05](05-admin-backend-and-maintenance.md) §2, §6 below | 90-day full-detail `query_log` retention (no separate anonymization step); 90-day orphan/low-traffic lookback; 180-day superseded-source retention (confirms §6's illustrative default) |
| Classifier confidence calibration | [03](03-ingestion-and-review-workflows.md) §1, §3 | Self-reported confidence in structured output, periodically recalibrated against admin-resolved `classification` review items |
| Relevance testing / regression process | [04](04-search-and-retrieval.md) §3–4, [07](07-additional-features-and-roadmap.md) §4 | No dedicated test set — the search result feedback loop is the regression signal |
| Taxonomy bulk-move execution model | [05](05-admin-backend-and-maintenance.md) §7 | Dry-run preview, then batched execute with per-batch progress; failed batch halts, completed batches not rolled back |
| FUSE-mount access scope | [02](02-storage-and-indexing.md) §2 | Read-only, opt-in per workspace via `access_policy`, wiki-export prefix only |
| Connector credentials & security | [05](05-admin-backend-and-maintenance.md) §7, [06](06-api-mcp-and-scaling.md) §3, §4 above | Secret lives in an external secrets manager, referenced by `credential_ref`; connector is a `connector:<id>` principal with `contributor` on exactly one workspace; config changes audit to `admin_action_log`, runs to `ingestion_log` |
| API conventions | [06](06-api-mcp-and-scaling.md) §1 | Cursor pagination; one error envelope keyed by a stable `type`; `Idempotency-Key` on submit/resolve; partial search flagged explicitly; `RateLimit-*` headers |
| Baseline auth scope for Phase 1 | [06](06-api-mcp-and-scaling.md) §3, `phase1-tasklist.md` | Authorization (`access_policy` + three roles) ships in Phase 1; authentication is a pluggable provider, trusted-header first and OIDC/SAML in Phase 2 |
| Near-duplicate similarity metric | [02](02-storage-and-indexing.md) §4, [03](03-ingestion-and-review-workflows.md) §4, §6 below | Lexeme containment over the FTS index, not `ts_rank` (unbounded, length-dependent); `near_duplicate_score` recalibrated from 0.85 to 0.60 against measured scores |
| Placeholder source page timing | [03](03-ingestion-and-review-workflows.md) §1 | Created once classification resolves the workspace, not literally at `submitted` — no `wiki_page` row is legal to write before then; UI labels are derived from `pipeline_state` at read time, never stored as page content |
| Review item workspace timing | [02](02-storage-and-indexing.md) §3, [03](03-ingestion-and-review-workflows.md) §3, §5 | `workspace_id` nullable, unlike the placeholder page — `submission`/`classification` items are created at their literal spec-stated moments, before a workspace may exist |
| LLM model selection | [00](00-overview.md) §3, [08](08-implementation-stack.md) §2 | Configurable per agent role: workspace `SCHEMA.md` override, else platform default. One Pydantic AI `provider:model` string; `openai:gpt-5-nano` in every environment, cost-first with curation quality as the watched risk; credentials stay in the secrets manager |

## 3. Pipeline-State Storage

`03` §1 defines a 9-state ingestion pipeline (`submitted → classifying → classified →
duplicate_check → ... → ingested|error|rejected`). `02` §3's `raw_source.status`
(`active|superseded|archived|rejected`) is a *lifecycle/retention* axis — a different concept —
so the pipeline-progress axis needs its own home.

**Decision**: add `raw_source.pipeline_state` (the 9-value enum from `03` §1), maintained as a
denormalized "current state" pointer updated atomically alongside each `ingestion_log` append
(`02` §5) — the same "current pointer + full append-only history" pattern `02` §3 already uses
for `wiki_page.current_version_id` / `page_version`. `ingestion_log` remains the system of record
for history; `pipeline_state` is what `06` §1's `sources/{id}` "get status" reads directly,
avoiding a log-table scan on a frequently-polled endpoint (`06` §2: `wiki_get_source_status`
"typically polled after `wiki_submit`").

With this field, `03` §6 step 7 reads as: *set `raw_source.pipeline_state = ingested`;
`raw_source.status` remains `active`* — consistent with the wording already fixed in `03` §6.

**Spec touch-point** (applied): `02` §3's `raw_source` row now includes `pipeline_state`
(9-value enum, `03` §1).

## 4. Connector Execution Model

`03` §2 says connectors "discover new/changed content and submit it the same way" as end users;
`05` §7 covers *configuring* connectors (schedule, credentials, ingestion policy) but not *how a
run executes*.

**Decision**: connectors get their own lightweight, IO-bound worker pool — "connector polling" —
alongside the existing per-job-type pools (`06` §4: classification, curation/ingest, indexing,
maintenance advisor). On each scheduled run (per-connector interval, `05` §7), the pool:

1. Fetches the connector's current state listing (commit SHAs, page IDs + modified timestamps,
   etc. — connector-type-specific).
2. Diffs against the last-sync state stored for that connector (a small per-connector cursor,
   not a new core table).
3. For each new/changed item, creates a `raw_source` record (`submitted`,
   `submitted_by=connector:<connector_id>`) exactly as if a user uploaded it.

From step 3 onward the item is indistinguishable from any other submission — classification,
dedup, and ingestion (`03` §3–7) proceed unchanged. This keeps "connectors are just another
submission source" (`03` §2) literally true at the execution level, and requires no new pipeline
states or review-item kinds.

Polling vs. webhook: polling is the default (matches `05` §7's "schedule/refresh interval").
Webhook-triggered connectors (e.g., a Git push webhook) are an additive optimization that
short-circuits step 1–2 for that one item — same step 3 onward.

**Roadmap note** (applied): `07` §6 Phase 2 now includes a "Connector framework" deliverable,
sequenced after "Multi-workspace + taxonomy routing" since connector-routed content depends on the
taxonomy being in place.

## 5. MCP On-Behalf-Of Delegation

`06` §2 says `wiki_submit` "lets an agent submit a document on a user's behalf," but `06` §3's
auth table only covers direct principals (end user via SSO, API/MCP client via API
key/OAuth client-credentials) — not delegation.

**Decision**: an on-behalf-of `wiki_submit` call carries two identities:

- **The calling agent's own credential** — authenticated normally (API key / OAuth
  client-credentials, `06` §3).
- **An `acting_as: user:<id>` claim** identifying the end user being represented.

AuthZ requires **both**: the agent's own grant must include `contributor` on the target
workspace, *and* the `acting_as` user must independently have `contributor` on that workspace —
whichever is more restrictive applies. This prevents an agent from using its own broader access
to submit "as" a user who couldn't have submitted there themselves.

On success: `raw_source.submitted_by = user:<id>` (the represented user — `02` §3's existing
enum, unchanged) and any resulting `page_version.author = user:<id>` (`01` §5, unchanged). The
calling agent's own identity is recorded in the `ingestion_log` entry's metadata for audit —
no new core field required.

**Spec touch-point** (applied): no changes to `01`/`02`'s enums; `06` §3 now includes a one-line
note describing this dual-identity check.

## 6. `SCHEMA.md` Example Template

`01` §7 describes `SCHEMA.md`'s contents conceptually but gives no example or starting defaults.
Below is an illustrative template — starting points to tune per workspace, not mandated values:

```yaml
workspace_id: eng-docs
document_types:
  - eng.design-doc
  - eng.runbook

page_conventions:
  required_tags_min: 2          # global default per 01 §6
  additional_required_tags: [team]   # workspace-specific addition

curator:
  tone: concise, engineering-focused
  concept_vs_entity: >
    entity = a named system, service, tool, or vendor;
    concept = a pattern, practice, or methodology discussed across sources.

ingestion_policy: gated         # auto | gated, per 03 §7

llm:                            # optional — omit to inherit the platform default (§16)
  classifier:
    model: openai:<model-id>    # Pydantic AI model string: provider:model
  curator:
    model: openai:<model-id>

thresholds:
  staleness:
    high_traffic_days: 90       # re-check sooner for frequently-queried pages (05 §2)
    low_traffic_days: 365
  classification:
    min_confidence: 0.75        # below this -> classification review item (03 §3, §9 above)
  dedup:
    near_duplicate_score: 0.60  # lexeme containment, 0-1 (03 §4; see §17)
  orphan:
    query_log_lookback_days: 90 # zero appearances in this window -> prune candidate (05 §2)

retention:
  superseded_source_days: 180   # before Superseded-Source Detector proposes prune (05 §2)
  page_version_max_count: 50    # before older versions become prune candidates (01 §5)
```

## 7. `diff_ref` Format

`01` §5 and `02` §3 leave `diff_ref`'s format unspecified.

**Decision**: `diff_ref` is a path into the Object Store — `/{workspace_id}/diffs/{version_id}.diff`
— containing a unified diff against the previous version, computed once and written by the Wiki
Service at the same time it writes the new `page_version` row. `pages/{id}/versions/diff` (`06`
§1) streams this stored blob rather than recomputing on every read — important for
frequently-viewed history on pages with many versions.

Alternative considered (compute-on-read from two `page_version.content` blobs): no extra storage,
but pays the diff cost on every `versions/diff` call, growing with version count. Write-once
avoids this at the cost of one small object per version.

**Spec touch-point** (applied): `02` §1's Object Store row now lists "page-version diffs"; `02` §2
documents the `/{workspace_id}/diffs/{version_id}.diff` path scheme; `02` §3's `diff_ref` now notes
its type as an object-store path.

## 8. Query-Log & Threshold Retention Defaults

`02` §5 describes `query_log` as "(anonymized per policy)" without defining the policy; `05` §2's
Orphan/Low-Traffic Detector and Superseded-Source Detector key off "the workspace's lookback
window" and "retention window" respectively, also unspecified. §6 above gave
`retention.superseded_source_days: 180` as an illustrative default.

**Decision**:
- `query_log` entries are retained in full detail (query text, principal, resolved workspaces,
  returned page IDs/scores) for **90 days**, then purged — the retention window is itself the
  privacy boundary; no separate anonymization step.
- Orphan/Low-Traffic Detector lookback window: **90 days** (`SCHEMA.md`
  `thresholds.orphan.query_log_lookback_days`, §6 above), within the `query_log` retention window.
- Superseded-Source retention window: **180 days** — confirms §6's illustrative
  `retention.superseded_source_days` as the actual default.

**Spec touch-point** (applied): `02` §5's `query_log` row and surrounding text now state the
retention policy; §6 above's `SCHEMA.md` template gains `thresholds.orphan.query_log_lookback_days`.

## 9. Classifier Confidence Calibration

`03` §1/§3 gate routing on "confidence ≥ the workspace's configured threshold" without specifying
how confidence is produced — relevant because `00` §3 keeps the LLM provider swappable, and
self-reported LLM confidence is notoriously poorly calibrated.

**Decision**: confidence is **self-reported** by the Classifier as part of its structured output
(`08`'s Pydantic AI `ClassificationResult` model) — provider-agnostic, no dependency on
provider-specific log-prob APIs.

**Paired with an independent signal at decision time.** Self-reported confidence alone is weak, and
the recalibration below only pays off once enough admin resolutions have accumulated — which is
exactly when a new deployment has none. `03` §3's routing gate therefore requires the LLM's label
to agree with a deterministic lexical taxonomy match before routing, and sends a disagreement to a
`classification` review item whatever the self-reported score claims. That signal is independent of
the model, costs a static-table lookup, and works from the first document rather than the
hundredth. It is also what makes a cheaper model a defensible choice (§16): the gate's quality no
longer rests solely on the model's own estimate of itself.

This score is **periodically recalibrated**: admin resolutions of
`classification` review items (`03` §3's "confirm the top suggestion, pick a different type") are
ground truth — if admins consistently override a particular confidence band, the workspace's
threshold (`SCHEMA.md`) is adjusted accordingly. No new pipeline state or review-item kind:
recalibration is an offline analysis of existing `review_item.resolved_action` data
([02](02-storage-and-indexing.md) §3), producing a `SCHEMA.md` config change (itself an
`admin_action_log` entry, [02](02-storage-and-indexing.md) §5).

**Alternative considered**: a secondary scoring pass (separate model/heuristic scores each
classification) — more consistent, but doubles classification cost per source. Log-prob-based
confidence — not universally exposed across LLM providers, would violate `00` §3's
provider-neutrality.

**Spec touch-point** (applied): `03` §3 step 6 now notes the calibration loop.

## 10. Relevance Testing / Regression Process

`04` §3–4 defines ranking/boosting behavior (catalog-match boost, cross-backend score
normalization, tie-breaks) but no process for validating that a change to this logic is an
improvement.

**Decision**: no dedicated golden-query test set initially. `07` §4's **Search result feedback
loop** (thumbs-up/down per result, recorded alongside `query_log`) is the relevance-regression
signal — a ranking/boosting change is monitored via this feedback's trend for affected query
patterns before and after the change, rather than a separate offline test suite.

**Alternative considered**: a maintained golden query set (shared or per-workspace) — more
rigorous, but adds an ongoing-ownership burden ahead of having real usage data to build the set
from. Revisit once feedback-loop data exists, or once a dedicated-index workspace's cross-backend
score normalization ([04](04-search-and-retrieval.md) §4, [08](08-implementation-stack.md) §3)
needs empirical validation against real rankings.

**Spec touch-point** (applied): `07` §4's "Search result feedback loop" row now notes this dual
purpose.

## 11. Taxonomy Bulk-Move Execution Model

`05` §7 names a bulk "move workspace" admin action (re-homing pages/sources after a document-type
taxonomy change) but doesn't define batch limits, preview, or partial-failure handling.

**Decision**: dry-run preview, then batched execute. The admin first previews the affected
page/source count and list (no writes); on confirmation, the move executes in batches, each page
re-homed via its `wiki_page.workspace_id`/`raw_source.workspace_id` update plus a `page_version`
with `trigger=manual_edit` ([01](01-architecture-and-data-model.md) §5) — with per-batch progress
visible in the Admin Console. A failed batch halts the operation; already-completed batches are
not rolled back (each page move is independently valid and versioned) — the admin resumes or
retries the remaining batches.

**Alternative considered**: all-or-nothing transactional move — simpler mental model, but
impractical at the page counts [06](06-api-mcp-and-scaling.md) §6 now targets (5,000–50,000
pages/workspace) and conflicts with the partitioned, append-only versioning model
([02](02-storage-and-indexing.md) §3).

**Spec touch-point** (applied): `05` §7's taxonomy row now describes the dry-run + batched
execution model.

## 12. FUSE-Mount Access Scope

`02` §2 and `08` §3 describe FUSE-mounting an fsspec backend for "file-based agent access" to the
wiki export, without defining who gets this access or whether it's read-only.

**Decision**: read-only, opt-in per workspace via `access_policy`
([02](02-storage-and-indexing.md) §3) — a workspace admin explicitly grants a principal FUSE
access (not automatic for every existing `reader`/`contributor`). The mount exposes only the
regenerated wiki markdown export (`/{workspace_id}/wiki/...`, [02](02-storage-and-indexing.md)
§2) — never the `sources/`, `diffs/`, or `assets/` prefixes — and never write access; content
changes continue to go through the Wiki Service (gateway-mediated, `06`), preserving "every wiki
page write creates a new version" ([00](00-overview.md) §2 Principle 9).

**Spec touch-point** (applied): `02` §2's "File-based agent access" bullet now states this scope.

## 13. Connector Credential & Security Model

§4 above defines *how* a connector run executes, and `05` §7 lists "credentials" as part of
connector configuration — but neither says where a credential lives, how it rotates, what a
connector may do once authenticated, or what its actions leave behind for audit.

**Decision**: a connector is an ordinary Platform principal whose secret is held outside the
Platform.

**Credential storage.** Connector secrets are never stored in the Metadata DB, `SCHEMA.md`, the
Object Store, or any log stream. A `connector` row (`02` §3) holds a `credential_ref` — an opaque
pointer into the deployment's external secrets manager (a role, not a product: Vault, AWS Secrets
Manager, GCP Secret Manager, Kubernetes secrets) — resolved by the connector polling worker (§4)
at the start of each run and held in memory for that run only. The `connectors` API (`06` §1)
accepts a secret write-only on configure/update and never returns it; reads return the
`credential_ref` plus non-sensitive metadata (last rotated, expiry, last auth outcome).

**Rotation.** Rotation happens in the secrets manager, not the Platform: the `credential_ref` is
stable across rotations, so no Platform-side record changes and no scheduled run is interrupted. A
run whose credential fails to authenticate does **not** retry on the normal schedule — the
connector moves to `disabled_auth` and surfaces via the Admin Console's operational health
(`05` §8) and the Notification Service (`07`), because a connector retrying an expired credential
on a poll interval is the usual way an integration account gets locked out at the source system.
An admin re-enables it once the secret is fixed. Optional `credential_expires_at` metadata lets the
same channel warn ahead of expiry rather than after failure.

**Secret scope.** One credential per connector, scoped to that connector's single target
workspace — two connectors against the same source system get separate refs, so revoking one never
disables the other. The credential should be the least-privileged read-only account the source
system offers: connectors only ever *read* from the source and *write* into the Platform.

**Permission boundary.** `connector:<connector_id>` is a principal in `access_policy` (`02` §3,
`06` §3) exactly like a user or API client, granted `contributor` on exactly one workspace — never
`admin`, never several workspaces, never global. That grant permits only what `contributor`
already permits: create a `raw_source` (`03` §2). A connector cannot resolve review items, edit or
roll back pages, read `query_log`, or configure connectors, and it cannot route content outside
its own workspace. Connector-submitted sources obey that workspace's `ingestion_policy`
(`auto`/`gated`, `03` §7) like any other submission; a connector's own configured policy (`05` §7)
may only *tighten* that to `gated`, never relax a `gated` workspace to `auto` — a connector is not
a trusted bypass. This mirrors §5's rule for delegated submission: no principal borrows privilege
by routing through another.

**Audit.** No new log stream is required:

- *Configuration* — create/update/disable and each rotation event (the fact and timestamp, never
  the value) append to `admin_action_log` (`02` §5) with the acting admin.
- *Runs* — each run appends an `ingestion_log` entry carrying connector id, run id, auth outcome,
  items discovered, and `raw_source`s created, alongside the per-source state transitions that
  stream already records.
- *Items* — attribution already exists: `raw_source.submitted_by = connector:<connector_id>`
  (`02` §3, unchanged), so every wiki page traces back to the connector that fetched its source.

**Alternative considered**: encrypted credentials in the Metadata DB (an `encrypted_credential`
column plus a platform-managed key). Fewer moving parts for a small deployment, but it makes the
Platform a secrets custodian — key management, rotation tooling, and an exfiltration surface it
otherwise doesn't have — and enterprise deployments of the kind `00` §3 targets already run a
secrets manager.

**Spec touch-point** (applied): `02` §3 gains a `connector` table (§4's "small per-connector
cursor" is its `last_sync_cursor` field, still not a table of its own) and notes
`connector:<connector_id>` as an `access_policy` principal; `05` §7's connector row points here
for the credential/security model; `06` §3's principal table gains a Connector row.

## 14. API Conventions

`06` §1 defines the resource/operation table but not the cross-cutting mechanics every endpoint
needs. `techfeasibility.md` §3 deferred these to "the API design phase" — Phase 1 reaches them at
its first endpoint (`phase1-tasklist.md` step 7), so they are settled here.

**Pagination.** Cursor-based, not offset, for append-heavy content tables. List endpoints over
`page_version`, `raw_source`, `review_item`, and the log streams accept `limit` (default 50, max
200) and an opaque `cursor`, and return `{"items": [...], "next_cursor": <string|null>}`. The
cursor encodes the sort key plus a tiebreak id. Offset pagination is wrong for this data: these
tables are append-heavy and partitioned (`02` §3), so rows inserted mid-scan shift every later
offset and a paging admin silently skips items.

Five endpoints are a deliberate, documented exception to the contract above — a plain, capped
`limit` (same constants, no `cursor`/`next_cursor`) rather than real cursor pagination:
`GET /search` (`04` §4, `09` §28 — a ranked query, not a catalog crawl, so "page 2 of the same
ranking" isn't the same problem cursor pagination solves), and `GET /document-types`,
`GET /connectors`, `GET /workspaces`, `GET /workspaces/{id}/access-policy` (`09` §71,
phase3-tasklist.md step 66 — deployment-*configuration* cardinality, not append-heavy content;
none of the four backing tables even has a `created_at` column, and none is expected to need one).

**Error responses.** One shape, on every non-2xx:

```json
{"error": {"type": "invalid_request", "message": "human-readable summary",
           "detail": {"field": "page_type", "reason": "not a valid page type"},
           "request_id": "req_01H..."}}
```

`type` is a stable machine-readable slug — `invalid_request` (400), `unauthenticated` (401),
`forbidden` (403), `not_found` (404), `conflict` (409), `rate_limited` (429), `internal` (500) —
and callers branch on it, never on `message`. `detail` is optional and shape-varies by `type`.
Every response, success or failure, carries the `request_id` as a header so a user-reported failure
maps to a log line.

**Idempotency.** `sources` submit and `review-items/{id}/resolve` (`06` §1) accept an
`Idempotency-Key` header. The gateway stores `(key, principal, endpoint)` with the response for 24
hours; a replay returns the stored response rather than re-executing. This is what makes a client
retry after a timeout safe — without it, a retried submit creates a second `raw_source` and a
second ingestion run. Distinct from content-hash duplicate detection (`02` §3, `03` §4): dedup asks
"is this the same *document*", idempotency asks "is this the same *request*", and a resubmission of
identical content by design still creates a new `raw_source` that dedup then flags.

**Partial failure.** `search` fans out across workspaces (`04` §4), so one unavailable index must
not fail the whole query. A partially-served search returns 200 with the results it has plus
`"partial": true` and `"unavailable": [<workspace_id>, ...]`. The flag is mandatory, not advisory —
a caller that renders partial results as complete is the failure this prevents. Callers needing
all-or-nothing check `partial` and retry. Single-workspace operations have no partial state and
never carry the field.

**Rate limiting.** The gateway's limiter (`01` §2, `07` §3) returns `RateLimit-Limit`,
`RateLimit-Remaining`, and `RateLimit-Reset` on every response, and a 429 carries `Retry-After` in
seconds alongside the standard error body (`type: "rate_limited"`).

**Spec touch-point**: none — these are contract details below the level `00`–`07` specify, recorded
here so the implementation doesn't invent five inconsistent answers.

## 15. Baseline Auth Scope for Phase 1

`06` §3 defines the auth model, but `07` §6's Phase 1 says only "basic admin console" and
`phase1-tasklist.md` originally carried no auth step at all — while steps 19–20 build an admin
console that presupposes an admin role. Building the gateway first and retrofitting authorization
is the expensive order, so the scope needs settling before step 7.

**Decision**: split authentication from authorization, and implement them on different schedules.

- **Authorization is Phase 1.** The `access_policy` table (`02` §3), the three roles from `06` §3
  (`reader`, `contributor`, `admin`), and a gateway check on every endpoint against the caller
  column in `06` §1's table. This is the part that is expensive to retrofit: it determines the
  shape of every handler signature, so it goes in with the first endpoint.
- **Authentication is pluggable, and Phase 1 ships the trivial provider.** The gateway resolves a
  principal through a small interface with one method — request in, principal out. Phase 1 ships a
  trusted-header provider for single-tenant/dev deployment; the OIDC/SAML provider (`06` §3)
  lands in Phase 2 as a second implementation, with no change to any handler. This is what lets
  Phase 1 proceed without waiting on the organization's IdP.

**Explicitly out of Phase 1**: fine-grained per-page-type permissions (a roadmap item already,
`06` §3, `07`), connector principals (§13 — connectors are Phase 2), and MCP on-behalf-of
delegation (§5 — it needs the MCP surface, also Phase 2).

**Alternative considered**: declare Phase 1 fully trusted with no `access_policy` at all. Fewer
moving parts, but every endpoint written in that phase then hard-codes "the caller may do
anything," and steps 19–20's admin console has no way to be an admin — the retrofit would touch
every handler built in 1b and 1c.

**Spec touch-point** (applied): `phase1-tasklist.md` step 2 gains the `access_policy` table, and
step 7 — the first endpoint — gains principal resolution and role enforcement as the gateway's
cross-cutting concern rather than a separate later step.

## 16. LLM Model Selection and Configuration

`00` §3 puts LLM provider selection out of scope and `08` §2 picks Pydantic AI as the agent
framework without naming a model — leaving the Classifier (`03` §3) and Curator (`03` §6) with no
concrete model to run against, and no defined place to put one.

**Decision**: the model is a **configuration value**, resolved per agent role — never a code
dependency. The adopting organization's provider is **OpenAI**, and the selected model for both
roles, in every environment including production, is **`openai:gpt-5-nano`** — the provider's
lowest-cost tier.

### Resolution order

For each of the two agent roles independently, the effective model is the first of:

1. **Workspace override** — `llm.classifier.model` / `llm.curator.model` in that workspace's
   `SCHEMA.md` (§6 above). Absent by default.
2. **Platform default** — deployment configuration (`08` §4), one setting per role.

The value is a single Pydantic AI model string carrying provider and model together
(`openai:<model-id>`), so switching provider is the same operation as switching model and no code
path branches on provider. That is what keeps `00` §3's neutrality real rather than nominal: the
Platform has no OpenAI-specific code, only an OpenAI-specific configuration value.

Splitting the two roles matters because their economics differ — the Classifier runs once per
source over a short summary with a constrained structured output, while the Curator writes whole
pages. An organization that later wants a cheaper classifier can change one config value without
touching curation.

### Why the lowest tier, and what it trades

This is a deliberate cost-first choice. At `06` §6's 10–100 documents/hour, both agents together
run roughly $80/month at this tier against roughly $2,000/month at the flagship — a ~25× difference
on a workload whose value is not yet demonstrated. Running the same model in development and
production is a second, smaller benefit: the confidence threshold calibrated during development
(§9) carries into production instead of being discarded, and there is no class of defect that
appears only after promotion.

The risk this accepts is concentrated in **curation, not classification**. Classification is a
constrained pick-one-label task over a short summary, and it is already backstopped: low confidence
routes to a `classification` review item (`03` §3 step 6) and an admin corrects it, so a weak
classifier degrades into review-queue volume rather than silent error. Curation has no equivalent
backstop — the Curator's output *is* the product, the wiki pages users read and cite, and a
thin or inaccurate page is not obviously wrong to the reviewer approving it.

**Signals that should trigger revisiting the curator's model**, in rough order of how early they
appear:

- `classification` review items running persistently high after §9's calibration has converged —
  the cheap signal, and the one that arrives first.
- `duplicate` review items where the Curator's near-duplicate judgment (`03` §4) disagrees with the
  admin's resolution more often than not.
- Curated pages being edited by hand shortly after ingest — visible as `page_version` rows with
  `trigger=manual_edit` close behind an `ingest` version for the same page. This is the direct
  measure of curation quality and needs no new instrumentation.

Because the model is configuration, acting on any of these is a `SCHEMA.md` edit for one workspace
or a change to one deployment setting — not a migration. Raising the curator's tier while leaving
the classifier at this one is the natural first step, and the per-role split exists to make it a
one-value change.

### What is *not* configurable here

**Credentials never live in `SCHEMA.md`.** `SCHEMA.md` is versioned, admin-editable, and part of
the wiki export (`02` §2) — a workspace-editable model name belongs there, a secret does not. Nor
does the key belong in the Platform's own configuration: the OpenAI SDK and Pydantic AI read
`OPENAI_API_KEY` from the environment themselves, so the Platform never reads, holds, or logs it,
and `config.py` deliberately has no setting for it. Where the variable comes from is per
environment:

| Environment | Source of `OPENAI_API_KEY` |
|---|---|
| Local development | A gitignored `.env` file, from the committed `.env.example` template |
| Deployment | Injected as an environment variable by the secrets manager at container start, the same custody model §13 defines for connector credentials |

This is the one credential the Platform holds on its own behalf rather than a workspace's, so it is
a single deployment-wide secret rather than a per-workspace `credential_ref` row. Rotating it is a
secrets-manager operation and a restart; no Platform record changes.

### Consequences of making it configurable

- **A model change resets that workspace's confidence calibration.** §9 tunes
  `thresholds.classification.min_confidence` against admin resolutions from a *specific* model's
  self-reported confidence; a different model reports on a different scale. Changing
  `llm.classifier.model` therefore invalidates the tuned threshold, and the calibration window
  restarts from the change. Because `SCHEMA.md` is versioned and its edits land in
  `admin_action_log` (`05` §6, `02` §5), the change is already dated — the calibration analysis
  reads that date rather than needing a new field.
- **Prompts must be ordered stable-prefix-first regardless of model.** Both agents send a large
  stable prefix and a small variable suffix — the Classifier its system prompt plus the
  document-type taxonomy (`01` §3), the Curator its `SCHEMA.md` rules and tone (`01` §7, §6 above)
  — with the source document last. Prompt caching is prefix-based across providers, so the stable
  part must physically precede the variable part; ordered that way the per-workspace prefix is
  reused across every document in a batch. The mechanism differs by provider (some cache
  automatically above a size threshold, others require explicit markers) but the ordering
  requirement does not, which is why it belongs in the provider-neutral prompt construction rather
  than in provider-specific code.
- **Size the output-token ceiling for reasoning plus visible output.** On reasoning-capable models
  the reasoning tokens are billed as output and count against the same cap, so a ceiling sized only
  for the expected `ClassificationResult` truncates. This is per-model, so it belongs alongside the
  model in configuration rather than as a constant.
- **Confidence stays self-reported** (§9). That decision was taken to avoid depending on
  provider-specific log-probability APIs, and it is what makes the model swappable without
  reworking `03` §3's routing gate.

**Spec touch-point** (applied): §6 above's `SCHEMA.md` template gains an optional `llm:` block;
`08` §2's LLM-layer row and §3's notes record the resolution order and the prompt-ordering
requirement.

## 17. Near-Duplicate Similarity Metric

`03` §4 and `02` §4 specify a "more like this" similarity query and a `SCHEMA.md` threshold above
which a match blocks, but not what the score *is*. §6's template shipped
`near_duplicate_score: 0.85` as an illustrative value with no metric behind it. Since the number
gates a correctness check, it needs both.

**Decision**: the score is **lexeme containment** — the fraction of the candidate text's distinct
lexemes that appear in the page — computed over the Full-Text Index, with a `tsquery` prefilter so
the GIN index still does the selection.

Ranking functions are the wrong tool for this. `ts_rank`/`ts_rank_cd` are unbounded and scale with
document length, so they cannot be compared against a fixed threshold. Normalising a page's rank
against the candidate's own self-rank does not fix it either: a longer page routinely out-ranks a
short candidate on the candidate's own query, so the ratio saturates and *every* result clamps to
1.0 — identical text becomes indistinguishable from merely-related text. Containment is bounded by
construction and degrades smoothly.

Containment rather than Jaccard because `03` §4 runs the Classifier's **summary** against full page
text. The summary is legitimately far shorter, and Jaccard would score that length asymmetry as
dissimilarity rather than measuring the thing we care about — whether the page already covers what
this document says.

**Measured against a single indexed page**, varying only the probe:

| Probe | Containment |
|---|---|
| Identical text | 1.00 |
| Light paraphrase | 0.67 |
| Same topic, different content | 0.43 |
| Heavier paraphrase | 0.36 |
| Unrelated document | 0.11 |

**The 0.85 default was therefore wrong for this metric** — it would have caught only near-identical
text and let a paraphrase of the same document through, which is precisely the case duplicate
detection exists for. §6's template now ships `0.60`, sitting in the gap between a light paraphrase
(a real duplicate, 0.67) and a same-topic document (not one, 0.43), with margin on both sides.

Two caveats worth carrying: the ordering between "same topic" and "heavier paraphrase" inverts,
so the metric ranks confidently only at the ends of its range — which is all a threshold needs; and
these figures come from short probes against one page. Re-measure against real content before
tuning a workspace away from the default, and note that `03` §4 gives the Curator the final
near-duplicate judgement precisely because a lexical score is a candidate generator, not a verdict.

**Spec touch-point** (applied): §6 above's `SCHEMA.md` template now reads
`near_duplicate_score: 0.60` and names the metric.

## 18. Placeholder Source Page Timing

`03` §1 says the placeholder `source` page is created "immediately" at `submitted`, so that "the
document is 'in the wiki'" from the moment of upload. Implementing this literally isn't possible:
a `wiki_page` row needs `workspace_id` as its partition key (`01` §3) and required frontmatter
including `workspace_id` (`01` §6), and neither exists until classification resolves the workspace
— which for a submission `03` §2 deliberately accepts in the target-undetermined state.

**Decision**: the placeholder is created once classification resolves the workspace — the first
moment a `wiki_page` row is legal to write — not literally at `submitted`. Concretely, this lands
right before the `classifying → classified` transition, using the same title/description/tags
every time (`status: draft`, tagged `processing`) so the write carries no information beyond "this
exists and is in progress." The window this leaves uncovered is `submitted` and the portion of
`classifying` before the workspace resolves — sub-second in the normal case, and genuinely without
a page in the two cases where classification never resolves a workspace at all (a low-confidence or
cross-check-disagreement routing straight to `pending_review`, `03` §3). In both, `raw_source` and
`GET /sources/{id}` (`06` §1) remain the visibility mechanism — the caller can always see
`pipeline_state`, just not yet as a wiki page.

**Alternatives considered**: a real workspace-less placeholder, making `wiki_page.workspace_id`
nullable (mirroring `raw_source.workspace_id`, `09` §3) and relaxing required frontmatter to permit
a missing `workspace_id`. Rejected — it loosens two schema invariants everywhere to serve a
sub-second window with no title, no workspace, and nothing meaningful to show. Also considered:
no `wiki_page` row until `ingested`, with visibility satisfied entirely by the status endpoint.
Rejected as a bigger departure from `03` §1's stated table, which has every state (bar the
uncovered window above) showing a "placeholder `source` page."

**UI labels are derived, not stored.** `03` §1's "processing" / "awaiting review" / "rejected" /
"error" markings are explicitly *not* the page's frontmatter `status` (the note already in this
section says as much) — they are computed from `pipeline_state` by a pure mapping
(`PipelineState → label`) at read time, not written as page content on every transition. Writing a
new `page_version` each time the label changes would churn `index_status` to `stale` on every
transition (`02` §7) for content that never actually changed, and would duplicate `pipeline_state`,
which is already `06` §1's authoritative read path (`09` §3). The placeholder gets exactly two real
content writes: creation (draft, generic body) and finalization — either the Curator's content on
`ingested`, or a rejection notice carrying the admin's reason on `rejected`.

**Creation must be idempotent, not create-once.** `03` §1 allows `pending_review → classifying`
(an admin retrying a failed classification), so a second successful classification for the same
source hits this same code path again. The placeholder write finds-or-creates by
`(workspace_id, path)` and updates in place rather than colliding with the first attempt.

**Spec touch-point** (applied): `03` §1's table row for `submitted` no longer says "created
immediately" — it now describes the placeholder as created once classification resolves the
workspace, with a forward pointer here. A new note states the uncovered window and that
`raw_source`/the status endpoint cover it.

## 19. Review Item Workspace Timing

`02` §3 lists `workspace_id` as a `review_item` field with no nullability noted, and `03` §5 says
the `submission` review item is created "the moment the `raw_source` record exists (state
`submitted`)" — before classification has run. Both `submission` and `classification` items
(`03` §3, §5) can therefore need to exist before a workspace is known, the same timing conflict
§18 resolved for the placeholder page.

**Decision**: unlike §18, don't defer creation — make `review_item.workspace_id` nullable and
create these items at their literal spec-stated moments, leaving `workspace_id` unset until (if
ever) one resolves.

This is a deliberate departure from §18's resolution of the same-shaped conflict, not an
inconsistency: the two artifacts differ in what pre-workspace existence is *for*. The placeholder
page is workspace-partitioned wiki content — before a workspace exists there is nothing coherent to
show, so deferring costs nothing. A review item is an admin task-queue entry, and `03` §5 states
its purpose includes letting an admin "reassign workspace... before `ingesting` completes" —
deferring creation until a workspace is resolved would silently remove the one capability the
spec names first. For `classification` items specifically, a workspace may never resolve
automatically at all: resolving the item — an admin picking a `document_type` (`03` §3) — is
what assigns one. Nullable-with-optional-backfill isn't a new pattern here; it's the same shape
`raw_source.workspace_id` already uses (`09` §3), applied to a second table for the same reason.

`duplicate` items don't share this conflict — `03` §4 runs `duplicate_check` only after a
workspace is resolved, so they always carry one at creation.

**What does *not* get a review item.** `03` §7: a `gated` workspace with no duplicate concerns
still parks at `pending_review`, but this is policy gating, not a finding — no dedicated item kind
exists for it, and none is created. The always-open `submission` item, already created at
`submitted`, is what an admin uses to notice and act on that source; inventing a second item would
duplicate what the first already covers.

**Spec touch-point** (applied): `02` §3's `review_item` row now marks `workspace_id` nullable; `03`
§5 notes it may be unset at creation.

## 20. Catalog-Match Boost Without an `index.md` Page

`04` §3 defines the catalog-match boost as: each workspace's `index.md` catalog (one-line
summaries per page, `01` §4) is itself indexed, and a query matching a page's catalog entry ranks
that page higher. But no code builds an actual `index.md` wiki page — flagged as an accepted gap
under phase1-tasklist.md step 16, deliberately carried forward to "when 1c or search needs it."
Step 17 is that moment.

**Decision**: realize the boost via `page_index`'s existing weight tiers rather than a separate
catalog page and join. Every page already carries `description` — a required one-sentence summary
(`01` §6) — which *is* the content an `index.md` entry would hold for that page (`01` §4: "one-line
summaries per page"). `index_page` now indexes it as its own tier, between title (`'A'`) and body
(`'D'`): `title 'A' | description 'B' | body 'D'`. A query matching the summary ranks the page above
one that only matches in body text — the ranking effect `04` §3 describes — falling out of
Postgres's own weighted `ts_rank_cd`, no separate catalog page, no match-to-target-page join.

**Alternative considered**: build the real `index.md` page now (curate.py maintains one per
workspace; search indexes it and maps a matched line back to its target page). Matches `04` §3
literally, but needs markdown-link parsing to associate a matched catalog line with a page —
`page_link` parsing, the other gap flagged alongside this one — and reaches into curation, past
what a search-only step needs. Deferred; the gap stands, now scoped specifically to "no browsable
catalog page exists," not "no catalog-match boost."

**Consequence**: pages indexed before this change won't get the description-tier boost until
reindexed (`index_page` fully replaces a page's row, so any future reindex — step 18's lifecycle,
once it lands — picks it up automatically; no backfill migration was written since Phase 1 has no
production data yet).

**Spec touch-point**: none — `04` §3 already described the desired ranking behavior; this note
records how it's realized without the literal `index.md` page the prose assumes.

## 21. Indexing Lifecycle: Explicit, Not Dispatched

`02` §7's state diagram marks `stale -> indexing` "always automatic," and `tasks.py`'s `indexing`
Celery queue has stood empty since step 5 with a comment that its tasks "arrive in 1b and 1c" —
1c is where step 18 lands. So implementing the lifecycle raised the same question every prior
pipeline stage (classification, curation, dedup) already answered the same way: real Celery
dispatch is a deliberately deferred, separate piece of work (the "async job wiring" gap
`phase1-tasklist.md`'s accepted-simplifications note names, and what install/scaling docs are
gated on).

**Decision**: `search.py` gets the full state machine as plain functions —
`reindex(session, page_id)` runs one page through `pending`/`stale -> indexing -> indexed`/`error`;
`reindex_pending(session)` sweeps `pending_pages()` through it; `retry_errored(session)` moves
`error` back to `pending`. Nothing calls these automatically — not from `versioning.create_page`/
`write_version`, not from `tasks.py`. A page written via the normal ingestion path sits at
`pending` until a test, an admin action, or (later) a real worker calls `reindex_pending`. This
keeps every stage in the same state — explicit-call, no real async — rather than closing the gap
for indexing alone while classification/curation/dedup stay as they are, which would leave the
codebase's async story split without a good reason. Consequently, the "async job wiring" gap and
the docs-gating decision it drives (`09` — see the note on install/scaling docs) are unchanged by
this step; indexing's lifecycle is just one more thing that gap will need to pick up once tackled.

**Alternative considered**: wire `karpwiki.indexing.reindex_page` as a real Celery task, dispatched
from `create_page`/`write_version`. Matches `02` §7 literally and finally exercises the queue, but
is materially more scope (retry/idempotency semantics, an eager-mode test harness) for one stage,
and reopens a decision (deferring async wiring) the user had already settled for the rest of the
pipeline — better revisited as one piece covering every stage than fragmented across steps.

Also fixed in passing, since it's the same code path: `index_page` never set `last_indexed_at`
despite the column existing — `02` §8 names it as part of what makes the pending/error backlog
observable, so a page reaching `indexed` now stamps it.

**Spec touch-point**: none — `02` §7-8 already describe this lifecycle; this note records that
Phase 1 implements the state machine without the automatic dispatch the diagram assumes, same as
every other stage so far.

## 22. Review Queue and Resolution (Step 19)

Several decisions landed together building the admin console's review queue (`05` §1) and the
resolution actions `03` §3-5 describe but leave to "a later step" (`review.py`'s own prior
docstring).

**`admin_action_log` had no field list.** `02` §3's conceptual-tables list doesn't include it at
all — like `ingestion_log`, `query_log`, and `lint_log`, it's introduced only in `02` §5 as a named
stream with a purpose and consumers, no schema. Modeled a new `AdminActionLog` on `IngestionLog`
(`entry_id`, `actor`, `action`, `workspace_id` nullable, `subject_ref`, `detail` JSONB,
`created_at`), the one other append-only actor/action/detail history already in the schema —
nothing here calls for a different shape.

**Authorizing a workspace-less item reuses `any_workspace_with_role`, not a new global-admin
grant.** `06` §3's caller table names "global admin across all workspaces" as distinct from
per-workspace admin, but nothing in the schema represents it, and every `access_policy` grant is
workspace-scoped. Rather than add that concept now, `review.list_items`/the resolve endpoint treat
"admin in at least one workspace" as sufficient to see or act on a `submission`/`classification`
item with `workspace_id IS NULL` — the same check submission's own auth already uses (`09` §15) for
the identical "no workspace yet" situation. Once an item (or the source behind it) has a
workspace, `has_role` scopes it normally. Building real cross-workspace grants is deferred to
whenever multi-workspace routing itself lands (`phase1-tasklist.md`'s exclusions) — building the
access-control primitive first, with no caller needing it, would be speculative.

**`duplicate` resolution: `supersede` needed no new curation code; `merge` did, and is scoped to
near-duplicate evidence only.** Tracing `03` §4's four actions against what already existed:
`reject` reuses `reject_source`; `keep_both` is `pending_review -> ingesting`, the same edge
`check_duplicates`' own "no concerns" path already takes. `supersede` looked like it would need new
page-update logic too ("existing source/page marked superseded... updated in place via new
page_versions") — but `curate_source`'s existing title-match upsert (`_write_curated_page`,
step 12) already updates an existing concept/entity page in place when titles match. So
`supersede` is: mark the prior source(s) `RawSourceStatus.superseded`, transition to `ingesting`,
and let the normal ingest path (a separate, later call, same as every other stage) do the rest.

`merge` is genuinely different — the Curator must fold content into a *specific* page an admin
already identified, not run its normal independent extraction. `ReviewItem` carries no structured
detail column, so both `supersede` and `merge` read their evidence back off the `pending_review`
transition's `ingestion_log.detail` (`_duplicate_evidence`) rather than re-running `dedup.check`
against what may now be a changed DB state. That evidence only names a matched *page*
(`similar_pages`) for the near-duplicate verdict — an exact-match or newer-version duplicate
records a prior *source*, not a page, so `merge` is unavailable for those and raises
`InvalidResolutionError` rather than guessing which page to target. A failed merge call leaves the
review item `open` (not resolved) so an admin can retry it, unlike every other action, which
resolves regardless of outcome since they can't themselves fail past the point of no return.

**`pending_review -> ingested` isn't a legal edge, even for merge.** `03` §1's diagram has no such
transition, and `pipeline.py`'s own docstring says nothing is widened beyond that diagram. A merge
*is* a completed ingest, but goes through `ingesting` first like every other path to `ingested`,
via two ordinary legal transitions in sequence, rather than adding a new edge for one resolution.

**Cursor format** (`09` §14 promised one; this is the first list endpoint to actually need it):
base64 of `"{created_at.isoformat()}|{review_id}"`, sorted newest-first with `review_id` as
tiebreak, compared as a Postgres row-value tuple (`tuple_(...) < tuple_(...)`) rather than two
separate `WHERE` clauses — the latter would incorrectly exclude same-timestamp rows on the
tiebreak alone.

**Spec touch-point** (applied): none of `03`-`06` needed correction — `05` §1 already left
resolution mechanics unspecified ("the available resolution actions for that kind"), and `03` §4's
action table already described `merge`/`supersede` at the level this note fills in underneath.

## 23. Version Browser and Rollback (Step 20)

**`log.md` now actually merges `ingestion_log` and `admin_action_log`, not just `ingestion_log`.**
`05` §6 says rollback is "logged to `admin_action_log` and `log.md`" — the former didn't exist
until step 19, so `log.md`'s renderer had only ever drawn from `ingestion_log`, and step 19's own
note said as much. That's now stale: `curate.render_log_body` takes pre-formatted
`(timestamp, description)` pairs rather than ingestion-shaped `(timestamp, filename, count)`
tuples, and `ingestion._refresh_log` merges both streams (sorted newest-first) before rendering.
This isn't scoped to rollback specifically — every `admin_action_log` entry now surfaces in
`log.md`, including step 19's review-item resolutions — because the mechanism is inherently general
once built, and `02` §5 already describes `log.md` as materialized from every named stream
(`lint_log` excepted — no lint pass exists in Phase 1).

**Diff is recomputed directly from stored content, not composed from `diff_ref`.** `05` §6 wants a
diff between *any* two versions, but `diff_ref` (`09` §7) only ever holds the diff against the
version immediately before it — composing an arbitrary-pair diff from a chain of adjacent diffs is
real work (and lossy without care). Since `page_version.content` already stores each version's full
document, `versioning.diff` just runs `difflib.unified_diff` directly between the two requested
versions' `content`, exactly like `_write_diff` does for adjacent ones. `diff_ref` remains what it
was: a cheap precomputed cache for the common "diff to previous" case in a version list.

**Cursor pagination moved to a shared `pagination.py`.** Step 19 built `review.py`'s cursor
encode/decode as private module functions. This step needed the identical `(created_at, id)`
cursor for `versioning.list_versions`, so rather than duplicate it a second time, both now import
from `pagination.py` (`encode_cursor`, `decode_cursor`, `DEFAULT_LIST_LIMIT`, `MAX_LIST_LIMIT`).
`review.py` re-exports the constants it already had, so nothing calling `review.DEFAULT_LIST_LIMIT`
needed to change.

**Route registration order matters for `/pages/{id}/versions/diff` vs. `/versions/{version_id}`.**
FastAPI/Starlette matches routes in registration order; a literal path segment must be registered
before a path-parameter route that would otherwise shadow it (`version_id="diff"` matching first).
The diff route is defined immediately before the get-one-version route in `api.py` for exactly this
reason, and a test (`test_diff_route_is_not_shadowed_by_the_version_id_route`) pins it.

**Scope**: `06` §1's `pages` resource (plain get/list) is not built — out of step 20's citation
(`05` §6 only) and not needed here, since every version/rollback operation takes a `page_id` path
param directly rather than discovering one through a page-listing call.

**Spec touch-point** (applied): none — `05` §6 and `06` §1 already described this surface; this
note records how `log.md`'s merge and the diff mechanics are realized underneath it.

## 24. Two Bugs Caught by Live Verification (Step 21)

Step 21's own end-to-end script (submit → search → resolve one item of every kind → roll back a
version, through the real gateway) caught two gaps that 188 passing tests had been masking, both
in code from steps 19-20:

**`POST /pages/{id}/rollback` never actually refreshed `log.md`.** `versioning.rollback` writes the
`admin_action_log` entry (`09` §23), but nothing called `ingestion.refresh_log` afterward — the one
test exercising the merge (`test_log_merges_a_rollback_alongside_ingests`) called `refresh_log`
itself directly, which only proved the *renderer* merges both streams correctly, not that the real
request path triggers it. It didn't, because nothing had ever wired it in. Fixed by renaming the
private `_refresh_log` to public `refresh_log` (it now has two callers — `curate_source`
internally, and `api.py`'s rollback handler — so it can't stay module-private) and calling it from
the rollback endpoint after a successful rollback. `versioning.py` can't call it directly:
`ingestion.py` already imports `versioning.py`, so the reverse import would cycle.

**A resolved `classification` review item never got backfilled with its new workspace.**
`resolve_classification` correctly sets `source.workspace_id` (via `_accept_classification`), but
never set `item.workspace_id` — so the item's `admin_action_log` entry was written with
`workspace_id=None` even after resolution settled one, making it invisible to that workspace's
`log.md` and to `review.list_items`' `workspace_id` filter. `09` §19 had already named
"nullable-with-optional-backfill" as the intended shape for `review_item.workspace_id`, using
`raw_source.workspace_id`'s own backfill as the precedent — this was that pattern's other half,
simply never implemented. Fixed by setting `item.workspace_id = workspace.workspace_id` in
`resolve_classification` before calling `review.resolve`. `submission` and `duplicate` items don't
need the same fix: `duplicate` items always have a workspace at creation (`09` §22), and
`submission` items resolve independently of classification ever settling one, so there's no
analogous "moment resolution and workspace-resolution coincide" for them to backfill at.

Neither gap failed a single existing test — both are absences (a call that should have happened
and didn't, a field that should have been set and wasn't), which is exactly the failure mode a
green suite can't catch on its own and a live run reading actual output can.

**Spec touch-point**: none — both are implementation bugs against decisions `09` §19 and §23
already made, not new spec ground.

## 25. Document-Type Table Design (Phase 2 Step 22)

`02` §3 lists `document_type` as `type_code`, `workspace_id`, `description` — a field list, not a
key. Phase 1's `models.py` used a `Workspace.document_types` array column instead, with a comment
marking it a simplification pending this table.

**Decision**: `type_code` is the table's primary key on its own — not a composite
`(workspace_id, type_code)` key. Classification (`03` §3) produces a bare `type_code` string with
no workspace attached; the entire point of "routing" is that a code determines its one owning
workspace, not the other way around. A composite key would allow the same code to exist under two
workspaces, which nothing in `03`'s routing model could then disambiguate at classification time —
`classify.route`'s gate already assumes `result.document_type` is checked against exactly one
workspace's list. Phase 1's array column had this same implicit assumption (nothing stopped the
same string appearing in two workspaces' arrays, but nothing needed to prevent it either, since
only one workspace's array was ever checked against). The migration's backfill (`e22a1b4c3004`)
keeps a type_code's *first* occurrence across workspaces and drops later duplicates for exactly
this reason — there's no correct second home for a code that already claims a primary-key slot.

**CRUD shape** (`document_types.py`): `create`/`list_for_workspace`/`update`/`delete`, plus
`list_for_workspaces` (a set, not one workspace) and `type_codes_for_workspace` (the bare
`list[str]` `classify.py`'s pure functions expect — what `workspace.document_types` used to
provide). `update` supports renaming (changing the primary key itself — safe here because nothing
else in the schema foreign-keys against `type_code`; no `raw_source`/`wiki_page` column stores a
type code directly, only the `workspace_id` classification resolves) and reassigning
`workspace_id`, matching `05` §7's "add/remove/rename... reassign a type's target workspace"
verbatim. Reassignment touches only this one row — `05` §7 is explicit that it "affects future
routing only," and moving already-ingested content is the separate, already-designed bulk-move
action (`09` §11).

**API auth shape**: every `document-types` operation — list included — requires `admin`, unlike
`workspaces` (list/get open to any authenticated caller, `06` §1). The resource row itself signals
this: `06` §1 gives `document-types` one combined "list, manage | admin (manage)" row rather than
splitting read and write callers the way `workspaces`' two rows do. Listing without a
`workspace_id` filter mirrors the review queue's shape (`09` §22): admin in at least one workspace
sees every type across every workspace they administer, not a global listing.

**Spec touch-point**: none — `02` §3 and `05` §7 already specify the fields and the CRUD
operations; this note records the key design and auth shape underneath them.

## 26. Workspace CRUD, Its Bootstrap Problem, and `schema_ref`'s Scope (Phase 2 Step 23)

**Operation set matches `06` §1 exactly: create, update, archive — no delete, no unarchive.** `01`
§3 frames deletion as rare, explicit, and gated on an export prerequisite (`05` §7) — a
substantially bigger, separate piece of work, not a fourth CRUD verb to add here. Archived is
described as "read-only, excluded from default search/ingestion routing but still queryable,"
never as reversible, and the operation table doesn't name an unarchive action — so none exists.

**Creating a workspace has no target workspace to check admin against.** Every other admin check
in this codebase (`document-types`, `pages/{id}/rollback`, `review-items/{id}/resolve`) scopes
`has_role` to an *existing* workspace. Workspace creation can't — nothing exists yet. Reused the
same bootstrap answer `09` §22 already gave for workspace-less review items: admin in at least one
existing workspace is sufficient, rather than building the global-admin grant `09` §22 explicitly
declined to add. This still leaves the very first workspace in a fresh deployment uncreatable
through the API (no workspace exists yet to hold the first admin grant) — expected and
out of scope: seeding the first workspace and its first admin grant is deployment setup, the same
category of concern as seeding the first Postgres role, not a gateway operation.

**Creating a workspace grants the creator `admin` on it automatically.** Not directly specified,
but required for the endpoint to not be a dead end: every subsequent mutation (`update`, `archive`,
granting *other* principals access) requires admin *in that workspace*, and a freshly created
workspace starts with zero `access_policy` rows. Without this grant, a workspace's creator
couldn't manage what they just created through the API at all — only a raw DB write could recover
it. `workspaces.create()` itself stays a pure DB operation with no principal concept; the grant is
the `POST /workspaces` handler's own follow-up call to `workspaces.grant()`, not something the
create function does implicitly.

**`schema_ref` stays a plain pointer field — SCHEMA.md itself is not implemented.** `05` §7's
"Workspace lifecycle" bullet lists "edit SCHEMA.md" alongside create/archive, but no code anywhere
loads, parses, versions, or applies a real per-workspace `SCHEMA.md` today — every threshold
(`09` §6's near-duplicate score, classification confidence, etc.) is a hardcoded Python default,
and `llm.resolve_model`'s `schema: dict | None` parameter has never been passed anything but
`None`. Building real SCHEMA.md storage (parsing, validation, versioning through the page-version
machinery per `01` §7's "versioned like a wiki page," then rewiring `classify.py`/`dedup.py`/
`curate.py`/`llm.py` to read live per-workspace overrides instead of constants) is a
self-contained feature on the scale of a track of its own, not a workspace-CRUD side effect. This
step's `update` lets an admin set/change the *pointer* string, matching `01` §3's own framing of
the field ("pointer to this workspace's SCHEMA.md") — the content behind that pointer remains a
carried-forward gap, flagged here rather than silently built or silently skipped.

**Spec touch-point**: none — `06` §1 and `05` §7 already specify the operations; this note records
the bootstrap-auth answer and the `schema_ref` scope boundary underneath them.

## 27. Classifier Routing Against the Central Taxonomy (Phase 2 Step 24)

`classify_source` and `resolve_classification` no longer take a `workspace` parameter. `03` §3
assigns `document_type` from "the central taxonomy" (step 5) and resolves `document_type ->
workspace_id` "via the taxonomy's routing table" (step 6) — Phase 1's single-workspace shortcut
had the caller pick the workspace first and classify only against its slice, which is the
opposite of what "routing" means once more than one workspace exists. `document_types.list_active`
(the full central taxonomy, every type across every *active* workspace — `01` §3 excludes archived
workspaces from ingestion routing) replaces `type_codes_for_workspace` at both call sites;
`document_types.workspace_for_type` (a code -> its active workspace, or `None`) replaces the
admin-supplied `workspace_id` in resolution.

**Verified against the real model, not just stubs**: a taxonomy spanning two workspaces
(`eng.runbook`/`eng.design-doc` under one, `policy.hr`/`policy.security` under another), a leave-
policy document, `gpt-5-nano` correctly picked `policy.hr` and the source landed in the `policies`
workspace with zero workspace named anywhere in the call — this is the behavior the whole step
exists to prove, so a live run reading the actual resolved `workspace_id` mattered more than a
stubbed assertion would have.

**Gate-then-route, not route-then-gate, unlike `03` §3's literal step order.** The spec's steps run
6 (resolve workspace) before 7 (confidence gate, "the workspace's configured threshold"), implying
the threshold is workspace-specific. Per `09` §26, no workspace has a real threshold yet — every
gate still uses the one hardcoded `DEFAULT_MIN_CONFIDENCE`. Since there's no per-workspace value to
look up, resolving the workspace before the gate would only add a lookup with nothing yet to do
with it, so the code still gates first (as Phase 1 did) and resolves the workspace only on
acceptance. This becomes a real ordering question — and needs revisiting — once SCHEMA.md
thresholds are real; noted here so it isn't mistaken for an oversight before then.

**`resolve_classification`'s workspace-scoped admin check moved into `api.py`, ahead of the call.**
`ResolveRequest.workspace_id` is gone — the field existed only so an admin could name the target
workspace for a classification resolution, which the taxonomy table now answers on its own.
Authorization still needs to happen *before* dispatch, though, and by that point `payload.action`
(the chosen `document_type`) is all the endpoint has — so it looks up the `DocumentType` row
itself, checks `has_role` against *its* `workspace_id`, and only then calls
`ingestion.resolve_review_item`. This preserves the exact security property step 22/23's pattern
already established (admin must hold the role in the specific workspace being written into, not
just "admin somewhere") — it just derives that workspace from `action` instead of trusting a
separate field a caller could otherwise point anywhere.

**Spec touch-point**: none — `03` §3 already specified this routing; this note records why the
gate still runs before workspace resolution (tied to `09` §26's still-open SCHEMA.md gap) and how
resolution's authorization was re-derived without an admin-supplied `workspace_id`.

## 28. The Search Endpoint (Phase 2 Step 25)

**`SearchResult` is a new type, not an extension of `Hit`.** `search.search()`'s old return type
(`Hit`: `page_id`/`workspace_id`/`path`/`score`) is also `find_similar`'s return type, and
near-duplicate scoring (`03` §4) needs none of `04` §7's provenance (title, `page_type`, excerpt,
citations). Adding those fields to `Hit` would force `find_similar` to either populate data it
doesn't need or carry them as always-empty optionals; a second frozen dataclass keeps each
function's contract honest about what it actually returns. `search()`'s existing callers needed no
changes — every field they used (`page_id`, `path`, `score`) still exists on the richer type.

**Citations and excerpt come from one query, not a per-hit follow-up.** `page_index.version_id`
already points at the exact indexed version, so joining `page_version` in the same statement gets
`content` (for `ts_headline` and footnote parsing) and `frontmatter ->> 'title'` without an N+1.
Citations are extracted in Python (`_extract_citations`, a regex over `[^N]: definition` lines —
`01` §6's footnote convention) rather than in SQL, since Postgres has no primitive for "parse
markdown footnote syntax."

**`ts_headline` runs against the whole `page_version.content`, frontmatter included, not a
body-only excerpt.** Stripping frontmatter first would need either a second column (schema change)
or a Python-side split before a *second* SQL round-trip per hit — not worth it against the actual
risk, which is small: `ts_headline` picks the highest-lexeme-density fragment, so it drifts into
the YAML block only when a query term matches frontmatter (e.g. a tag) and nothing in the body,
which is arguably still a reasonable excerpt to show. Noted as a deliberate simplification, not an
oversight, in case a real query surfaces it as a UX problem worth the extra column later.

**JSONB `?|` needs an explicit `ARRAY(String)` bind, not `expanding=True`.** The `tags` filter's
`frontmatter -> 'tags' ?| :tags` takes one array operand; `expanding=True` (used for
`workspace_ids`/`page_types`, both `IN (...)` clauses) instead unrolls the list into scalar
placeholders, which `?|` can't accept — asyncpg raised `DataError` until the bind carried an
explicit array type. `page_type`/`date_from`/`date_to` filters needed no equivalent fix.

**The taxonomy pre-filter only runs when the caller didn't already scope the search.** `04` §4
doesn't explicitly say whether the pre-filter applies to an explicit `workspace_id` list too, but
auto-narrowing a search the caller already scoped themselves would silently second-guess an
explicit choice — so `_taxonomy_prefilter` only executes in the "unscoped, defaults to everything
accessible" branch, reusing `classify.lexical_match` (03 §3's own ingest-time function) run against
query text instead of a document, then `document_types.workspace_for_type` to map the matched
label to a workspace — the query-path mirror of step 24's ingest-path routing.

**Draft visibility resolves a stricter workspace set up front, not a post-hoc filter.** `04` §6:
"admins/API callers with elevated scope may include draft." Rather than resolving with `reader`
and filtering draft rows after the fact (which would need to know, per row, whether *that*
workspace individually grants the caller `contributor` — a second per-workspace check the simple
`workspace_ids IN (...)` query shape doesn't naturally support), `include_drafts=True` resolves the
accessible-workspace set with `contributor` required from the start. Simpler, and strictly more
conservative — a caller only ever sees drafts in workspaces they could also submit to.

**No cursor pagination for `/search`, unlike every other list endpoint (`09` §14, §22-23).**
Ranked results aren't an append-ordered list a cursor was built for — the underlying data and
ranking can both shift between page fetches, and typical search UX is "top-K, refine the query,"
not deep pagination through thousands of scored hits. `limit` (default 20, `search()`'s existing
default) is the only size control.

**`query_log` is written unconditionally, including empty-result and zero-accessible-workspace
searches** — `04` §8 says "every search call," and a query that found nothing is still a data point
for the future orphan/low-traffic detector (`05` §2) that consumes this table.

**Spec touch-point**: none — `04` §1, §4-8 already specify this surface; this note records the
mechanics (types, the JSONB bind fix, pre-filter scope, draft-visibility resolution, and the
no-cursor-pagination call) underneath it.

## 29. Dedicated-Index-Per-Workspace via OpenSearch (Phase 2 Step 26)

Deepak chose full OpenSearch integration over the lighter-weight alternative offered (simulating a
second backend's score scale with a second Postgres query) — a real new infrastructure dependency
(`docker-compose.yml`'s `opensearch` service, `opensearch-py`), not just new application code.

**One shared OpenSearch index for every dedicated workspace, not one index per workspace.** `08` §2
says "dedicated index *instance*"; `02` §4 motivates it with both "very large corpora" and
"stricter isolation requirements." Read here as "a dedicated *backend/technology*," matching how
the shared Postgres index already partitions logically by `workspace_id` rather than physically
(`02` §4's own stated principle) — `dedicated_index.py` applies the identical filter, just against
OpenSearch. Per-workspace physical OpenSearch indices (closer to "stricter isolation" read
literally) would need dynamic index lifecycle management with no current caller needing it;
deferred rather than built for a requirement nothing has asked for yet.

**Postgres stays the system of record for near-duplicate detection, regardless of a workspace's
search backend.** `02` §4 names two workloads the Full-Text Index serves: search/retrieval and
near-duplicate detection (`03` §4). Diverting a dedicated workspace's pages away from Postgres
entirely would silently break `find_similar` for exactly the workspaces most likely to have enough
volume for near-duplicates to matter. Decision: `search.index_page` **always** writes the shared
Postgres index — dedicated or not — and *additionally* writes OpenSearch for a dedicated workspace.
OpenSearch is purely a query-serving overlay for search traffic; `find_similar` never looks at it.
This does mean a dedicated workspace's content isn't actually isolated out of Postgres, which
sits in tension with `02` §4's "stricter isolation requirements" motivation — flagged plainly
rather than silently glossed over, since it's a real trade-off: full isolation would need
`find_similar` to also become backend-aware (an OpenSearch-native near-duplicate metric matching
the calibrated Postgres containment score, `09` §17, so `SCHEMA.md`'s threshold stays meaningful
regardless of backend) — real work with no current caller, deferred rather than built speculatively.

**Circular import, broken by extracting the shared type.** `search.py` needs to call
`dedicated_index.index_page` (the write-dispatch); `dedicated_index.py` needs `search.SearchResult`
to return a compatible shape. Neither can import the other. `search_result.py` now owns
`SearchResult` and `extract_citations`, imported by both, importing neither.

**A fresh `AsyncOpenSearch` client per call, not a persistent module singleton — a real bug caught
before it shipped, not a style preference.** The first implementation created one client at import
time, matching `db.py`'s `create_async_engine` pattern. It broke every test after the first with
`RuntimeError: Timeout context manager should be used inside a task`: the client's underlying
`aiohttp` session binds to whichever asyncio event loop is running on first use, and this test
suite's default (`asyncio_mode = "auto"`) gives each async test function its own loop — the second
test's loop couldn't use a session opened under the first test's loop. `db.py` never hits this
because the `session` test fixture already creates a fresh `AsyncEngine` per test; `dedicated_
index.py`'s `_client()` async context manager does the same thing per *call* instead, since there's
no per-request session-scoped fixture equivalent yet in application code. (A global
`asyncio_default_fixture_loop_scope = "session"` pytest setting was tried and reverted — it worked,
but changes event-loop behavior for the entire suite to fix one module's lifecycle assumption; the
per-call client is the more local, surgical fix.)

**Highlight tags aligned across backends.** `ts_headline` (search.py) defaults to `<b>`/`</b>`;
OpenSearch's highlighter defaults to `<em>`/`<mark>`-style tags depending on version. Explicitly set
to `<b>`/`</b>` in `dedicated_index.py` so a merged federated result set doesn't mark matches with
different tags depending on which backend happened to serve them — caught by running the live
round-trip against a real OpenSearch instance, not assumed.

**`merge_federated`**: normalizes only the dedicated backend's scores (min-max to `[0,1]`, within
that query's result set — a single tied score maps to `1.0` rather than dividing by zero, `04` §4's
own "approximation" caveat, not a spec-defined case); the shared index's raw `ts_rank_cd` scores are
left as-is. Sorted descending, tie-broken `workspace_id` then `page_id`. `limit` is applied by the
caller (`api.py`) *after* merging both pools, not independently per backend before — taking `limit`
from each pool first could drop a higher-ranked hit from one pool in favor of a lower-ranked hit
from the other.

**`dedicated_index` needed an actual admin-facing toggle — caught by asking "is this feature usable
end-to-end," not by a test.** The column existed and the query-time split worked, but nothing let
an admin ever set it to `true` short of a raw database write. Added to `workspaces.update`
(update-only, not `create` — `02` §4 and `06` §6 frame this as an operational decision made once a
workspace approaches scale, not a creation-time choice) and `POST /workspaces/{id}`. Toggling
affects **future writes only**, the same scope `05` §7 already gives taxonomy reassignment —
content indexed before the toggle stays on whichever backend indexed it until the next reindex;
there is no retroactive migration between backends here, and none was asked for.

**Spec touch-point**: none — `02` §4, `04` §4, and `08` §2 already specify the behavior and the
technology choice; this note records the backend-scope decision (one shared index, Postgres as
dedup's system of record regardless of search backend), the async client-lifecycle bug and fix, and
the admin-toggle gap found and closed.

## 30. Taxonomy Bulk-Move — What "A Set of Pages/Sources" Actually Means (Phase 2 Step 27)

`11` already settled the execution model (dry-run preview, batched execute, failed batch halts
without rolling back completed ones). What it left open, because no code existed yet to force the
question: **how is "the set of pages/sources" identified?** `raw_source` and `wiki_page` carry only
`workspace_id`, never the `document_type` a source was originally classified under — that field is
never persisted past the classification step. So a bulk move triggered by "reassign a type's target
workspace" cannot filter *by that type* after the fact; there is nothing left to filter on. Decision:
`bulk_move.py` takes an explicit `page_ids`/`source_ids` list from the caller rather than deriving a
set from a document-type reassignment — the admin identifies what should move (via `pages`/`sources`
listing/search, not built by this step) and hands the gateway concrete ids. This is narrower than "a
type's content moves automatically," but matches what `05` §7 actually says: a *separate* admin
action the operator invokes "if needed," not an automatic consequence of reassignment.

**Module never commits; `api.py` owns the batch/commit loop — the one deliberate exception to this
codebase's "modules never call `session.commit()`" convention, forced by the spec's own requirement.**
`11`'s "failed batch halts without rolling back completed batches" is only real if each batch is its
own durable transaction — a single `execute()` call that processes everything and lets the *caller*
commit once at the end (`api.py`'s usual pattern) can't express that. `bulk_move.execute_batch`
moves exactly the ids it's given and never touches the session's transaction state; `api.py`'s
`bulk_move_execute` slices the request into `BATCH_SIZE`-sized chunks, calling `execute_batch` and
committing after each, catching an exception to stop the loop (rolling back only that batch) while
prior commits stand. `BATCH_SIZE = 100` — no existing default anywhere to inherit; picked so `11`'s
5,000–50,000-page/workspace ceiling stays in the tens-to-hundreds of batches, not thousands.

**Resumability is per-item idempotency, not a resume token.** `11`: "the admin resumes or retries
the remaining batches." Rather than tracking operation state server-side, `execute_batch` silently
skips any id no longer in the *source* workspace (already moved, or never valid) instead of erroring
— calling the execute endpoint again with the exact same `page_ids`/`source_ids` after a partial
failure is safe and correct by construction. `preview` reports this case explicitly as
`already_at_target` so an admin re-previewing mid-operation sees accurate progress.

**No `Idempotency-Key` support on the execute endpoint — a deliberate omission, not an oversight.**
Every other mutating endpoint (`09` §14) pairs a single commit with an `IdempotencyRecord` write in
the same transaction. Bulk-move's multi-commit batch loop has no single point to attach one, and
doesn't need it: the resumability property above already makes a bare retry safe.

**A page leaving a dedicated workspace needed an explicit OpenSearch cleanup — a real, pre-existing
gap this step's first design pass surfaced, not introduced by it.** `search.index_page` (`09` §29)
only ever *adds* a page to OpenSearch when its current workspace is dedicated; nothing ever removed
one when a page's workspace changed away from dedicated — impossible before this step, since nothing
could previously change `wiki_page.workspace_id` at all. Fixing `index_page` itself (delete when not
dedicated, on every reindex) was rejected: it would make **every** reindex call, including every
non-dedicated workspace's, touch OpenSearch, expanding the test-suite's OpenSearch dependency (`09`
§29's scoped-to-`test_dedicated_index.py`/`test_federated_search_api.py` note) to nearly the whole
suite for a cleanup only bulk-move can currently trigger. Scoped instead to `bulk_move.execute_batch`:
when the *source* workspace is dedicated, it calls `dedicated_index.delete_page` directly after
moving a page out. The equivalent gap from simply toggling `dedicated_index` off a workspace (no
page move involved) stays exactly as `09` §29 already documented and accepted — "future writes only,
no retroactive migration" — since that is a different, already-decided trade-off this step didn't
reopen.

**`ingestion._relocate` made public as `relocate`.** Re-homing a `raw_source` needs the identical
copy-repoint-delete sequence classification-time routing already uses (`02` §2's per-workspace
object-store prefix) — reused rather than duplicated, so `bulk_move.py` imports it directly.

**Spec touch-point**: none new — `05` §7 and `09` §11 already described dry-run + batched execute;
this note records what "the set" concretely means given the schema, the module/API commit-boundary
split, and the OpenSearch cleanup gap found and closed.

## 31. `page_link` Parsing — What "Fully-Qualified Workspace-Relative" Means (Phase 2 Step 28)

`01` §6 defines the link convention but not its exact syntax: "Cross-references use standard
markdown links; links that target another workspace are written as fully-qualified
workspace-relative paths." Two concrete choices this step had to make, neither spec'd literally:

**Same-workspace target = an exact `wiki_page.path` match.** `[text](concepts/foo.md)` resolves
against `WikiPage.path` within the linking page's own `workspace_id`, string-for-string — no
directory-relative resolution (`../`, etc.), since pages aren't served from an actual filesystem
tree a browser or renderer would resolve relative links against; `path` is just a flat identifying
string per workspace (`01` §5).

**Cross-workspace target = `/{workspace_id}/{path}`.** No other convention for "fully-qualified
workspace-relative" exists anywhere in the spec, but this codebase already has exactly one
precedent for what a fully-qualified per-workspace path looks like:
`objectstore.py`'s `/{workspace_id}/sources/{source_id}/{filename}` and
`/{workspace_id}/diffs/{version_id}.diff` (`02` §2, `09` §7). Reused the identical shape rather than
inventing a second convention: `/{workspace_id}/{page.path}`.

**Parsed automatically inside every version write, not as a separate explicit-call lifecycle.**
Unlike reindexing (`09` §18, deliberately explicit/deferred because it's LLM-adjacent-cost), `02`
§3 says these rows are "(re)written ... whenever a page's cross-references are parsed during a
write" — read as synchronous-with-the-write, and cheap enough (a regex plus a handful of
point lookups, no LLM call) to just do inline. `page_links.sync` runs inside both
`versioning.create_page` and `write_version`, alongside the existing `_mark_stale` call —
`rollback` and `bulk_move`'s page re-home (`09` §30) both go through `write_version`, so they pick
this up automatically with no extra wiring.

**Delete-then-reinsert per write, mirroring `search.index_page`'s existing pattern for `page_index`.**
A version's outbound links fully replace whatever the previous version pointed to; there's no
"unchanged link" case worth preserving row identity for.

**Excluded on purpose**: image embeds (`![alt](src)`, via a negative lookbehind on `!`), footnote
citations (`[^1]`, which have no `(...)` target at all so the pattern never matches them), and
reference-style links (`[text][ref]` + a separate `[ref]: url` definition) — `01` §6 says "standard
markdown links" without specifying inline vs. reference style; scoped to inline only since nothing
in this codebase generates reference-style links today. A dangling link (target doesn't resolve to
any `wiki_page.path`) or an external URL both simply produce no row — not an error, since citation
footnotes and external references are legitimate content this table was never meant to track.

**Read-time link resolution is out of scope — no caller exists yet.** `01` §3's table requires the
gateway to re-check AuthZ against a link's *target* workspace before resolving it for a reader, but
that's a concern for whatever endpoint serves rendered page content with links resolved, and
`pages/{id}` get isn't built (`06` §1 — deferred to 2d). This step only keeps `page_link` rows
correct; resolving them at read time is the future endpoint's job, the same "wire the caller when
the caller exists" gap `09` §20 already accepted for the catalog-match boost.

**No separate live-verification script.** Every other Phase 2 step needing one touched a backend
pytest doesn't exercise identically to production (a real LLM call, OpenSearch's async client
lifecycle, real S3 object moves against MinIO vs. the test suite's `file://` temp dir). This step
touches only Postgres, and the test suite already runs against a real, unmocked Postgres instance
with no behavioral gap from production — so the pytest run itself *is* the live verification here.

**Spec touch-point**: none new — `01` §6 and `02` §3 already specify the behavior; this note records
the two concrete syntax choices ("fully-qualified" and same-workspace path matching) and the
automatic-not-explicit wiring decision.

## 32. 2a Track Verification — No New Bugs, One Search Semantics Clarification (Phase 2 Step 29)

Closes out track 2a. `tests/test_end_to_end_2a.py` ties steps 22–28 together through the real
gateway with a mocked LLM (same convention as `09` §24's Phase 1 close): two documents route to
different workspaces with no workspace named in either submission, one `GET /search` query returns
merged ranked results from both, and a taxonomy bulk-move relocates a batch of pages with real
per-batch progress (`BATCH_SIZE` forced to 1 so `batch_count` genuinely reflects multiple batches,
not a single one trivially succeeding).

A companion live script (not committed, same as `09` §24/§26's) ran the identical flow against the
**real** LLM (`gpt-5-nano`, both classify and curate) and the real dev Postgres DB — two genuinely
different documents (a Kubernetes runbook, an HR time-off policy), submitted with no workspace
named. Unlike `09` §24 and §26, **this run found no new application bugs** — routing, curation,
reindexing, federated search, and bulk-move all worked correctly against the real model and real
data on the first attempt.

**One clarification worth recording, surfaced by the live script's own first mistake, not an
application bug**: `search.py`'s `websearch_to_tsquery` (`09` §28) ANDs bare terms by default — a
multi-word query like `payments-worker time off` requires *all* those words in one document, and
correctly returns nothing when two genuinely unrelated documents share no vocabulary. Confirming
"one query merges results from both workspaces" against two topically unrelated real documents
needs `websearch_to_tsquery`'s explicit `OR` syntax (`payments-worker OR "time off"`), not a
concatenation of both documents' distinctive terms. This is pre-existing, correct, standard
`websearch_to_tsquery` behavior from Phase 1 (`09` §28) — not something this step changed — recorded
here because it wasn't written down anywhere before and a live check is exactly where an implicit
assumption like this gets caught.

**Spec touch-point**: none — this section is the closing verification record for 2a, not a new
decision; `00` §7's traceability rows for multi-workspace routing, federated search, and the
taxonomy bulk-move admin action are now met.

## 33. Real Celery Tasks — One "Curation" Task Bundles Dedup and Curate (Phase 2 Step 30)

`§21` deliberately deferred real async dispatch for every pipeline stage rather than wiring
indexing alone. This step closes that gap for the task-registration half of it (dispatch — who
calls `.delay()` and when — is step 32; `docker-compose.yml` worker services are step 31): three
real `@app.task`s in [`tasks.py`](../src/karpwiki/tasks.py) wrap the existing pure orchestration
functions — `classify_source` (queue `classification`), `curate_source` (queue `curation`),
`search.reindex` (queue `indexing`) — using the routing `tasks.py` already defined.

**Decision: `check_duplicates` (dedup) is not its own task.** The tasklist names exactly three
tasks to register, matching `QUEUES`'s four fixed queues (classification/curation/indexing/
maintenance — no `dedup` queue). Step 32's "acceptance enqueues dedup then curate" is read as one
dispatch — the `curation` task itself runs `check_duplicates`, and only calls `curate_source` if
the verdict is `ingesting` (a duplicate or a `gated` policy parks it at `pending_review` instead,
same as `tests/test_end_to_end_2a.py`'s existing manual `classify_source` -> `check_duplicates` ->
`curate_source` sequence already does by hand). Two chained tasks were the alternative — closer to
"dedup then curate" read literally as two enqueues — but adds a queue hop and a dispatch-time
decision for a step that's cheap, synchronous, and has nowhere else to route to; better one task
matching the tasklist's own enumeration than inventing a fourth queue this session's tasklist
didn't ask for.

**`check_duplicates` needs the Classifier's `summary`** (03 §4: the near-duplicate query text),
which `classify_source` never returns — only `PipelineState`. Rather than widen that return type
or add a column, the curation task reads it back off `ingestion_log`'s `classified` transition
detail, the same pattern `ingestion._duplicate_evidence` already uses for duplicate evidence. Empty
when classification was admin-assigned (`resolve_classification`, 09 §22) rather than run by the
model — there was no classifier call to have produced one, so the near-duplicate check simply finds
nothing, which is the correct degrade rather than an error.

**Async-in-sync**: every pipeline function is `async`, but a Celery worker calls task bodies
synchronously (09 §21's other module-level-async-client lesson, from step 26's dedicated-index
work, doesn't apply here — each task call opens its own `db.session_scope()` per invocation, no
long-lived client). Each `@app.task` is a thin sync wrapper doing `asyncio.run(...)` over an async
inner function (`_classify`/`_curate`/`_reindex`) that does the real work; the inner functions are
what tests call directly, and what step 32's dispatch call sites will eventually pass IDs to.

**Test-only `call` seam on the inner functions**: `_classify`/`_curate` accept an optional `call`
keyword (defaulting to the real `ingestion.call_model`/`call_curator_model`), never passed by the
`@app.task` wrapper — the same injectable-`Protocol` pattern `ingestion.py` already uses throughout
for the same reason, one layer up, rather than a new mocking approach for this layer alone.

**Live-verified** (`tests/test_tasks.py`'s five tests use mocked classify/curate calls against the
real test DB; a companion, not-committed live script separately drove the same three tasks with no
`call` override — real `gpt-5-nano`, real dev Postgres, real MinIO object store — one document
through `_classify` -> `_curate` -> `_reindex`, confirming it reached `ingested`, produced source/
concept/entity pages plus refreshed `overview.md`/`log.md`, indexed, and became searchable. No
bugs found).

**Spec touch-point**: none — `06` §4 and `tasks.py`'s own docstring already describe the queue
split; this section records how the three tasks were shaped and why dedup rides inside `curation`
rather than getting a fourth.

## 34. Worker Containers, and a Real Cross-Event-Loop Bug in the Live Check (Phase 2 Step 31)

**New: this repo's first `Dockerfile`.** Nothing containerized the app itself before this step —
`docker-compose.yml` ran only infra (Postgres/Redis/MinIO/OpenSearch), and the README's own
install instructions run the app from a local venv. Step 31 needs a buildable image to give the
four worker services (06 §4's per-queue pools: classification, curation, indexing, maintenance)
something real to run, so a minimal `Dockerfile` (`python:3.14-slim`, matching the dev venv's
tested version, installs the package, runs as a non-root user, default `CMD` is a plain `celery
worker`) was added alongside it — flagged to the user as a real fork before building, since
step 34 (install/scaling docs) will end up describing this image and it's worth getting the shape
right rather than revising it later. `docker-compose.yml` gets one service per queue, each
overriding `command:` with `-Q <queue>`; only `worker-classification` declares `build:` and the
other three reference the same `karpwiki-worker:local` tag, so `docker compose build` doesn't
rebuild the identical image four times. A `worker-maintenance` service exists even though no real
task fills that queue yet (Maintenance Advisor is track 2c) — the tasklist names all four queues,
and an idle consumer on an empty queue costs nothing. Container-network hostnames
(`postgres`/`redis`/`minio`/`opensearch`) are set explicitly per service via an `x-worker-env`
YAML anchor, since `.env`'s `localhost`-based URLs are for a host-run app talking to these same
containers over published ports (02 §2's "every process group must agree on one path/URL" — inside
the compose network that's the service hostnames, not `localhost`); only the LLM model strings and
`OPENAI_API_KEY` are pulled from the project `.env` Compose auto-loads.

**A real bug, caught only because this step's live check dispatched through the actual broker to
the actual containers** (`tasks.classify_source.delay(...)` etc., not a direct call like step 30's
live script): the *second* task processed by a given worker process crashed with `RuntimeError:
Task ... got Future ... attached to a different loop`. Root cause: `db.engine` (`db.py`) is
a process-level singleton whose connection pool holds asyncpg connections bound to whichever
asyncio event loop was running when they were opened; every `@app.task` wrapper runs its own
`asyncio.run(...)` (step 30 §33 — a fresh event loop *per call*), so a pooled connection opened
during task 1's loop can't be reused during task 2's loop in the same long-lived worker process.
This is the exact failure mode `09 §21`'s OpenSearch-client note already named and asked to watch
for on "a second long-lived async external-service client" — it showed up for the DB engine
instead, under Celery's prefork model rather than pytest's per-test event loop. **Step 30's own
tests and live script never hit it**: both drive `_classify`/`_curate`/`_reindex` directly inside
one shared `asyncio.run()` (one test function, one script's `main()`), so no loop boundary was ever
crossed — only a real dispatch through independent per-task `asyncio.run()` calls exposes it. This
is itself evidence for why 09's "live-verify before calling a step done" discipline exists as a
per-step requirement, not just a phase-boundary one.

**Fix**: `_run_and_release(coro)` in `tasks.py` wraps every task body and disposes `db.engine`'s
pool in a `finally` block after every single call, not just after fork — the next task, whatever
process handles it, always finds an empty pool and reconnects fresh under its own loop. Considered
and rejected: disposing only in a `worker_process_init` post-fork signal handler, which fixes the
*first* task in a fresh child but not the second (the pool is a process, not a fork, boundary
here) — the bug reproduced with prefork's persistent child processes reusing the exact same
`ForkPoolWorker` across three sequential classify calls in the live check, confirming a per-fork
fix alone would have been insufficient. Also considered: one persistent event loop per worker
process (via `worker_process_init`, reused for every task instead of a fresh `asyncio.run()` each
time), which would let the pool's connections actually get reused across tasks instead of
reconnecting every time — real, but more architecture than this step needs; a candidate to revisit
if per-task reconnect latency ever matters at the concurrency step 33 introduces.

**Live-verified**: after the fix, the same live-check script dispatched `classify_source.delay()`
-> `curate_source.delay()` -> `reindex.delay()` through the real broker to the real containers
(`docker compose up -d worker-classification worker-curation worker-indexing worker-maintenance`),
polling `raw_source.pipeline_state`/`index_status.state` in Postgres for completion (no Celery
result backend is configured — 09/`docker-compose.yml`'s own comment already says queued-job state
is meant to be re-derivable from Postgres, so polling the DB rather than adding a backend is the
existing intent, not a new decision). One document round-tripped end to end through three separate
container processes, on the correct dedicated queue each time, and became searchable — the
`worker-classification` container's log confirms the same forked child process handled three
consecutive tasks cleanly post-fix. No further bugs found.

**Spec touch-point**: `06` §5's deployment topology ("independently scalable container/process
groups") now has its first real container in this repo, matching the shape that section describes;
no wording changes needed there.

## 35. Wiring Real Dispatch (Phase 2 Step 32)

Closes the loop steps 30–31 set up: `api.py` now calls `.delay()` at every point step 32 names,
always strictly *after* the commit that made the referenced row visible (a task opens its own
session against a separately-connected worker process, so dispatching before commit would race it).

**Submission -> classification**: `submit_source`, right after `_store` + commit.

**Acceptance -> dedup-then-curate — two dispatch sites feeding one task**: `_classify` itself
dispatches `curate_source` when `ingestion.classify_source` returns `classified` (the automatic
path — no `api.py` involvement, since nothing there ever calls `classify_source` directly). The
admin path — `resolve_review_item_endpoint` after a `classification` resolution (always lands at
`classified`) or a `duplicate` resolution's `keep_both`/`supersede` (land at `ingesting`, dedup
already resolved by the human) — dispatches the *same* `curate_source` task. This exposed a gap
step 30's design hadn't needed to consider: `tasks._curate` unconditionally ran `check_duplicates`
first, correct only for the fresh-classification entry. Resuming at `ingesting` through that same
path would both re-litigate a decision a human already made and hit an illegal `ingesting ->
duplicate_check` transition (03 §1's edges don't allow it). Fixed by having `_curate` branch on the
source's *current* `pipeline_state` at entry — `classified` runs dedup then curate; `ingesting`
skips straight to curate — rather than adding a second, dedup-free task. Same reasoning as `09`
§33's original bundling call: one task per tasklist-named queue, not a queue per branch.

**Merge is not "curate"**: `resolve_duplicate`'s `merge` outcome writes directly into the matched
page (`ingestion._resolve_merge`) and reaches `ingested` synchronously inside the request — no
curation task involved, so what it needs is a reindex dispatch instead. The written page's id isn't
returned by `resolve_review_item`, so `resolve_review_item_endpoint` reads it back off the last
`ingestion_log` entry's `target_page_id` detail (`_resolve_merge`'s own write) — the same read-back
convention `ingestion._duplicate_evidence` and `tasks._classification_summary` already established,
reused rather than widening `resolve_review_item`'s return contract for one caller.

**Page write -> reindex, at three different levels of precision**: `rollback_page` and
`bulk_move_execute` (per batch, right after its own commit) already know the exact page id(s) they
touched, so they dispatch `reindex` directly for those. `tasks._curate` doesn't track exactly which
pages `curate_source` wrote (it can create a source page plus several concept/entity pages, some new
and some upserted into existing ones) — rather than widen `curate_source`'s return contract too, it
calls the now-workspace-scoped `search.pending_pages(session, workspace_id=...)` (new optional
filter, additive) right before its own transaction commits and dispatches `reindex` for everything
that comes back. This can occasionally sweep in an unrelated already-pending page from the same
workspace, not just ones this call wrote — accepted as correct-enough (reindexing a genuinely
pending/stale page is never wrong) rather than threading exact page-id tracking through
`curate_source`'s pure-function contract for one dispatch site.

**A real, reproduced-once bug during live verification, root-caused to this session's own test
run rather than the dispatch code**: the first live check (submit over real HTTP, poll, nothing
manually driven) reached `classified` but `curate_source` was never received by the curation worker
container — no exception, no message on the `curation` Redis queue at all. A manual reproduction
with temporary debug logging immediately after showed the *identical* code path dispatching
correctly (a real `AsyncResult`, received by the worker within milliseconds), and two further live
runs both completed the full submit -> classify -> curate -> reindex -> searchable chain purely via
dispatch. The likely cause, never fully proven: immediately before the failed run, a *single* full
pytest run happened to execute with the dispatch code already active but *before* `tests/
conftest.py`'s `dispatched` autouse fixture existed yet to intercept `.delay()` — every test hitting
`submit_source`/`resolve_review_item_endpoint`/`rollback_page`/`bulk_move_execute`, plus `tests/
test_tasks.py`'s direct `_classify`/`_curate` calls, published real messages to the real broker in a
~30-second burst (confirmed after the fact via worker container logs full of `"no source"`
warnings for test-DB-only ids arriving at the real, dev-DB-backed workers). One dispatch going
missing shortly after that burst, with everything working cleanly before and after, points at
transient broker/connection-pool stress from that flood rather than a logic bug — and the fixture
added specifically for step 32's own tests (below) permanently prevents any future test run from
producing that flood again.

**Tests**: `tests/test_dispatch.py` (new) verifies the *wiring* — the right task gets `.delay()`d
with the right id at the right point — for all six call sites above (submission, classification
resolve, duplicate keep_both/reject/merge, rollback, bulk-move) plus `_curate`'s own reindex
dispatch, via an autouse `dispatched` fixture in `conftest.py` that intercepts every `.delay()` call
so the pytest suite never touches the real broker (previously true by accident, since nothing
called `.delay()` before this step; now true by design). Live-verified separately (not committed):
one document submitted over real HTTP to a running gateway, with nothing manually driving any
pipeline step, reached `ingested` and became searchable purely through the four real worker
containers within seconds.

**Spec touch-point**: `02` §7's "always automatic" reindex and `03`'s pipeline diagrams are now
literally true end-to-end, not just modeled as explicit-call stand-ins (09 §21's original framing) —
no wording changes needed, since both already described this as the eventual, intended behavior.

## 36. Task Retry/Idempotency Semantics (Phase 2 Step 33)

03 §1: transient failures (a rate-limited/timed-out LLM call) are "retried inside the worker with
backoff. Only exhausted retries transition to error. ... The attempt count and failure context go
in the ingestion_log entry's detail." No count or backoff schedule is specified anywhere in
`spec/` — this step's implementation-readiness decision: 3 attempts, delay doubling from 1s.

**Retry lives inside the three real LLM-calling functions, not around the pipeline functions that
call them.** `ingestion.classify_source`/`curate_source`/`_resolve_merge` each wrap exactly one
external call in a `try`/`except` that writes the `error` transition — and that whole function,
not just the call, is not safe to retry as a unit: its first action is a `pipeline.transition` to
`classifying`/etc., and retrying the *whole* function on a second pass would hit that transition
again from a state that's already moved past `submitted`, raising `IllegalTransition`. So
`_retry_transient` (new, `ingestion.py`) wraps only `call_model`/`call_curator_model`/
`call_merge_model`'s own `agent.run(...)` — never the generic `call`/`workspace` parameters a
caller (including every existing test) injects, so the ~300 existing tests that pass a
single-shot fake `call` stay exactly as fast and deterministic as before. Retries on *any*
exception rather than specific provider error types (`openai.RateLimitError` etc.) — Pydantic AI
abstracts the provider (08 §2), so pinning to one SDK's exception hierarchy would be brittle, and
nothing in `spec/` asks for that precision; a permanent failure just costs a few wasted attempts
before giving up, same as a transient one exhausting its budget.

**Attempt count reaches `ingestion_log` without widening classify_source/curate_source's
control flow.** `_retry_transient` raises a `TransientCallFailed(attempts)` once exhausted,
chained (`raise ... from exc`) onto the real underlying exception. A shared `_failure_detail(step,
exc)` helper builds `{"step", "error"}` from `exc.__cause__ or exc` (so the *real* exception type
still shows, not `TransientCallFailed` itself) and adds `"attempts"` only when the exception really
is a `TransientCallFailed` — a test's directly-injected failure (no retry involved) keeps the exact
`{"step", "error"}` shape two existing tests (`test_ingestion.py`, `test_curate_orchestration.py`)
already assert on unchanged.

**The other half of "retried inside the worker": a worker dying mid-task must not silently lose
the job.** `tasks.py` now sets `task_acks_late = True` (a task is only acked once it finishes, not
when a worker picks it up) and `task_reject_on_worker_lost = True`. No blanket `autoretry_for` on
top of this: an *ordinary* exception surfacing from a task (an `IllegalTransition`, an
`InvalidResolutionError`) is a real bug or a race, not a transient failure — retrying it would
fail identically every time, for no benefit. What actually makes a redelivered/duplicate task
execution *safe* rather than corrupting is `pipeline.TRANSITIONS`'s own guard, already built:
re-entering a stage that already advanced past raises `IllegalTransition` loudly rather than
silently duplicating work (`tasks._curate`'s `classified`/`ingesting` branch, 09 §35, already
no-ops cleanly on the one redelivery shape that's actually still legal — resuming right where a
crash left off). That existing guard, not a new idempotency-key/lock mechanism, is what step 33
leans on.

**A real gap found live, not by any test**: `acks_late` alone turned out to be close to a no-op.
Verified by killing `worker-classification` (`docker kill`, not a graceful stop) mid-task, right
in the middle of a live classify call's ~8s network wait, then restarting the container. The
source sat at `submitted` for minutes afterward — Celery's Redis transport doesn't detect a
dropped consumer and requeue immediately the way RabbitMQ does; it tracks unacked messages in a
Redis hash and only restores one to its queue once `broker_transport_options.visibility_timeout`
elapses, which **defaults to 3600 seconds**. Fixed by setting it to 600s explicitly (comfortably
above the slowest real path today — `curate_source`'s several sequential LLM-touched page writes —
while keeping genuine-crash recovery bounded to minutes rather than up to an hour). Re-verified
with the timeout temporarily lowered to 15s for a fast repro: the same killed task's source reached
`ingested` entirely on its own once the window elapsed and the restarted worker picked it back up
— no test in the suite can exercise this (it needs a real process kill and a real broker), so this
was a pure live-check finding, exactly the kind step 33's own framing exists to catch.

**Explicitly out of scope**: a sweep to detect/recover a source stuck mid-pipeline with genuinely
no task ever redelivered for it (e.g., a message lost before the broker ever recorded it unacked,
or a permanent single-attempt task-level exception that isn't a transient LLM-call failure). That's
operational tooling in the shape of 05 §3's Maintenance Advisor `reindex`-review-item pattern, not
something `spec/03` §1's transient-retry language asks this step to build — flagged the same way
`09` has flagged other track-2c-shaped gaps rather than silently building or silently skipping it.

**Spec touch-point**: none — `03` §1's transient-retry paragraph is now implemented as described;
no wording changes needed.

## 37. Install/Scaling Docs (Phase 2 Step 34)

Phase 1's accepted-simplifications note gated this doc on real async dispatch existing — "'worker
pools scale independently per job type' isn't a demonstrable property until a worker does
something." Steps 30–33 made it demonstrable; this step writes it up, in the README rather than a
new file (the README is already this repo's operational doc — quickstart, volumes, config — spec/
is the vendor-neutral design, not a deployment runbook for this specific reference implementation).

**Live-verified before writing the claim, not assumed from the design**: two `worker-classification`
replicas (`docker compose up -d --scale worker-classification=2 worker-classification`), four
documents dispatched in one burst — each replica's log showed it picked up exactly two, both
finished correctly, no duplicate or dropped work. Celery's own consumer-group semantics over the
shared Redis broker are what does this, not anything this codebase adds — worth confirming live
anyway, since `-n classification@%h`'s worker name (`tasks.py`) depends on each replica getting a
distinct Docker-assigned hostname, which was never exercised before this step. Also
literally re-ran the exact `--scale worker-classification=3 worker-classification` line the README
now prints, rather than writing a plausible-looking command that was never actually typed.

**README also had two stale claims from before dispatch was wired, fixed while here**: the
"`curl` alone won't take a document all the way to a published, searchable page" line (step 32
made this false) and `spec/09-implementation-notes.md`'s section count frozen at Phase 1's close
("24 sections") despite the file being 36 sections deep by now, both corrected — the kind of drift
this project's own discipline treats as a bug to fix on sight, not scope creep.

**What the new Scaling section deliberately does *not* claim**: Metadata DB partitioning and the
optional cache ([06](06-api-mcp-and-scaling.md) §4, [02](02-storage-and-indexing.md) §6) are named
in spec's scaling table but not built anywhere in this repo — flagged explicitly as `07` roadmap
items rather than silently omitted, so a reader doesn't infer they exist from their absence. The
Gateway's own multi-instance story is described but not load-tested (no real load balancer stood up
here) — called out as the one claim in the section resting on architecture/reasoning rather than a
live run, unlike the worker-pool claim next to it.

**Spec touch-point**: none — this section documents this repo's current state against `06` §4's
existing design; no spec wording changes needed.

## 38. 2b Track Verification — Real Async Job Dispatch Closes Out (Phase 2 Step 35)

Closes track 2b, the same shape as `09` §24 (Phase 1 close) and §32 (2a close): a committed,
fast, deterministic test plus a real live run.

**`tests/test_end_to_end_2b.py`** submits through the real gateway, then *drains exactly the
dispatch chain the real wiring produced* — pops each `.delay()` call the autouse `dispatched`
fixture recorded and runs the corresponding real task body (`tasks._classify`/`_curate`/
`_reindex`) in turn, a stand-in for a worker process rather than a reimplementation of one. Mocked
LLM, no live broker — the fast, CI-safe counterpart to the live run below, verifying the *wiring*
is exactly right (classify dispatches curate, curate dispatches reindex for the page it wrote) with
no network calls.

**Live run** (not committed): real HTTP against a running gateway, real broker, real worker
containers, real `gpt-5-nano`, real dev Postgres/MinIO — a shipping-manifest document submitted
with nothing manually driven from that point on reached `ingested` in **36.5s** and was searchable
at the same poll (both well inside a 90s bound, chosen generously above every real run this track's
live checks have seen — the slowest, `curate_source` with several sequential LLM-touched page
writes, has taken up to ~30s). No new bugs found — track 2b's prior steps (30-34) each already
live-verified their own piece as it landed, so this closing run exercises the same wiring those
did, just as the single, formal, tasklist-named "submit and confirm, nothing manual, bounded time"
check rather than another ad hoc one.

**Track 2b (Real Async Job Dispatch) is complete**: real Celery tasks (step 30), worker containers
(step 31), dispatch wired end-to-end (step 32), retry/idempotency semantics (step 33), install/
scaling docs (step 34), and this closing verification (step 35). `00` §7's traceability rows for
async job dispatch and worker-pool scaling are now met.

**Spec touch-point**: none — this section is the closing verification record for 2b, not a new
decision.

## 39. Staleness Detector — `ReviewItem.detail`, and What Signal 2 Can Actually Reach (Phase 2 Step 36)

Track 2c's first detector (05 §2's table, §3), and the first Maintenance Advisor code —
`advisor.py` is new, shared by all five detectors (steps 36-40).

**`ReviewItem` gains a `detail: JSONB` column** (migration `20102d0aa751`, nullable, no backfill).
09 §22 deliberately left `ReviewItem` without one, since ingest-time items (submission/
classification/duplicate) can always read structured evidence back off `ingestion_log`, keyed by
`source_id`. Maintenance Advisor items have no such log — a `reindex`/`prune` finding isn't a
`RawSource` pipeline transition at all — so there was nowhere for 05 §3's "Reason: ..., Estimated
cost: ..., Evidence: ..." to live without either a real column or stuffing formatted text into the
existing `proposed_action` field. Flagged to the user as a real fork before writing any detector
code, since every one of steps 36-40 needed the answer; the column was the clear pick, confirmed
before proceeding. `proposed_action` keeps its existing meaning (a short action slug) for every
kind; `detail` is where evidence, scope, and (per step 38's own wording) a `raised_by` marker for
advisor-raised items all live, without a data model split by kind.

**Two signals, one gap in reach.** 05 §2's staleness signal is "`index_status = stale` for longer
than the threshold, OR a cited `raw_source` was superseded without re-ingestion." Signal 1
(`find_stale_pages`) is direct — join `index_status`/`page_version`, using the current version's
`created_at` as "how long stale" since `index_status` itself carries no timestamp for when
staleness began. Signal 2 (`find_pages_citing_superseded_sources`) can only reach a `raw_source`'s
own dedicated page (`sources/{source_id}.md`, `ingestion._write_source_page`'s literal path
convention) — a concept/entity page's provenance back to whichever source(s) shaped it isn't
tracked anywhere as structured data (citations are free-text footnotes, `01` §6, not an FK), so
there's no way to find "which curated pages cite this superseded source" without inventing a
citation graph nothing else in this codebase needs yet. Scoped explicitly to source pages rather
than silently under-detecting or over-building; worth revisiting if/when a citation FK ever gets
built for another reason.

**Popularity-tiering deferred to the scheduler, per the spec's own framing, not skipped as an
oversight.** `09` §6's SCHEMA.md template gives two threshold values (`high_traffic_days: 90`,
`low_traffic_days: 365`), but `05` §2 explicitly assigns that tiering to "the scheduler... a
tuning detail... not a hard architectural requirement" — step 41, not this one. `find_stale_pages`/
`run_staleness_detector` take a plain `threshold_days` parameter (default 90, the more responsive
tier) so step 41 can pass a per-page/per-tier value later without touching detector internals.

**Batching, not one item per page.** 05 §3: "small, single-page reindexes... do not go through
this review path — only batched/costly reindexes do." `run_staleness_detector` raises exactly one
`reindex` item per workspace per run, with every finding folded into `detail.pages`, and skips
entirely if an equivalent item is already open — necessary since nothing schedules this detector
yet (step 41); a naive re-run without that guard would spam duplicates every time it's invoked.

**Resolution, added now rather than left for a later step.** No step in 36-42 is explicitly named
"wire reindex resolution," but step 42's verify explicitly requires resolving "through the same
endpoints ingest-time items already use" — a review item nothing can resolve is an incomplete
feature, not an appropriately narrow one, so `advisor.resolve_reindex` and its wiring into
`ingestion.resolve_review_item`'s dispatcher (a new early-return branch, since `reindex`'s
`subject_ref` is a `workspace_id`, not a `source_id` the existing generic `RawSource` lookup
expects) landed with the detector. `advisor.py` cannot import `tasks.py` (would cycle:
`tasks -> ingestion -> advisor -> tasks`), so `resolve_reindex` is bookkeeping only — `api.py`
dispatches `reindex` for each `item.detail["pages"]` entry itself, post-commit, the same pattern
already used for a duplicate resolution's `merge` outcome (`09` §35). `"schedule for off-peak"`
(05 §3's third proposed action) gets a clean `InvalidResolutionError` rather than silently
behaving like immediate dispatch — no off-peak scheduling primitive exists anywhere in this
codebase (step 41's Celery beat is recurring-schedule, not one-off-at-a-later-time).

**Live-verified** against real dev Postgres and the real worker containers (not committed): a
workspace seeded with both signals — a page stale 120 days past a 90-day threshold, a superseded
source whose own page had never been re-touched — dispatched to the real `detect_staleness` task,
produced one `reindex` review item with both pages and correct per-page reasons
(`stale_content`/`source_updated`) within 0.1s, resolved over the real HTTP gateway as
`"reindex now"`, and both pages reached `indexed` within 2s via the real `worker-indexing`
container. No LLM calls involved anywhere in this detector (matches 02 §7: reindexing is cheap,
no LLM), so no bugs and no cost either.

**Spec touch-point**: `05` §1's review-item kind table already lists `reindex`; no wording changes
needed — this section records the `detail` column decision and Signal 2's honest scope limit.

## 40. Superseded-Source Detector — Another Missing Timestamp, and What "Delete" Actually Means (Phase 2 Step 37)

Track 2c's second detector (05 §4, retention window already decided at 09 §8: 180 days).

**Same missing-timestamp shape as step 36's staleness signal, same fix.** `RawSource` has no
`updated_at` or equivalent, and `ingestion._resolve_supersede` — the only place `status` ever
flips to `superseded` — never recorded when. Rather than proxy through something indirect (the
successor source's `ingested_at`, which turned out to never actually be set by anything either —
a separate pre-existing gap, left alone rather than fixed in passing here since nothing in this
step needs it fixed), added `RawSource.superseded_at` (migration `da3c87c7d151`, nullable, set at
the exact moment `_resolve_supersede` does its work) — direct and unambiguous rather than another
proxy. Sources superseded before this column existed have nothing to check against and are
skipped by `find_superseded_sources_past_retention`, not assumed either way.

**"Delete superseded source" only ever flips a status column.** 05 §4: "Hard deletion... follows
the object-store lifecycle tiering described in 02 §2 (cold storage before erasure)... unless a
compliance-driven erasure workflow applies." 02 §2 assigns that tiering to *object-store lifecycle
rules* reacting to the `RawSourceStatus` tag (`active`/`superseded`/`archived`/`rejected`) —
external policy, not application code — and `objectstore.delete()` already documents itself as
staging-only, never for a final-key object. So `advisor.resolve_prune`'s `"delete superseded
source"` action does exactly one thing: flips `RawSource.status` from `superseded` to `archived`
(an already-existing enum value, needing no change). Whatever cold-storage/erasure policy a real
deployment configures on its object store is what actually acts on that tag change — this
codebase was never meant to implement that policy itself.

**`resolve_prune` built to extend, not to be reopened per detector.** No prune-raising detector
exists yet beyond this one, but 05 §4 names four reasons (`orphaned`, `low_traffic`,
`superseded_source_retention`, `contradicted_by`) sharing one `ReviewKind.prune`. Branching on
`item.detail["reason"]` — only `superseded_source_retention` implemented, everything else a clean
`InvalidResolutionError` — means steps 39-40 add a branch each rather than a second resolver
function or a kind split; the same one-action-at-a-time growth `resolve_duplicate` already went
through for `reject`/`keep_both`/`supersede`/`merge`.

**Live-verified** against real dev Postgres and the real worker containers (not committed): a
source superseded 200 days ago (past the 180-day default) was flagged, one superseded only 30 days
ago was correctly excluded, the item's `detail.sources` evidence was correct, resolved as "delete
superseded source" over the real HTTP gateway, and the source's status flipped to `archived` while
the recent one stayed untouched. No LLM calls, no dispatch of any kind — this resolution is pure
synchronous bookkeeping, unlike `reindex`'s.

**Spec touch-point**: none — `05` §4 and `02` §2 already describe this lifecycle; this section
records where the timestamp gap was closed and confirms the "app flips a tag, storage policy acts
on it" reading is what got built.

## 41. Existing-Content Duplicate Detector — A Real Import Cycle, and Why This One Isn't Batched (Phase 2 Step 38)

Track 2c's third detector (05 §5), and the first to genuinely diverge from steps 36-37's shape
rather than repeat it.

**Not batched — one review item per pair.** Steps 36-37 batch findings into one item per
workspace because their resolution ("reindex now", "delete superseded source") applies
uniformly across the whole batch. Here, `merge`/`supersede`/`keep_both`/`reject` are inherently
pair-specific — one pair might get merged, an unrelated pair dismissed — so batching them into one
item would force one resolution action onto findings that need independent ones. One item per
pair instead matches how ingest-time `duplicate` items already work (03 §4: always singular, never
batched) more closely than it matches steps 36-37's new pattern. A `max_items`
(default 10) caps how many pairs one run raises, and duplicate-prevention is per-pair (an open
advisor-raised item already covering this exact pair) rather than "any item open for this
workspace," since the latter would block all pairs after the first one ever gets raised.

**`resolve_duplicate` stays completely untouched — a new function, not a variant.** The tasklist
says "reusing resolve_duplicate's ... actions unchanged," which reads most naturally as: same
action *vocabulary*, same familiar admin-facing UX, not literally the same Python function.
`ingestion.resolve_duplicate` is built entirely around a `RawSource` moving through
`PipelineState` — neither exists for two already-published `WikiPage`s. `advisor.
resolve_existing_duplicate` implements the same four action names with page-pair semantics:
`merge` folds the duplicate's content into the primary (LLM call) and archives the duplicate;
`supersede` just archives the duplicate; `reject`/`keep_both` are no-ops that differ only in
their recorded audit label — there's no "new" item here to actually reject, so both leave every
page untouched. `ingestion.resolve_review_item` routes here via `item.detail["raised_by"] ==
"advisor"` — the exact tag the tasklist names — checked *before* the generic `RawSource` lookup,
same as steps 36-37's `reindex`/`prune` branches, since `subject_ref` here is a page_id.

**A real import cycle forced a small refactor.** `merge` needs an LLM call with step 33's
retry-with-backoff, but that lived in `ingestion.py`, and `advisor.py` cannot import
`ingestion.py` (`ingestion -> advisor` already exists for `resolve_review_item`'s dispatch, so
the reverse would cycle: `ingestion -> advisor -> ingestion`). Moved `TransientCallFailed`/
`retry_transient`/`failure_detail` (renamed public, dropping their leading underscores) from
`ingestion.py` into `llm.py` — already dependency-free, already imported by both modules, a
better home now that a second module needs identical behavior rather than a widened `ingestion.py`
API surface. `ingestion.py`'s three call sites and `advisor.py`'s new `call_page_merge_model`
(a second, independent Pydantic AI call — not reused from `ingestion.call_merge_model`, since that
would need the same import `advisor.py` can't make) both call through `llm.py` now. Existing
`ingestion.py` tests for the retry helper moved to a new `tests/test_llm.py` alongside it; one
integration test (`classify_source` records the attempt count correctly) stayed in
`test_ingestion.py`, updated to call through `llm.` instead.

**Incident, not a design decision**: writing `tests/test_llm.py`, a `Write` call overwrote an
existing file — `test_llm.py` already existed from Phase 1 (`llm.resolve_model` tests) — instead
of appending, silently deleting its four original tests for one `pytest --collect-only` cycle.
Caught immediately by watching the total test count drop (336 -> 332) after a change that should
have been count-neutral, not by any tool warning. Fixed by restoring the original four tests
alongside the three new ones. Recorded as a reminder: a file that already exists needs a Read (or
at least a check) before `Write`, not just before *editing* — this project's own tooling requires
a prior Read for `Write` on an existing file, and this slipped through, so treat that guarantee as
a floor to double-check against, not a substitute for looking.

**Live-verified** against real dev Postgres and the real worker containers, including a real LLM
merge call (not committed): two pages seeded with identical bodies were correctly matched (score
1.0, older page = primary) within 0.1s of dispatch, resolved as `merge` over the real HTTP gateway
in 13.1s (real `gpt-5-nano`, wrote a real, sensible change summary), the primary page got a real
new version, the duplicate was archived, and the primary reindexed within 2s via the real
`worker-indexing` container.

**Spec touch-point**: none — `05` §5 already describes this; this section records the batching
divergence, the resolution-function split, and the `llm.py` refactor.

## 42. Orphan/Low-Traffic Detector — A Reason-Scoping Bug in `_open_prune_item`, Surfaced by Adding a Second Prune Reason (Phase 2 Step 39)

Track 2c's fourth detector (05 §2's table row, §4), and the first to reuse `ReviewKind.prune`
for a genuinely different reason than step 37's.

**Both conditions, not either.** 05 §2 is explicit: "zero inbound cross-references... **and**
zero appearances in `query_log`... over the lookback window" — a page with real query traffic but
no incoming links is still in use; a rarely-linked page people keep searching for isn't truly
orphaned. `find_orphaned_pages` runs this as two stages: a cheap, workspace-wide "zero inbound
`page_link`" query first, then a `query_log` JSONB-containment check (`results.contains([{"page_id":
...}])`, SQLAlchemy's JSONB comparator, no raw SQL needed) only against that already-small
candidate set — one query per candidate, matching every other detector's "periodic batch job, not
a hot path" cost shape. The 90-day lookback (`09` §8, `SCHEMA.md`'s
`thresholds.orphan.query_log_lookback_days`) sits inside `query_log`'s own 90-day retention window
by construction, so "zero appearances" can never silently mean "the log already got purged."

**Scoped to real content pages only.** `overview`/`index`/`log` page types are structural
bookkeeping pages that legitimately have zero inbound links (nothing links *to* the overview page)
and were never meant to be prune candidates; `source` pages are cited via free-text footnotes, not
`page_link` rows (the same limit `09` §39 named for step 36's Signal 2), and already have their
own retention detector (step 37) — including them here would double-handle the same subject under
two different detectors. `ORPHAN_CANDIDATE_PAGE_TYPES` is `concept`/`entity`/`comparison` only.

**A real bug in the existing `_open_prune_item`, caught before it shipped.** Step 37's
duplicate-prevention check (`_open_prune_item`) looked for *any* open `prune` item for the
workspace, regardless of reason — correct when only one prune reason existed, but adding this
detector's `orphaned` reason meant an open `superseded_source_retention` item would have silently
blocked every future orphan finding from ever being raised (and vice versa) — the same class of
bug step 38's per-pair duplicate check was already built to avoid, just not yet applied to `prune`.
Fixed by making `_open_prune_item` take a `reason` and filter by `item.detail["reason"]`, the same
client-side comparison step 38 already uses (an open-items list per workspace is always small, so
this isn't the same cost concern as scanning all of `query_log`). `run_superseded_source_detector`
updated to pass its own reason explicitly — this is a real, if narrow, retroactive fix to step 37's
own code, not new step-39 surface area, caught by reasoning through the scenario before writing
`run_orphan_detector`, not by a failing test.

**`resolve_prune` restructured to branch by reason, not by a single hardcoded check.** Now
`superseded_source_retention` → `delete superseded source`/`dismiss`, `orphaned` → `archive
page`/`dismiss` (`WikiPage.status = archived` — search already filters to `published`/`draft`-if-
requested at query time, so an archived page silently stops appearing with no index cleanup or
dispatch needed, same no-dispatch shape as step 37's status flip), everything else still a clean
`InvalidResolutionError` naming `contradicted_by` (step 40) as what's still missing.

**Live-verified** against real dev Postgres and the real worker containers (not committed): four
pages seeded — a truly orphaned page, a page with a real inbound link via step 28's actual
cross-reference parsing, a page found via a real `GET /search` call that wrote a real `query_log`
row, and the "linker" page itself (which turned out to be a genuine orphan too, since nothing
links to *it* either — the live script's own first assertion was wrong about this, not the
detector, caught and fixed before the run counted as passing). The detector correctly flagged
exactly the two genuine orphans and excluded the linked and searched pages; resolved as `archive
page` over the real gateway.

**Spec touch-point**: none — `05` §2/§4 already describe this; this section records the
`_open_prune_item` fix and the content-page-type scoping.

## 43. Contradiction Detector — The First Detector That Spends an LLM Call on Detection Itself (Phase 2 Step 40)

Track 2c's fifth and final detector (05 §2's table row), and the one the tasklist itself flagged
as "net new LLM capability, no prior design to build from." Two forks resolved via
AskUserQuestion before writing any code, since nothing in `09` §6's `SCHEMA.md` template or
anywhere else in `spec/` names a threshold for this:

- **Candidate similarity band: `[0.35, 0.60)`.** Lexical containment (`search.find_similar`,
  same mechanism step 38 uses) can't answer "do these two pages *agree* or *conflict*" — that's a
  semantic judgment nothing but the LLM can make — so the band is purely a cost prefilter: pairs
  below 0.35 don't share enough vocabulary to plausibly be making a claim about the same subject;
  pairs at or above `dedup.DEFAULT_NEAR_DUPLICATE_SCORE` (0.60) are step 38's near-duplicate
  territory already (a near-duplicate is a merge candidate, not a contradiction candidate) — the
  upper bound reuses that existing constant rather than inventing a second number, so the two
  detectors' candidate pools never overlap by construction.
- **Per-run cap: 5 LLM checks**, tighter than step 38's `DEFAULT_MAX_DUPLICATE_ITEMS_PER_RUN`
  (10) on purpose — that cap bounds *items raised*, cheap since step 38 never calls the LLM during
  detection at all (only at `merge` resolution). This cap bounds LLM calls made during *detection
  itself*: every candidate the band surfaces costs a real call whether or not the Curator confirms
  a contradiction, so it has to be the tighter number.

**This is the first detector where lexical similarity alone genuinely cannot do the job.**
Steps 36-39 all worked from data already in Postgres (timestamps, `page_link` rows, `query_log`
rows) or from a bounded lexical score (step 38's near-duplicate containment). "Do two pages make a
conflicting factual claim" has no such proxy — two pages can score anywhere in the similarity band
while agreeing completely (e.g. two systems' parallel incident-response runbooks), or while
genuinely conflicting. So `find_contradiction_candidates` stays a cheap, DB-only prefilter exactly
like step 38's scan, but `run_contradiction_detector` spends a real Pydantic AI call
(`call_contradiction_check`, output type `ContradictionJudgment`) per surviving candidate, asking
the Curator model to decide `contradicts: bool` and, if so, which page (`outdated_page: "a" | "b"`)
should be retired.

**Findings reuse `ReviewKind.prune`, reason `contradicted_by`** — already reserved for exactly this
by `05` §4's reason table and by step 39's own forward-pointing docstring
(`InvalidResolutionError` naming it as "what's still missing"). Pair-specific, not batched, same
shape as step 38's `duplicate` items (an admin resolves each contradiction independently); a new
`_open_prune_item_for_pair` (pair-scoped, unlike the workspace-scoped `_open_prune_item` steps
37/39 use) prevents a naive re-run from re-flagging a pair an admin hasn't resolved yet.
`resolve_prune` gained a third reason branch (`archive page` | `dismiss`, same two actions as the
other prune reasons) — no new resolver function, following the pattern its own docstring predicted.

**`lint_log` stays unbuilt, deliberately** — flagged as a real scope question and resolved via
AskUserQuestion rather than assumed. `02` §5 names `lint_log` as one of `log.md`'s four source
streams, and `09` §23's note ("`lint_log` excepted — no lint pass exists in Phase 1") reads as a
forward pointer to exactly this step. Decided to skip it anyway: steps 36-39 never wrote to any log
stream either, relying entirely on `ReviewItem.detail` as the evidence store (`09` §22's
established pattern) — building `lint_log` now would be the first detector to break that
consistency, for a stream nothing currently reads. `lint_log` remains a named-but-unbuilt stream,
same as before this step, not a regression.

**Live-verified against real dev Postgres and a real `gpt-5-nano` call (not committed), both
directions.** A true-positive case (two pages making a real conflicting claim about Friday
deploys — one allows them, one bans them, tuned via a throwaway scoring script to land at 0.53
similarity) was correctly confirmed as a contradiction in 10.5s, with a coherent explanation
naming which page's claim was stale; resolved via `archive page` over the real code path, and the
flagged page's status flipped while the other stayed published. A true-negative case (two
same-band, topically-related but non-conflicting procedures — a database incident runbook and an
unrelated cache-warmup procedure, tuned to 0.42 similarity) correctly raised zero review items —
worth checking explicitly rather than assuming the model wouldn't over-flag, since this is the
first LLM call in the codebase making a bare `bool` judgment on real content with no verification
step of its own.

**Spec touch-point**: none required — `05` §2/§4 already named `contradicted_by` as a reason and
`prune`/`reindex` as the possible outcomes; this section records the candidate-band/cap decision
and the `lint_log` scope call.

## 44. Maintenance Advisor Scheduling — Cadence as a Deployment Knob, and a Real Beat Permission Bug (Phase 2 Step 41)

Track 2c's closing step (05 §2's "scheduling philosophy") — the five detectors (steps 36-40) go
from purely callable functions to something a real `celery-beat` process fires automatically.

**Cadence is env-overridable; content thresholds are not — a deliberate asymmetry, confirmed with
the user before writing code.** `config.py` gains `KARPWIKI_MAINTENANCE_INTERVAL_HOURS` (default
24) and `KARPWIKI_MAINTENANCE_CONTRADICTION_INTERVAL_HOURS` (default 168/weekly) — Contradiction
Detection spends a real LLM call per candidate (step 40), so it gets a separate, slower default
than the other four (no LLM cost at detection). The first draft of this step hardcoded both as
Python constants, matching every other detector threshold in `advisor.py`; asked "why hardcode it"
and, after distinguishing *what* was being hardcoded, the user asked for cadence specifically (and
anything like it) to be env-overridable. The distinction that emerged: cadence is a **deployment-
wide operational knob** — how often *this deployment's* beat process sweeps, independent of any
workspace's content — the same category `KARPWIKI_CELERY_BROKER_URL`/`KARPWIKI_LLM_CURATOR_MODEL`
already occupy in `config.py`. Every other detector threshold (staleness days, orphan lookback,
dedup similarity, contradiction band/cap) is a **per-workspace content threshold** that `09` §6's
SCHEMA.md template scopes per workspace, not per deployment — real SCHEMA.md parsing stays out of
scope (`09` §26), so those stay Python defaults with a function-parameter override, and promoting
just cadence to an env var doesn't create the inconsistency it would if applied selectively to
only some content thresholds. The two new staleness tiering values
(`KARPWIKI_STALENESS_HIGH_TRAFFIC_DAYS`/`_LOW_TRAFFIC_DAYS`, defaults 90/365) ended up env vars too
— the user's explicit call after weighing the tradeoff (an inconsistency with every *other*
existing threshold, accepted in exchange for a real ops knob on this specific, newly-added pair)
rather than the recommended "leave them as Python defaults like everything else" option.

**Popularity tiering layers on `find_stale_pages` without changing it.** `find_stale_pages_tiered`
(new, `advisor.py`) calls the existing, untouched `find_stale_pages` once at each tier's day count
— `high_traffic_days` (90) is the more permissive/inclusive bound, `low_traffic_days` (365) the
stricter one — then keeps a page from the permissive result if it's either genuinely high-traffic
(reusing the orphan detector's own query_log-presence check and lookback window as the popularity
signal, rather than inventing a second one) or already present in the strict result on its own
merits. No new field on `StaleFinding`, no join added to `find_stale_pages` itself — every existing
test and call site of that function is untouched. `run_staleness_detector` gained `tiered: bool =
False` (default preserves the exact pre-step-41 flat behavior every existing test already
exercises) plus the tier parameters; `tasks._detect_staleness_tiered` is a new, separate task
(rather than a `tiered` kwarg on `detect_staleness` itself) matching this module's existing
one-task-per-detector shape and sidestepping the `dispatched` test fixture's kwarg-less `.delay()`
lambda entirely.

**The dispatch shape: two thin tasks, not a static per-workspace schedule.** Celery beat's own
schedule config is static and can't know about a workspace created after the process started, so
`dispatch_daily_detectors`/`dispatch_contradiction_detector` are the only two `beat_schedule`
entries — each enumerates `Workspace.status == active` at fire time and re-enqueues the
already-existing per-workspace task (`detect_staleness_tiered`/`detect_superseded_sources`/
`detect_existing_duplicates`/`detect_orphans`, or `detect_contradictions`) for each one, fanning
out across `worker-maintenance` replicas rather than processing every workspace serially inside
one long-running dispatch task (consistent with step 34's established per-queue scaling story).

**A real, step-40 gap found while wiring this up: `detect_contradictions` didn't exist.**
Steps 36-39 each added their own manual-dispatch Celery task alongside the detector itself
(`detect_staleness`, `detect_superseded_sources`, `detect_existing_duplicates`, `detect_orphans`);
step 40 built the Contradiction Detector in `advisor.py` but never gave it a task wrapper — a real
gap in the prior step's own scope, caught only because this step needs every detector to have one
to dispatch. Fixed here rather than treated as this step's own new surface, with the same `call`
test-seam `_classify`/`_curate` already use (this detector's `call` is real work, unlike the other
four, whose LLM calls — if any — happen at resolution, not detection).

**A real bug caught live, not by any test:** `celery-beat` starts as the Dockerfile's non-root
`karpwiki` user, but its default persistence file (`celerybeat-schedule`) writes to `/app` (the
image's `WORKDIR`), which is root-owned from the build — `docker compose up -d celery-beat` failed
immediately with `PermissionError: [Errno 13] Permission denied: 'celerybeat-schedule'`. No test
could have caught this: nothing in the test suite runs a real containerized beat process. Fixed
with `--schedule=/tmp/celerybeat-schedule` (world-writable, and losing this file on a restart only
costs beat one schedule tick's memory of "did I already fire this," not application state — the
detectors' own idempotency guards, e.g. `_open_prune_item`, are what actually prevent duplicate
review items, not this file).

**Live-verified against the real dev Postgres/broker/worker containers** (not committed): a
workspace seeded with a high-traffic stale page (queried once, 100 days stale) and a low-traffic
stale page (never queried, also 100 days stale). Both real `dispatch_*` tasks were called directly
— exactly what a real `celery-beat` tick does — and the real `worker-maintenance` container picked
up and completed every dispatched task across every active workspace in the dev DB: the tiered
reindex item correctly named only the high-traffic page (the low-traffic one correctly excluded,
100 days being short of its 365-day bar).

The first contradiction check used the wrong test content and, in doing so, caught a real gap in
this codebase's own test coverage rather than a bug: the pair borrowed from `test_advisor.py`'s
unit tests ("restart daily via an automated script" vs. "restart weekly via a manual checklist")
scored correctly into the candidate band, but the real `gpt-5-nano` call correctly judged it *not*
a contradiction — two restart cadences can reasonably coexist, unlike step 40's proven "Friday
deploys: allowed vs. banned" pair. That unit-test pair had only ever been exercised against a fake
`call` forcing `contradicts=True`, never against the real model — this is the first time it hit
a live judgment, and the negative result is correct, not a defect. Re-run with step 40's own
proven pair, dispatched directly at the single target workspace (`detect_contradictions.delay`)
rather than re-sweeping every active workspace a second time: confirmed and flagged correctly,
explanation named the specific Friday/weekend conflict, resolved via `archive page` over the real
code path.

This run also surfaced a real operational fact worth recording rather than quietly fixing: **24
workspaces in the dev DB are currently `active`**, almost all throwaway debris from this and prior
sessions' live-verification scripts (steps 27, 29, 30, 31, 38-41's own `live*` workspaces, per each
step's writeup) — a real beat tick now sweeps every one of them on every fire, including a real LLM
call per workspace for the Contradiction Detector. Flagged directly to the user rather than
archived unilaterally, since cleanup wasn't asked for and this step's own job is to verify the
scheduler works, not to curate what it sweeps.

**Spec touch-point**: none required — `05` §2 already frames scheduling as scheduler-owned tuning;
this section records the cadence/env-var split, the tiering mechanism, and the beat permission fix.

## 45. Track 2c Closing Verify (Phase 2 Step 42)

Ties together, through the same surfaces ingest-time review items already use, what steps 36-41
built individually: all five detectors, one consolidated queue, resolution through the real
`POST /review-items/{id}/resolve` endpoint — the same pattern step 29 (2a) and step 35 (2b) each
closed their own track with.

**`tests/test_end_to_end_2c.py` (new, committed)**: seeds one workspace with all five signals at
once — a stale page, an orphaned page, a superseded source past retention, a near-duplicate pair,
and a contradicting pair — runs each detector's real task body directly (`tasks._detect_*`,
`task_db`), and resolves every resulting item through the real HTTP `client` fixture: `GET
/review-items` returns exactly 5 items with correct per-kind evidence, each resolved via `POST
.../resolve`, ending with an empty queue. Mocked LLM (a fake `call` for the Contradiction
Detector, the only one that spends one at detection) and no broker (the autouse `dispatched`
fixture) — fast and deterministic, matching test_end_to_end_2a.py/2b.py's own convention.

**A real, useful design conflict found while writing the seed data, not a bug**: nearly every
concept page seeded for one detector is *also* a legitimate orphan (zero inbound `page_link` rows,
zero `query_log` appearances) by the Orphan Detector's own correct definition — the five
signals aren't independent axes in a shared workspace, they overlap by construction whenever
content isn't deliberately cross-linked or queried. Fixed by giving every non-orphan-detector page
a `query_log` entry, which excludes it from orphan candidacy without affecting any other detector
(staleness keys off `index_status`/version age, duplicate/contradiction key off page bodies —
none of them read `query_log`) — keeps each detector's evidence assertion cleanly attributable to
its own seed rather than accidentally asserting on a moving target. Also surfaced (already known
from step 36, reconfirmed here): the Staleness Detector's Signal 1 and Signal 2 land in the *same*
batched `reindex` item, so a workspace seeded with both a stale page and a superseded-source page
gets one item covering both, not two.

**Live-verified against the real dev Postgres, a real running gateway (`uvicorn
karpwiki.api:app`), and a real `gpt-5-nano` call** (not committed) — closing the one combination
no prior step's live check had actually exercised together: a Contradiction Detector item, raised
by a real model call (not the committed test's fake `call`), resolved through the real HTTP
`POST /review-items/{id}/resolve` endpoint (not a direct Python call, which is what step 40's own
live check used). A real conflicting-claim pair (the same proven "Friday deploys: allowed vs.
banned" pair from step 40/41) was correctly confirmed, listed via `GET /review-items`, and
resolved via `archive page` over real HTTP — all in ~10s. The throwaway workspace this created was
deleted immediately after (same cleanup precedent step 41 established once `celery-beat` went
live), and the standalone `uvicorn` process started for this check was stopped afterward — it
isn't part of `docker-compose.yml`'s persistent dev stack.

**Spec touch-point**: none required — `05` §1's consolidated queue and resolution model were
already fully specified; this section records the closing-verify test and its one real live gap.

## 46. Completing the REST Surface — `pages`, the Raw Source Browser, and a Stubbed `connectors` (Phase 2 Step 43)

Track 2d's first step, and the first Phase 2 work outside track 2c. Three real gaps in `06` §1's
resource table, closed together since all three are genuinely small once identified: `pages`
get/list (never built — version history/rollback only ever took a `page_id` param, never a listing
call to discover one), the admin Raw Source Browser (`05` §7), and `connectors` list/configure,
deliberately stubbed until track 2e's real `Connector` model (step 51).

**A real, if small, schema gap found while designing pagination, flagged and resolved via
AskUserQuestion before writing code**: `RawSource` has no timestamp column at all — only
`superseded_at`/`ingested_at`/`source_modified_at`, all nullable and set later in the pipeline, and
nothing records "when was this submitted." Every other list endpoint in this codebase follows one
shared `(created_at, id)` cursor convention (`09` §14), so the Raw Source Browser had nothing to
key a cursor on. Resolved by adding `RawSource.created_at` (migration `e2bd2860a135`,
`server_default=clock_timestamp()`, same pattern `ingestion_log`/`page_version` already use) rather
than the alternative (skip cursor pagination for this one endpoint, sort by the UUID primary key) —
keeps the endpoint consistent with the rest of the API and gives a genuinely useful "most recent
first" browse order. Verified against both a real upgrade/downgrade/upgrade round-trip and a
genuinely empty database, per this project's standing migration discipline.

**`pages` list reuses `search.search()`'s exact tag/date filter semantics without reusing its
code.** `versioning.list_pages` (new) mirrors `search()`'s raw-SQL filter-building style
(`pv.frontmatter -> 'tags' ?| :tags`, `(pv.frontmatter ->> 'date')::date >= :date_from`) against the
identical `page_version.frontmatter` data, so a tag or date filter means the same thing in both
places — but drops the `page_index`/tsvector join and ranking entirely, since this is a plain
catalog browse (`page_type`/`tags`/`date`/`status` filters, no query string), not a search. Sorted
newest-first by the current version's `created_at` (matching `list_versions`' own order), since
`wiki_page` itself has no timestamp of its own.

**Draft visibility reuses `/search`'s already-decided "elevated scope" reasoning (`04` §6), applied
by analogy rather than re-litigated.** `06` §1 gives `pages` blanket "any authenticated caller"
access with no explicit carve-out, but an unreviewed `draft` page is the same content-sensitivity
concern `/search`'s `include_drafts` flag already resolved: `GET /pages`/`GET /pages/{id}` require
`contributor` (not just `reader`) to see or list `status=draft` content; `published`/`archived`
stay reader-visible, since neither carries the same "not yet reviewed" risk. Applied consistently
across both the list and get endpoints (`_reader_page`, mirroring `_admin_page`'s existing shape
one rank down).

**Raw Source Browser stays a "view," per the tasklist's own scoping.** `05` §7's full table row
also names "manually trigger re-ingestion" and "adjust retention" as Repository Management
functions, but phase2-tasklist.md step 43's own text narrows scope to "an admin raw-source browser
**view** of `supersedes` chains" — those two actions aren't built here, flagged explicitly rather
than silently only covering part of `05` §7's row. `GET /sources` returns each source's own
`supersedes` pointer rather than a pre-resolved chain per row: since the endpoint already returns
every source in the workspace, a client reconstructs a full chain by following that pointer through
the same response — no recursive query needed for what's scoped as a browse, not a
chain-resolution endpoint.

**`connectors` is a deliberate stub, not a partial implementation.** `GET /connectors` performs
real admin authorization (mirroring `document-types`' list shape: workspace-scoped when
`workspace_id` is given, "admin somewhere" otherwise) but always returns `{"items": []}`, since no
`Connector` table exists yet. `POST /connectors` checks the same "admin somewhere" bootstrap gate,
then unconditionally raises a `501 not_implemented` — refusing rather than silently accepting and
discarding a payload, so a caller can't mistake this for a working configure endpoint that just
happens to do nothing. Both close out once track 2e's step 51 builds the real `Connector` model.

**Live-verified against real dev Postgres and a real running gateway** (not committed, no LLM
involved — this step is pure CRUD/listing): two pages with different tags/dates, and two sources in
a real `supersedes` relationship. `GET /pages`'s tag and date filters correctly narrowed results
against real JSONB data (not just the test DB's fixtures); `GET /pages/{id}` returned real content;
`GET /sources` correctly 403'd a reader and, as admin, showed the real `supersedes` pointer and
real `clock_timestamp()`-derived `created_at` ordering; `GET /connectors` returned an empty list
and `POST /connectors` correctly 501'd. Cleaned up the throwaway workspace and stopped the
standalone `uvicorn` process afterward, same as step 42's precedent.

**Spec touch-point**: none required — `06` §1 and `05` §7 already fully describe these resources;
this section records the `RawSource.created_at` decision and the draft-visibility/stub-scope calls.

## 47. Performance Monitoring Dashboards (Phase 2 Step 44)

`05` §8's five dashboards, exposed as five new `GET /metrics/*` admin endpoints (a genuinely new
REST surface — `06` §1's resource table never named one, unlike `pages`/`sources`/`connectors` in
step 43). `00` §1 scopes this whole implementation to "admin console scope, not pixel-level UI
design," so this is backend data a real dashboard would render, not a UI — the same framing every
other admin-console feature (review queue, version browser, workspace management) already followed.

**Two real forks resolved via AskUserQuestion before writing code**, since the five dashboards vary
wildly in feasibility given what's actually built:

- **Search latency** (`p50`/`p95`): nothing recorded how long a `/search` call took. Added
  `QueryLog.duration_ms` (migration `f8fb063dacdb`, nullable — historical rows have nothing to
  backfill) and a wall-clock timer around `/search`'s existing work, timed from the top of the
  handler (not just the retrieval call) since that's what a caller actually experiences.
  `monitoring.search_performance` computes `percentile_cont(0.5|0.95)` over recent
  `query_log.duration_ms` rows via raw SQL (`text()`, matching `search.py`'s own established
  pattern for anything beyond ORM's comfortable zone) and flags a real `p95 > 1000ms` SLA breach
  against `06` §1's already-documented target.
- **Queue depth**: real Celery/Redis queue lengths, not a placeholder. `monitoring.queue_depths()`
  is the first code in this repo where `api.py`'s dependency graph reaches Redis directly (every
  other read here is Postgres-only; `tasks.*.delay()` is the only prior Redis touchpoint, and that's
  write-only) — a bare `LLEN` per `tasks.QUEUES` name via `redis.asyncio`, since Celery's Redis
  transport keys each queue by its plain name and no Celery inspection API is needed. Kept as its
  own function (no DB session) rather than folded into `ingestion_pipeline()`, so that function
  stays pure-DB and testable without a live Redis connection — the `/metrics/ingestion-pipeline`
  endpoint merges both into one response.

**Two accepted gaps, documented in `monitoring.py`'s own module docstring rather than silently
missing or faked**: **cache hit rate** (`02` §6's optional cache layer was never built in this
implementation — flagged roadmap-only since step 34's writeup; nothing to report a rate for, so
`search_performance()` returns `cache_hit_rate: None`) and **storage trend** (no metrics-history/
time-series mechanism exists anywhere in this codebase; `storage_utilization()` reports a current
snapshot only, `trend: None`). Neither needed a question — unlike the two forks above, there was no
real alternative to consider (no cache exists at all; building a time-series subsystem is clearly
outside "add a monitoring endpoint").

**`IndexStatus` has no "entered this state at" timestamp, the same gap `advisor.find_stale_pages`
already worked around (`09` §39) — reused its exact proxy rather than inventing a second one.**
`index_health`'s "stuck beyond threshold" count uses `COALESCE(last_indexed_at, current_version's
created_at)` against a 24-hour default threshold, consistent with the advisor's own reasoning for
why `last_indexed_at` alone (the last *successful* index, not "since when has this needed one")
isn't sufficient on its own.

**Storage figures are content-byte approximations, not real Postgres storage accounting** — `SUM
octet_length(page_version.content)` and `SUM pg_column_size(page_index.tsv)` per workspace, not
`pg_total_relation_size` (which sizes whole tables, including indexes/TOAST/WAL/free space, and
can't be filtered per workspace at all). Documented directly in `storage_utilization`'s own
docstring so the approximation isn't mistaken for exact accounting later. Object store volume is
real, via a new `objectstore.size_bytes()` using fsspec's generic `fs.du()` — works across every
backend this module already supports with no backend-specific branch.

**Every dashboard but `queue_depths()` takes an optional `workspace_id`**, reusing `document-types`'
already-established optional-scope shape exactly (`_require_admin_scope`, new in `api.py`, shared
by all five endpoints): scoped and admin-gated to one workspace when given, "admin somewhere"
otherwise. `queue_depths()` is the deliberate exception — a Celery queue mixes every workspace's
work, so per-workspace depth isn't a coherent concept, reported globally regardless of the caller's
`workspace_id` param.

**Live-verified against real dev Postgres, a real running gateway, and the real Redis broker** (not
committed): three real `/search` calls produced real `duration_ms` values, and `/metrics/search-
performance` computed a real p50/p95 from them (3.0ms/3.9ms locally — comfortably inside the 1s
SLA). `/metrics/storage-utilization` first came back with `object_store_bytes: 0` — not a bug: the
live script's first pass only created a page (whose content lives in Postgres, not the object
store) with no accompanying `raw_source`, so there was genuinely nothing under that workspace's
object-store prefix to measure. Re-verified after adding a real raw source to the script: real
non-zero bytes. `queue_depths()` deliberately never pushes synthetic data onto a real queue name in
either the live check or the committed test — this dev environment's real worker containers
actively consume from those same names on the same broker, and a hand-crafted, non-Celery-shaped
message could be popped and crash a live worker; both just confirm the function connects and
reports every known queue.

**Spec touch-point**: none required — `05` §8 already names all five dashboards' metrics; this
section records the `duration_ms`/direct-Redis-read decisions and the two accepted gaps.

## 48. MCP Server (Phase 2 Step 45)

Track 2d's third step: a real MCP server, all ten tools `06` §2's table names, over both
transports it asks for. On-behalf-of delegation for `wiki_submit` (`09` §5) is step 46, not this
one — every tool authenticates as the calling agent's own credential only, for now.

**The installed SDK's API surface is not what "FastMCP" documentation elsewhere describes.**
`08` §4 just says `mcp`, no version; the version that actually installs today is `2.0.0`, whose
high-level server class lives at `mcp.server.mcpserver.MCPServer` (not `mcp.server.fastmcp.
FastMCP`, which doesn't exist in this version at all). Pinned `mcp>=2.0` in `pyproject.toml`
rather than `>=1.0` specifically so a future install lands on an API compatible with what this
module actually calls — worth knowing before assuming any older "FastMCP" example applies here.

**Two real forks resolved via AskUserQuestion before writing code**:

- **Shared logic vs. per-protocol duplication.** `01` §2 frames the Common Gateway as *one*
  shared AuthN/AuthZ/workspace-resolution/dispatch layer with REST and MCP as two protocol
  adapters on top of it — not two independent copies of that logic. The two operations with
  genuinely substantial orchestration (`/search`'s ~90-line federated resolution/taxonomy-
  prefilter/dedicated-index-split/`query_log`-write chain, and `/review-items/{id}/resolve`'s
  multi-branch post-resolution dispatch) were extracted out of `api.py`'s route closures into two
  new module-level functions, `run_search`/`run_resolve_review_item`, that both `api.py`'s REST
  endpoints and `mcp_server.py`'s `wiki_search`/`wiki_resolve_review_item` tools call. This is a
  genuine, behavior-preserving refactor of working, tested code — verified by the full existing
  test suite passing completely unchanged (443 tests, no new failures, no assertions touched)
  before any MCP-specific test was added. The other eight tools are thin enough (one role check
  plus one existing service-layer call — `versioning.list_pages`, `workspaces.list_for_principal`,
  `review.list_items`, etc.) that writing the equivalent code directly in `mcp_server.py` matches
  how `api.py`'s own many endpoints already look; extracting a shared helper for each would be
  pure ceremony, not genuine gateway-orchestration reuse.
- **stdio identity.** `06` §2 wants both `stdio` and streamable HTTP. Streamable HTTP carries real
  per-request headers (`ctx.headers`, populated by the SDK whenever the transport is HTTP-based) —
  a straight reuse of the existing `Authenticator` interface, identical to how `api.py`'s
  `_principal` dependency already works. `stdio` carries none at all (`ctx.headers` is `None` —
  confirmed live, not just from the docstring, at `python -m karpwiki.mcp_server`'s real entry
  point below), since a stdio server is one local process for one caller. Resolved once, lazily, on
  first tool call from `KARPWIKI_MCP_USER`/`KARPWIKI_MCP_GROUPS` env vars — the same lightweight
  stand-in precedent `TrustedHeaderAuthenticator` itself set in Phase 1 ("so Phase 1 need not wait
  on an IdP"), reusing that exact class rather than inventing a new `Authenticator` implementation:
  the env vars are just fed through as a synthesized `headers` dict with the same header-name
  keys `TrustedHeaderAuthenticator.authenticate` already expects.

**`_resolve_http_principal`/`_resolve_stdio_principal` are standalone functions, not both buried
in one closure**, specifically so the header-based path is unit-testable without any real
transport, while the stdio path's server-instance-scoped caching (`stdio_principal`, `nonlocal`)
stays where it has to live — inside `create_mcp_server`'s closure, mirroring `api.create_app`'s
own factory shape (an injectable `Authenticator`, real `TrustedHeaderAuthenticator` by default).

**`wiki_submit` accepts pasted text only** — `POST /sources`'s file/URL input modes don't map
cleanly onto MCP's JSON-shaped tool arguments, nothing in `06` §2's tool table calls for file/URL
support specifically, and every existing test in this codebase already exercises the text path as
primary. A real gap, not silently dropped — flagged in both the module docstring and here.

**Testing an MCP server needed real protocol machinery, not raw function calls.** The ten tool
closures aren't individually importable (they're defined inside `create_mcp_server`'s closure,
mirroring `api.py`'s own `_register_routes` shape), and calling `MCPServer.call_tool()` directly
with no real transport raises `ValueError: Context is not available outside of a request` — `ctx.
headers` needs a real `request_context`, which only a real transport populates. `tests/
test_mcp_server.py` uses `mcp.client.client.Client(server)`, an in-process client the SDK ships
specifically for this, which runs calls through the real protocol (real argument validation, real
`Context` machinery) with no network involved — `ctx.headers` is `None` over this transport
(same as real stdio), so every committed tool test exercises the stdio env-var identity path;
`_resolve_http_principal` gets its own direct unit tests for the header path since no committed
test exercises a real HTTP transport.

**Live-verified against real dev Postgres, both real transports** (not committed): a real
`streamable_http_client` connection with an `X-Karpwiki-User` header round-tripped through a real
running `run_streamable_http_async` server and found real indexed content; a second connection
with no header correctly errored (`No authenticated principal on this request`), not crashed. A
real `python -m karpwiki.mcp_server` subprocess, launched exactly as a real local agent/IDE
integration would via `stdio_client`, listed all ten tools and correctly resolved `KARPWIKI_MCP_
USER` from its subprocess environment. Cleaned up both throwaway workspaces afterward.

**Spec touch-point**: none required — `06` §2 already names the ten tools and both transports;
this section records the shared-logic-vs-duplication call, the stdio identity mechanism, and the
installed SDK's real API surface.

## 49. MCP On-Behalf-Of Delegation, Implemented (Phase 2 Step 46)

`09` §5 already made every real design decision here — the dual-identity AuthZ rule, the
`acting_as: user:<id>` claim shape, `submitted_by`/`author` recording the represented user, the
agent's own identity going into `ingestion_log` detail rather than a new core field. This step is
the implementation of that decision, not a new one — only one genuine adaptation was needed, plus
one deliberate scope boundary.

**"Contributor on the target workspace" has no literal target workspace at submission time.**
`09` §5's rule is written as if a target workspace is already known, but `wiki_submit` (the only
operation `06` §2 names as delegatable) works exactly like the plain, non-delegated submission
path (`03` §2): the workspace is undetermined until classification runs later. The existing
non-delegated check already handles this by asking "does the caller have `contributor`
*anywhere*" (`any_workspace_with_role`) rather than against one workspace. The delegated version
extends this the same way, for two principals: `wiki_submit`'s `acting_as` branch computes the
agent's own contributor-workspace set and the represented user's contributor-workspace set
independently, then requires their **intersection** to be non-empty — "somewhere in common" rather
than one named workspace, the direct two-principal generalization of the one-principal check
already there. Like the existing non-delegated path, this doesn't guarantee the workspace
classification eventually picks is one where both hold access — that's a pre-existing property of
how classification routes purely on content, identical for delegated and non-delegated submissions
alike, not a gap this step introduces or is scoped to fix.

**Mechanism**: `api._store` (already shared by REST's `POST /sources` and MCP's `wiki_submit`
since step 45) gained one new optional parameter, `extra_detail: dict | None = None`, merged into
the first `ingestion_log` entry's `detail` alongside the existing `object_key`. `wiki_submit`
passes `{"acting_agent": "user:<agent's own id>"}` only on the delegated path; the non-delegated
path passes nothing, so `entry.detail` looks exactly as it always has when `acting_as` is omitted
— verified directly (`"acting_agent" not in entry.detail` for a plain submission).

**A known, deliberate scope boundary, not a silent gap**: `wiki_get_source_status`'s existing
submitter-only check (`source.submitted_by != f"user:{principal.id}"`) is unchanged — it still
matches the literal `submitted_by`, which for a delegated submission is the *represented user*,
not the calling agent. The agent that made a delegated submission therefore can't poll its status
itself. `09` §5 never names status-checking as part of delegation's scope, and the represented
user — who by the AuthZ rule's own requirement holds a real, independent `contributor` credential
— can always check it themselves, so this isn't a capability gap for the represented user, only a
convenience gap for the agent. Flagged in `mcp_server.py`'s own module docstring rather than
silently narrowing `wiki_submit`'s scope without saying so.

**Live-verified against real dev Postgres via the real `python -m karpwiki.mcp_server` stdio
subprocess entry point** (not committed, same mechanism step 45's own stdio live check used): two
real principals, both granted `contributor` — a real delegated submission succeeded, with the
real `raw_source.submitted_by` recording the represented user and the real `ingestion_log` entry's
`detail.acting_agent` recording the calling agent. A second real call, delegating to a principal
holding only `reader`, was correctly rejected with the intersection-empty message. Cleaned up the
throwaway workspace and the one real `raw_source` row the successful delegated call created
(classification had already run against it for real by cleanup time, parking it at
`pending_review` — cleaned up regardless of which state it reached, since it's throwaway data
either way).

**Spec touch-point**: none required — `09` §5 already fully specifies this feature; this section
records the target-workspace adaptation and the `wiki_get_source_status` scope boundary.

## 50. Real OIDC `Authenticator` (Phase 2 Step 47)

The second `Authenticator` implementation `09` §15 always intended: `OidcAuthenticator`, real
bearer-JWT validation against a configured IdP's JWKS, swapped in for `TrustedHeaderAuthenticator`
with no handler changes once configured — the exact property §15's pluggable-provider design was
built to deliver.

**A real, confirmed spec/implementation-stack mismatch, resolved via AskUserQuestion**: `08` §2
names "Authlib (OIDC/SAML)" as the auth library, but Authlib has no SAML support at all — checked
directly against the installed package (`authlib.oauth1`/`oauth2`/`oidc` only, no `saml` module
anywhere). Real SP-side SAML (XML signature validation, IdP metadata exchange, an assertion-
consumer endpoint) is a materially larger, separate feature needing a different library
(`python3-saml`/`pysaml2`), and nothing in `08` §4's dependency list or any tasklist step names
one. Resolved as: build real OIDC only, document SAML as unsupported by this pick rather than
silently missing — the same "flag it, don't build around it silently" treatment step 45 gave the
MCP SDK's own real API-surface surprise.

**`Authenticator.authenticate` became `async`, a real interface change confirmed with the user
first.** Phase 1's `TrustedHeaderAuthenticator` does no I/O, so a synchronous `authenticate` never
mattered; `OidcAuthenticator` fetches/caches a JWKS over the network, and blocking the event loop
on every cache miss is the wrong tradeoff for a gateway meant to serve more than one request
concurrently. The change touched three call sites — `api.py`'s `_principal` dependency and
`mcp_server.py`'s two principal-resolution helpers — all already inside async functions, so each
needed only an added `await`; `TrustedHeaderAuthenticator.authenticate` itself needed only
`async def`, no logic change, and the full pre-existing test suite caught every missed call site
immediately (four failing tests, all fixed by adding `await`/`async`).

**Uses `joserfc`, not `authlib.jose`, for the actual JWS/claims verification.** `authlib.jose` is
deprecated as of Authlib 1.7 ("please use joserfc instead") — `joserfc` is already an Authlib
dependency, not a separate library choice, and is what Authlib's own current documentation points
to. `authlib` itself contributes little concrete code to this implementation beyond being the
named dependency `08` §2 picked; the real validation logic is `joserfc.jwt.decode` (against a
`joserfc.jwk.KeySet` built from the fetched JWKS) plus `joserfc.jwt.JWTClaimsRegistry` for
`iss`/`aud`/`exp` validation. Confirmed empirically before writing `OidcAuthenticator` itself:
minted a real token with a real generated RSA keypair, decoded and validated it, and confirmed
wrong-audience/unknown-`kid`/expired cases each raise their own distinct, catchable error type
(`InvalidClaimError`, `InvalidKeyIdError`, `ExpiredTokenError`) rather than one generic failure.

**JWKS caching**: fetched once (via OIDC discovery, `{issuer}/.well-known/openid-configuration`,
unless `KARPWIKI_OIDC_JWKS_URI` is set directly) and cached indefinitely on the `OidcAuthenticator`
instance, refetched exactly once, inline, whenever a token's `kid` isn't in the cache — the
standard client pattern for surviving IdP key rotation without polling on a timer. A per-instance
`asyncio.Lock` serializes concurrent refreshes. `http_client` is injectable and built fresh per
instance rather than at module scope — a module-level async client bound to whichever event loop
is running on first use already broke across test functions once in this codebase (`09` §29's
OpenSearch-client lesson); each `OidcAuthenticator` getting its own client avoids that failure mode
by construction rather than by remembering not to repeat it.

**A real interaction bug caught before it shipped, not by a test but by re-reading what step
45/46 already built**: `stdio`'s identity resolution synthesized only `x-karpwiki-user`-shaped
headers from env vars — correct for `TrustedHeaderAuthenticator`, but `OidcAuthenticator` only
ever looks at an `Authorization: Bearer` header, so a deployment that configured real OIDC would
have silently broken stdio MCP auth entirely (every stdio tool call would 401-equivalent, with no
code path able to succeed). Fixed by adding `KARPWIKI_MCP_TOKEN` — a real bearer token, tried
first — falling back to `KARPWIKI_MCP_USER`/`_GROUPS` when unset, so the pre-existing
(`TrustedHeaderAuthenticator`-backed) default behavior from steps 45/46 is completely unchanged.
A bare local stdio process can't run an interactive OIDC login itself, so this is the only shape
that could possibly work — the caller has to already hold a token from somewhere.

**Live-verified against a real local HTTP server acting as the IdP** (not committed, unlike the
committed unit tests' `httpx.MockTransport`) — a real `http.server.HTTPServer` served a real
discovery document and JWKS on a real port; a real `uvicorn karpwiki.api:app` subprocess, started
with only `KARPWIKI_OIDC_ISSUER`/`_AUDIENCE` set (no code change, no `KARPWIKI_OIDC_JWKS_URI` —
exercising real discovery, not just a direct JWKS URI), correctly accepted a real signed token on
`GET /workspaces` (200) and correctly rejected no token, a wrong-audience token, and an expired
token (401 each) — confirming `default_authenticator()` really does swap providers from
configuration alone. The `KARPWIKI_MCP_TOKEN` fix was verified the same way: a real
`python -m karpwiki.mcp_server` stdio subprocess, launched against the same real IdP-issued token
with real OIDC active, authenticated correctly.

**Spec touch-point** (applied): `08` §2's "Authlib (OIDC/SAML)" line now needs read alongside this
section's SAML caveat — no wording change to `08` itself, since `09` is explicitly the
implementation-readiness appendix for exactly this kind of gap; `06` §3 needed no changes, its
auth model already anticipated a real OIDC provider without specifying the library-level details
this section fills in.

## 51. Rate Limiting (Phase 2 Step 48)

The `RateLimit-*`/`Retry-After` header contract §14 above already specified, with a real limiter
behind it at last: a Redis-backed fixed-window counter (`INCR` + `EXPIRE` on first hit), scoped to
the REST gateway only — MCP's own protocol has no HTTP header concept, and `stdio` transport has
no headers at all, so neither `06` §2's tool table nor this tasklist step named rate limiting for
MCP.

**Scope confirmed via AskUserQuestion before building, not assumed.** `07` §3 asks for "per-
principal and per-workspace" limits, but `workspace_id` isn't a uniform request parameter — it's
absent from taxonomy-pre-filter submissions and not-yet-classified sources. Chose "principal +
category limits always-on; workspace limit opportunistic" over deferring workspace limiting
entirely: per-principal throttling (keyed off a coarse, unverified identity — the raw
`Authorization`/`X-Karpwiki-User` header value, SHA-256 hashed before it ever becomes a Redis key
name, matching this project's never-print-a-secret discipline applied to a new surface) covers the
abuse case unconditionally; the per-workspace check only runs when `workspace_id` is already a
plain query/path parameter on the request, rather than duplicating real business logic
(taxonomy/classification) in middleware just to resolve "which workspace" for every endpoint
shape.

**Deliberately does not call the real `Authenticator`.** Re-running `default_authenticator()` —
possibly a real OIDC JWKS network fetch, §50 above — just to bucket a rate-limit counter would be
both wasteful and wrong: an unauthenticated or invalid-token caller still needs throttling, which
the coarse, unverified key provides without needing to *validate* anything. AuthN/AuthZ and rate
limiting stay logically separate checks even though `01` §2 frames them as one Common Gateway
layer.

**Three mutually exclusive categories**, matching `07` §3's own three ("submissions, search
calls, and API requests") by path/method — "API requests" reads as the general catch-all every
other endpoint falls into, not a fourth layer stacked on top of the other two.

**Middleware ordering, verified empirically rather than assumed.** `enforce_rate_limit` is
registered *before* `attach_request_id` in `create_app()` — Starlette wraps `@app.middleware`
registrations in reverse order (last-registered ends up outermost), confirmed with a standalone
test script before relying on it, so `enforce_rate_limit` ends up the *inner* middleware and
always sees a real `request.state.request_id` already set, giving a 429 response body the same
`request_id` a caller would see on any other error.

**A real test-isolation gap, found by running the full suite, not anticipated in advance.** Unlike
the Postgres test DB (dropped/recreated per test by the `session` fixture), the rate limiter's
Redis counters live in the same real, shared Redis instance across an entire pytest run with no
reset between tests — and dozens of test files submit/search/etc. as the same `deepak` principal.
First pass: 4 failing tests (`KeyError: 'source_id'` after `POST /sources` in the 2a/2b end-to-end
tests, plus two 1c tests), because the accumulated per-principal counter from earlier tests in the
same run tripped the real default limits partway through. Two-part fix: (1) moved the
category→limit lookup dict from module-import time into `create_app()` itself, so it re-reads
`config.RATE_LIMIT_*` fresh per app instance instead of freezing values at import — needed because
`tests/conftest.py`'s `client` fixture calls `create_app()` fresh per test, and a frozen
module-level dict would never see a test's monkeypatched config; (2) added an autouse
`generous_rate_limits` fixture (mirroring step 32's `dispatched` fixture's own reasoning) that
monkeypatches every `RATE_LIMIT_*` constant up to 1,000,000 for the duration of each test — keeping
the real, unmocked code path exercised rather than mocking `ratelimit.check` away, while making
the suite's real request volume a non-issue. `tests/test_ratelimit.py` overrides the limit back
down for its own dedicated enforcement test, and deliberately uses a per-test-unique principal
header rather than the suite's shared `deepak`, since the "general" category's real Redis counter
for `deepak` is itself still being incremented by unrelated tests within the same 60s window.

**Live-verified against a real local server, not just the test suite.** A real `uvicorn
karpwiki.api:app` subprocess, started with `KARPWIKI_RATE_LIMIT_GENERAL_PER_PRINCIPAL=5` and
`KARPWIKI_RATE_LIMIT_WINDOW_SECONDS=30`, correctly returned `200` with descending
`RateLimit-Remaining` for the first 5 requests from one principal, then real `429`s with the
standard error envelope (`{"error": {"type": "rate_limited", ...}}`) and a `Retry-After` header for
every request after; waiting out the window produced a fresh `200` with the counter reset,
confirming the fixed-window `EXPIRE` behavior end to end against real Redis.

## 52. Horizontal Gateway Scaling (Phase 2 Step 49)

06 §5's "load-balanced Gateway tier" made real. Worker-pool independent scaling was already built
and live-verified in step 34 (`docker compose up --scale worker-classification=N`); the Gateway
itself had never been containerized at all — only ever run via a bare `uvicorn karpwiki.api:app
--reload` on the host (README). **Scope confirmed via AskUserQuestion before building**: chosen
over a documentation-only "audit statelessness, don't add infra" alternative, since step 50's own
closing verify needs a real second gateway instance behind a real load balancer to run its MCP
end-to-end check against, and this project's consistent discipline has been to build and
live-verify real infra for every other scaling claim (worker containers, `celery-beat`, OpenSearch)
rather than assert it from the design alone.

**`gateway` (new docker-compose service) reuses the existing worker image, not a new one** — same
`Dockerfile`, just `command: uvicorn karpwiki.api:app --host 0.0.0.0 --port 8000` in place of a
Celery worker command, extending the exact convention the four worker services already established
(one shared image, `command:` selects the role). `expose`, not `ports`: the service is meant to be
scaled (`--scale gateway=N`), so a static host port mapping would collide across replicas — only
`nginx` publishes a host port.

**`nginx` (new service) is the load balancer**, `nginx.conf` (new file) round-robining across
however many `gateway` replicas are up. The one real implementation subtlety: a bare `proxy_pass
http://gateway:8000;` resolves the hostname once, at nginx startup, and caches that single
container's IP for nginx's lifetime — new replicas added later via `--scale` would never receive
traffic, and Docker's own embedded DNS round-robining across a scaled service's multiple containers
would be invisible to nginx entirely. Fixed with the standard Docker Compose + nginx pattern: a
`resolver 127.0.0.11 valid=10s` (Docker's embedded DNS) plus routing `proxy_pass` through a `set
$upstream` variable, which forces nginx to re-resolve per the resolver's TTL instead of once at
startup.

**`GET /healthz` (new route) is the one new application-code surface this step needed** — Docker's
own per-replica healthcheck needs *something* to poll, and every existing endpoint requires a real
principal. Deliberately exempted from `enforce_rate_limit` entirely (an early return in the
middleware, not folded into the "general" category): every replica's own healthcheck presents no
auth header, so without the exemption every replica's healthchecks would all increment the *same*
shared "anon" Redis counter (step 48's rate limiter is intentionally principal-keyed, not
per-instance) — at enough replicas, healthchecks alone could exhaust that shared bucket and start
failing each other's own liveness probes, a self-inflicted failure mode worth naming even though it
never actually manifested at the replica counts tested here.

**Live-verified against real containers, not asserted from the design.** Built the image, brought
up the full stack, scaled `gateway` to 3 replicas (all reporting Docker-healthy independently), then
fired 15 requests at `nginx`'s published port and confirmed via each container's own logs that the
traffic actually split across all three (7/5/3, not all on one) — proving real round-robin
distribution, not just that multiple containers happened to be running. Ran one full submit → real
`gpt-5-nano` classify/curate → index → search round trip through the load balancer (not a bare-GET
smoke test) to confirm the LB-fronted path works for genuinely stateful, multi-request-pipeline
traffic, not only idempotent reads. Scaled back to 1 replica afterward (no reason to leave 3 idle
app containers running); the throwaway workspace created for the live check was deleted before
finishing, since `celery-beat` is live in this stack and would otherwise eventually sweep it with a
real paid Contradiction Detector LLM call, the same reasoning behind step 42's earlier cleanup.

**Spec touch-point** (applied): `06` §5's "minimal deployment... load-balanced Gateway tier" is now
demonstrated, not just described — no wording change needed to `06` itself, since its own text
already anticipated exactly this shape ("the spec doesn't mandate a specific orchestrator... all
fit this shape"); `nginx` here is one concrete instance of "standard orchestration," not a new
requirement.

## 53. 2d Closing Verify (Phase 2 Step 50)

Closes out track 2d (steps 43-49: complete REST surface, monitoring dashboards, MCP server,
on-behalf-of delegation, real OIDC, rate limiting, horizontal gateway scaling). Two independent
claims, verified separately since one is a wiring guarantee a committed test can capture and the
other is an infra claim that genuinely needs real containers.

**Committed**: `tests/test_end_to_end_2d.py` (new) — search, submit, and (as admin) resolve a
review item, entirely through the MCP protocol adapter (`mcp.client.client.Client`, same in-process
pattern `test_mcp_server.py` already uses), not the REST surface. Drains the real dispatch chain a
real `wiki_submit` call produces (mocked LLM/no broker, same convention as `test_end_to_end_2b.py`)
so `wiki_search` finds a genuinely curated-and-indexed page rather than a stub — the fast,
deterministic counterpart to the live check below, matching every prior closing-verify file's
split.

**Live-verified, MCP client against real infra (real dev Postgres, real Redis-dispatched workers,
real `gpt-5-nano`)**: a real `python -m karpwiki.mcp_server` stdio subprocess per identity (mirroring
steps 45-47's own live-check convention) ran submit → poll → search → admin-resolve as one real
flow. **A genuine, non-bug surprise caught mid-check**: the first two attempts landed at
`pending_review` instead of `ingested` — not a bug, but two different legitimate real outcomes this
step's own script hadn't accounted for: the first was the classifier's real confidence landing
below the auto-accept threshold (`03` §3/§5's designed review path); the second, after retrying
with near-identical wording, was the dedup detector correctly flagging the resubmission as a
near-duplicate of the first attempt's own already-classified source. Both are exactly the review
paths `03`/`05` design for, not failures — fixed by having the script resolve the classification
review item as admin (`action=<document_type>`) when that path is hit, and by using clearly
distinct content on the clean rerun rather than fighting the dedup detector. The clean run
completed for real: submit → real classify → real curate (multiple pages: a `source` page, a
`concept` page, and the workspace `overview.md` update) → real index → real search finding it →
real admin resolution of the submission's own review item.

**Live-verified, no session-affinity requirement**: reusing step 49's real `gateway`/`nginx`
docker-compose infra, scaled to 2 replicas. One `httpx.AsyncClient` (one persistent connection —
the same shape a real browser session uses) ran a REST submit → poll → search sequence against
`nginx`'s published port. Container logs confirmed the *individual requests within that one logical
session* landed on different replicas — `POST /sources` hit `gateway-2`; the subsequent
`GET /sources/{id}` polls for that same source interleaved across both `gateway-1` and `gateway-2`;
the final `GET /search` hit `gateway-1` — and every request succeeded regardless of which replica
served it, since all real state lives in shared Postgres/Redis, never in-process. This is the literal
claim step 50 asks for, not just "multiple containers happen to be running" (step 49's own check):
a single caller's multi-request flow is provably not pinned to one instance.

**Cleanup**: both live checks' throwaway workspaces (`live50-mcp-check` and, mid-check, some debris
from the two non-bug retry attempts above) were fully deleted from the real dev DB before finishing
— `celery-beat` is live in this stack, so anything left behind would eventually cost a real paid
LLM call, the same reasoning behind steps 42 and 49's own cleanups. Gateway scaled back to 1
replica afterward.

**Spec touch-point** (applied): none — `06` §2 and §5 already fully specified both claims this step
verifies; nothing here required a wording change to either.

## 54. `Connector` Model and API (Phase 2 Step 51)

Track 2e (Connector Framework) starts. `02` §3 already named the full `connector` table
(`connector_id`, `workspace_id`, `type`, `config`, `credential_ref`, `schedule`,
`ingestion_policy`, `state`, `last_sync_cursor`, `last_run_at`) and `06` §1 already named the API
(`connectors`: list, configure) — this step makes both real, replacing step 43's deliberate stub.
Storage and admin CRUD only: the polling worker pool that actually runs a connector (§4/step 52),
credential resolution against a real secrets manager (step 53), and the first concrete connector
type (step 54 — a naming collision with this section number is coincidental, tasklist step 54 is
the Git poller) are separate, later steps.

**`workspace_id` is fixed at creation, never reassignable** — unlike `document_type`. `09` §13's
permission boundary is "contributor on exactly one workspace, never several," and 05 §7 never
lists reassignment as a configurable connector property the way it explicitly does for document
types ("reassign a type's target workspace"). Reassigning would also mean atomically moving the
connector's own `access_policy` grant (below) to a new workspace — real complexity nothing in the
spec asks for, so `connectors.update` simply has no `workspace_id` parameter at all.

**`type` is a plain string, not a closed enum.** `05` §7's own list ("Git repos, websites,
Confluence, Notion, OpenAPI, etc.") is explicitly open-ended, and no concrete connector type is
implemented until tasklist step 54 — inventing a registry now would be building ahead of the step
that owns it.

**`config`/`schedule`/`last_sync_cursor` are opaque JSONB**, deliberately uninterpreted at this
step. `09` §4 calls the sync cursor "connector-type-specific"; `schedule`'s own internal shape
(a plain interval vs. cron vs. webhook-only, per §4's "polling vs. webhook" note) is left to
whichever step actually builds the poller (step 52) — committing to one shape now, before a real
poller exists to consume it, would be a guess this step doesn't need to make. `02` §3 names the
column `schedule`, not `schedule_interval_minutes` or similar, which reads as the spec itself
leaving this open.

**`credential_ref` never accepts a raw secret — a deliberate, flagged scope boundary, not an
oversight.** `09` §13 describes the *eventual* full contract: "accepts a secret write-only on
configure/update and never returns it; reads return the `credential_ref` plus non-sensitive
metadata." Read literally, that implies the API's real endpoint state takes a raw secret in and
exchanges it for a ref via the secrets manager — but that exchange is exactly what step 53
("Credential resolution via the secrets-manager interface") is scoped to build, and no secrets
manager integration exists yet. Storing whatever a caller passes directly into `credential_ref`
would risk exactly what `09` §13 forbids twice over ("Connector secrets are never stored in the
Metadata DB... any log stream") the moment an admin pastes a real API key into the field thinking
it's write-only-and-safe. Resolved as: `credential_ref` accepts only a pointer the caller already
holds from their *own* secrets manager — documented explicitly in the Pydantic model's docstring —
so nothing in this step's code path ever touches a raw credential. Step 53 will need to add the
real write-only-secret-in path; this step's `credential_ref` field name and contract can stay
exactly as-is underneath it. Confirmed against the model, not asked of the user — no genuine two-
sided fork remained once `09` §13's own "never stored" rule ruled out the alternative.

**`connector:<connector_id>` gets its `access_policy` grant automatically, at creation time.**
`09` §13 states the permission boundary ("granted `contributor` on exactly one workspace") as an
established fact about every connector, but nothing else could ever create that row — there is no
separate "invite a connector" flow the way there is for a user or group joining a workspace.
`connectors.create` inserts both rows in one transaction. Live-verified this specifically: a real
`POST /connectors` through the real, load-balanced gateway produced a real `access_policy` row for
`connector:<id>` with role `contributor`, confirmed by querying real dev Postgres directly.

**`ingestion_policy` stays a plain string** (`"auto"`/`"gated"`), matching
`ingestion.check_duplicates`'s own existing parameter convention exactly rather than introducing a
new enum for a two-value field nothing else in the codebase treats as one. Validated at the API
layer (400 on anything else) since, unlike `ingestion.py`'s call sites, this is admin-supplied
external input.

**Migration required an explicit downgrade fix, caught by this project's own round-trip
discipline, not assumed clean.** Alembic's `create_table`/`drop_table` autogenerate doesn't create
a symmetric `DROP TYPE` for the new `connector_state` enum — `drop_table` alone leaves the
Postgres enum type orphaned, so a downgrade-then-upgrade round trip failed with `type
"connector_state" already exists` on the first attempt. Fixed by adding an explicit
`sa.Enum(name='connector_state').drop(op.get_bind(), checkfirst=True)` to `downgrade()` after the
table drop. Re-verified clean: upgrade → downgrade → upgrade round trip against real dev Postgres,
and a full `upgrade head` against a genuinely empty throwaway database, both clean.

**Live-verified against a real, load-balanced gateway** (reusing step 49's `gateway`/`nginx`
infra, rebuilt with the new image): real `POST /connectors` → `GET /connectors` →
`POST /connectors/{id}` (disable) all succeeded through `nginx`'s published port against real dev
Postgres; a real unauthorized caller correctly got `403`. Cleaned up the throwaway workspace/
connector afterward.

**Spec touch-point**: none required — `02` §3 and `06` §1 already fully specified the table and
API this step builds; nothing here needed a wording change to either.

## 55. Connector Polling Worker Pool (Phase 2 Step 52)

09 §4's execution model made real: its own queue (mirroring 06 §4's per-job-type pools), a
`celery-beat` dispatcher, and the generic fetch/diff/create-`raw_source` orchestration — with no
concrete connector type to actually fetch anything yet (step 54 is the first one, a Git poller).

**The adapter registry is deliberately empty.** `connector_polling.py`'s `ConnectorAdapter`
Protocol + `ADAPTERS: dict[str, ConnectorAdapter]` mirrors `auth.py`'s pluggable `Authenticator`
shape (09 §13 already names that precedent for step 53's credential work; the same shape fits
step 52's "no concrete type yet" problem too). A connector whose `type` has no registered adapter
is not an error — `type` is deliberately open-ended (step 51) and nothing stops an admin from
configuring one ahead of its adapter landing — so `poll_connector` records a clean
`"unsupported_type"` outcome and leaves the connector `enabled`, not `disabled_auth` (that state
is reserved specifically for auth failures, 09 §13).

**Diffing against the cursor is the adapter's job, not the generic orchestrator's.** 09 §4 calls
the cursor "connector-type-specific" — a git SHA and a page-id/timestamp set have nothing in
common to diff generically, so `ConnectorAdapter.poll(connector, credential_ref)` receives the
whole `connector` row (including its current `last_sync_cursor`) and returns
`(new_or_changed_items, new_cursor)` itself. The generic orchestrator's job is steps 1-3's
sequencing and the create-`raw_source` call, nothing about *how* to diff.

**A real circular-import forced extracting `_store` out of `api.py`, into `ingestion.store`.**
`connector_polling.py` needs the same "create a raw_source exactly as if a user uploaded it" call
`api.py`'s `POST /sources` and the MCP `wiki_submit` tool already use — but `api.py` imports
`tasks`, and `tasks.py` needs to import `connector_polling` to dispatch `poll_connector`, so
`connector_polling` importing `api` would cycle (`api -> tasks -> connector_polling -> api`).
Moved `_store` to `ingestion.py` as `ingestion.store` (unchanged behavior) — `ingestion.py`
imports neither `api` nor `tasks`, so all three callers (`api.py`, `mcp_server.py`,
`connector_polling.py`) can import it safely. This is the same kind of extraction step 45 did for
`api.run_search`/`api.run_resolve_review_item` (REST+MCP sharing one implementation), just forced
by the async layer rather than by a second protocol adapter.

**Credential handling stays exactly as scoped in step 51**: `connector.credential_ref` — the
caller-supplied secrets-manager pointer, never a raw secret — passes straight through to the
adapter unresolved. No real secrets-manager fetch exists until step 53; an adapter built today
would receive the opaque pointer string, not a resolved credential.

**Run-outcome recording uses the new `Connector.last_run_detail` column decided via
AskUserQuestion before building** (not `ingestion_log`, which is shaped for one raw_source's
pipeline-state transitions and can't represent a zero-item or pre-fetch-failure run — see
`models.Connector`'s own docstring for the full reasoning). Every run updates `last_run_at`/
`last_run_detail` regardless of outcome (`"ok"`/`"unsupported_type"`/`"auth_failed"`/`"error"`) —
a poll that finds nothing new is still a completed run an operator can see happened, not a silent
no-op. A generic fetch error (network blip, source-system 500) leaves the connector `enabled` and
simply retries on its next scheduled run; only `ConnectorAuthError` flips it to `disabled_auth`
(09 §13's specific "auth failure disables rather than retries" rule) — these are deliberately
different outcomes, not the same catch-all.

**Dispatch mirrors step 41's exact shape**: `_dispatch_connector_polls` enumerates *enabled*
connectors at fire time (a connector created after `celery-beat` started must still get picked
up, same reasoning as the two detector dispatchers) and re-enqueues `poll_connector` for whichever
are due. "Due" reads `connector.schedule.get("interval_minutes")` against `last_run_at` — a
connector with no `interval_minutes` configured is never auto-dispatched (09 §4: polling is the
default, but a connector could be webhook-only or simply not yet scheduled). The dispatcher's own
tick cadence is a new env var, `KARPWIKI_CONNECTOR_DISPATCH_INTERVAL_MINUTES` (default 5m) —
deployment-wide operational tuning, same category as the maintenance cadence vars, not a
per-connector setting.

**Live-verified against real infra in two parts**, since no real adapter exists yet to exercise
the full path in one shot: (1) the real dispatch mechanics — a real `worker-connector-polling`
container (new, rebuilt image), real `celery-beat` (restarted with the new schedule entry), and a
real connector configured with an unregistered `type` — dispatched for real through the real
broker and correctly recorded `{"outcome": "unsupported_type", ...}` in real dev Postgres, state
staying `enabled`. (2) the create-`raw_source` success path — a throwaway stub adapter registered
in-process (not through the container, since `ADAPTERS` is an in-process dict step 54 will
populate for real) drove `poll_connector` against the same real dev Postgres/object store: a real
`raw_source` was created (`submitted_by=connector:<id>`), the cursor persisted, and dispatching
`classify_source` afterward through the real broker was picked up by the real
`worker-classification` container and classified via real `gpt-5-nano` — confirming a
connector-created source really is indistinguishable from a normal submission once past step 3, as
09 §4 claims. Cleaned up the throwaway workspace/connector/source afterward.

**Spec touch-point** (applied): none required — `09` §4 already specified this step's execution
model in full; nothing here needed a wording change.

## 56. Connector Credential Resolution (Phase 2 Step 53)

`secrets_manager.py` (new) — a `SecretResolver` Protocol + `default_secret_resolver()` factory
mirroring `auth.py`'s pluggable `Authenticator` shape exactly, per 09 §13's own note that
credential resolution "follows that same shape." `connector_polling.poll_connector` now resolves
`connector.credential_ref` into the real secret before ever calling the adapter — the adapter's
`poll(connector, credential)` receives the resolved value, never the ref (a deliberate signature
change from step 52, flagged here rather than silently changed, though no concrete adapter exists
yet to actually break).

**Scope confirmed via AskUserQuestion before building**: 09 §13 frames the backend as "a role, not
a product" — Vault, AWS Secrets Manager, GCP Secret Manager, and Kubernetes secrets are all named
as equally valid, unlike OIDC where `08` §2 named a specific library (Authlib) as the stack's own
pick. Built one concrete provider, `EnvSecretResolver` (`credential_ref` names an environment
variable; resolving it reads `os.environ`), over also standing up a real Vault dev-mode server +
`hvac` client. This is not a toy stand-in the way `TrustedHeaderAuthenticator` is (explicitly
"sound only behind a proxy") — it's how a Kubernetes Secret most commonly reaches a running
process in the first place (injected as a pod env var), so it's genuinely production-viable, not
just a local-dev convenience. A deployment backed by Vault/AWS/GCP instead implements
`SecretResolver` and swaps it in via `default_secret_resolver()`, with no change to
`connector_polling.py` — the same swap-with-no-handler-changes property `default_authenticator()`
already proved out for OIDC.

**A resolution failure is treated as an auth failure, not a generic fetch error.** `credential_ref`
that doesn't resolve to anything (`SecretNotFoundError`) is re-raised as `ConnectorAuthError`
inside `poll_connector`, funneling through the exact same handling step 52 already built —
`disabled_auth`, not a silent retry-next-time. Reasoning: a connector that can't even *obtain* its
credential can't possibly authenticate, the same outcome as an adapter rejecting a bad one after
actually trying. Resolution is skipped entirely (never attempted) when `credential_ref` is `None`
— not every connector type necessarily needs a credential (a public git repo, a public website).

**`credential_ref`'s own contract from step 51 is unchanged.** Step 53 is the *resolve* half only
— given a ref, fetch the real value at poll time, in memory, for one run (09 §13). It does not add
a "write a raw secret into the secrets manager" path to the `connectors` API; an admin still
configures `credential_ref` as a pointer they already hold from their own secrets manager, exactly
as step 51 scoped it. Updated stale docstrings in `models.Connector` and `api.CreateConnectorRequest`
that had described step 53 as "the write path" — it isn't; this section is the accurate boundary.

**Live-verified in three parts against real infra** (no real adapter exists until step 54, so no
single run exercises every piece at once): (1) `EnvSecretResolver` resolving a real env var, and
correctly raising on a missing one, run via `docker exec` *inside* the real, rebuilt
`worker-connector-polling` container — not just local pytest. (2) A real unresolvable
`credential_ref` against real dev Postgres, through the real `poll_connector` orchestration
(a throwaway adapter registered in-process asserted it was never even reached): correctly flipped
the connector to `disabled_auth` with the exact missing-variable message in `last_run_detail`, no
retry. (3) A real resolved credential flowing all the way through: a throwaway stub adapter
asserted it received the exact resolved string (not the ref), created a real `raw_source`, and
dispatching `classify_source` afterward through the real broker was picked up by the real
`worker-classification` container and classified via real `gpt-5-nano`. Cleaned up the throwaway
workspace/connector/source afterward.

**Spec touch-point** (applied): none required — `09` §13 already specified this step's model in
full ("mirroring the pluggable `Authenticator` pattern" is the tasklist's own wording); nothing
here needed a wording change.

## 57. Git Connector Adapter (Phase 2 Step 54)

`connectors_git.py` (new) — the first concrete `ConnectorAdapter`, registering itself into
`connector_polling.ADAPTERS["git"]` on import (`tasks.py` imports it for exactly that side effect).
"The simplest state model (commit-SHA diffing)" is the tasklist's own justification for picking
Git first over Confluence/Notion/website connectors, which would need per-page revision tracking
instead of one branch's single HEAD SHA.

**Scope confirmed via AskUserQuestion before building**: shells out to the real `git` CLI
(`asyncio.create_subprocess_exec`, explicit argument lists — no shell string, no injection
surface) rather than a pure-Python git library (`dulwich`) or one hosting provider's REST API.
Works against any remote (GitHub, GitLab, Bitbucket, self-hosted) since it speaks the actual git
protocol, matching "Git repo poller" literally rather than "GitHub poller" — and needed no new
Python dependency, just `git` added to the worker Docker image (`Dockerfile`, `apt-get install
git`).

**State model**: `Connector.last_sync_cursor = {"commit_sha": "<sha>"}`, exactly one string. First
poll (no cursor) treats every file in the tree as new (`git ls-tree -r --name-only HEAD`); a later
poll diffs the stored SHA against the branch's current HEAD (`git diff --name-only
--diff-filter=ACMR old..new`) — only Added/Copied/Modified/Renamed files become items.

**Deletions are not submitted as anything — a real, flagged scope boundary, not an oversight.**
Neither 09 §4 nor 03 §2 names removing/deprecating wiki content when a source disappears; no
tasklist step anywhere builds that (connector-driven or otherwise). `--diff-filter=ACMR`
deliberately excludes `D`.

**A file that fails to decode as UTF-8 is skipped, not submitted.** This connector targets
narrative content, and every downstream pipeline stage (classification, curation) expects text —
submitting a binary blob would just fail later, less legibly.

**Credential: HTTPS token only, embedded into the clone URL** (`https://<token>@host/...`) via
`_with_credential`; SSH remotes pass through untouched (`git@host:...` URLs don't take an embedded
HTTPS token this way) — "the simplest state model" extends to auth too, no known_hosts/key-format
handling. `credential` here is already the *resolved* secret (step 53), never held past the one
`poll()` call.

**A real correctness fix caught by reasoning about the deployment shape, not by a test**: without
`GIT_TERMINAL_PROMPT=0` set on the subprocess environment, a real worker process (no TTY) would
hang indefinitely on an auth failure waiting for a username/password prompt, instead of failing
fast with a message `_clone` can classify — `_CLONE_TIMEOUT_SECONDS` would eventually kill it, but
that's the wrong outcome (a slow, unclassified generic error) rather than the intended one (a fast,
correctly-classified `disabled_auth`). Fixed before it was ever exercised, then live-verified it
actually mattered: a real clone against a real, deliberately nonexistent/private-looking GitHub URL
with no credential failed in 0.43s with `fatal: could not read Username for 'https://github.com':
terminal prompts disabled` — exactly the fast, classified failure the fix was for.

**A stale `commit_sha` (force-push, rebase, or a genuinely wrong cursor) recovers as a full
resync**, not a failed run — `git diff` against an unreachable SHA fails, caught specifically
(`_GitDiffUnavailable`) and retried as `git ls-tree` (treat everything as new) rather than
propagating as a generic error.

**Tested against a real local git repository, not mocked** — `tests/test_connectors_git.py`'s
`origin` fixture is a real `git init`-ed repo in a pytest `tmp_path`, cloned via a real `file://`
URL. Git operations against a local repo are fast and fully hermetic (no network), so there was no
reason to fake any of it: first-poll discovery, added/modified/deleted-file diffing, unchanged-SHA
no-op, binary-file skipping, branch override, and the stale-cursor fallback are all exercised
against the real CLI. The one thing that couldn't be hermetic — a real auth failure, which needs a
real remote enforcing auth — is tested as `_clone`'s message-classification logic in isolation
(`_run` mocked to raise a crafted message) rather than committing a test that depends on network
access to a real host, matching this project's no-network-in-committed-tests convention; the real
network-dependent case is the live check below instead.

**Live-verified against real infra**: a real `worker-connector-polling` container (rebuilt with
`git` installed) with real outbound network access, polling a real public GitHub repository
(`octocat/Hello-World`) through the real broker — first poll discovered its one file and created a
real `raw_source` that a real `worker-classification` container then processed via real
`gpt-5-nano`; a second poll against the unchanged SHA correctly discovered zero items, no duplicate
`raw_source`. Separately, a real auth-failure run (above) confirmed the fast-fail fix for real.
Cleaned up the throwaway workspace/connectors/sources afterward.

**Spec touch-point** (applied): none required — 03 §2 already named Git repositories as a
connector example and 09 §4 already specified the execution model this fills in; nothing here
needed a wording change to either.

## 58. Connector Auth-Failure Notification Hook (Phase 2 Step 55)

`disabled_auth` itself already existed (step 51's enum, step 52's `poll_connector` setting it) —
this step is the second half of its own tasklist line: a real "Notification Service hook."

**The Notification Service's full delivery mechanics are explicitly Phase 3+** (phase2-
tasklist.md's own header: "Explicitly excluded from this phase... the Notification Service's full
delivery mechanics"; `07` §6's roadmap Gantt places it "after p2c," and its own "concretely it
should" description names email/chat-webhook delivery this phase never builds). So this step can
only be a call-out point, not real delivery — the open question was how real that hook should be.

**Scope confirmed via AskUserQuestion before building**: a pluggable `NotificationSink` interface
(`notifications.py`, new) — a `Protocol` + `default_notification_sink()` factory + one concrete
`LogNotificationSink` (a structured `logger.warning`) — over a bare, unabstracted log line with no
new module. This is the third time this exact "protocol + factory + one real default" shape has
been used (`auth.py`'s `Authenticator`, `secrets_manager.py`'s `SecretResolver`), and matches `01`
§1's own architecture diagram naming Notification as its own, separate Core Service rather than
folding it into `connector_polling.py` as an inline logging concern — a deployment with a real
notification backend implements `NotificationSink` and swaps it in, with no change to
`connector_polling.py`, the same swap-with-no-handler-changes property the other two providers
already proved out.

**Scoped narrowly to this one trigger** (`notify_connector_auth_failure`), not a speculative
general `notify(event_type, **kwargs)` API — `07` §6 names other triggers (aging review items, SLA
breaches, submitter outcomes) no tasklist step asks this module to handle yet, and a general event
API guessing at their shapes ahead of the step that actually needs them would be exactly the kind
of premature generality this project avoids.

**Fires exactly once per auth failure**, unconditionally (no de-duplication against a connector
already sitting in `disabled_auth` from a prior run) — `poll_connector`'s own dispatch guard
(`_dispatch_connector_polls` only ever dispatches *enabled* connectors) already keeps a disabled
connector from being auto-polled again, so double-notification isn't a real risk in the normal
schedule-driven path; a manual/test-driven re-poll notifying again is honest, not a bug to guard
against.

**Live-verified against real infra**: a real `worker-connector-polling` container running a real
`poll_connector` task against a real (deliberately private/nonexistent) GitHub URL produced the
exact structured warning line — connector id, workspace id, type, and message — in the container's
own real logs, alongside the real `disabled_auth` state change in real dev Postgres. Cleaned up the
throwaway workspace/connector afterward.

**Spec touch-point** (applied): none required — `09` §13 already named both halves of this step
("surfaces via... the Notification Service") in full; nothing here needed a wording change.

## 59. 2e Closing Verify (Phase 2 Step 56)

Closes out track 2e (steps 51-55: `Connector` model + API, the polling worker pool, credential
resolution, the Git adapter, the auth-failure notification hook). The literal claim: configure a
connector, run one poll cycle, confirm it creates `raw_source`s indistinguishable from a manual
upload flowing through the normal pipeline unchanged.

**Committed**: `tests/test_end_to_end_2e.py` (new) — configures a real connector
(`connectors.create`), runs one poll cycle through `tasks._poll_connector` (the real task wrapper,
not `connector_polling.poll_connector` directly, so the real "dispatch only after commit" step 32
discipline is exercised too, not just the lower-level orchestration) against the real `"git"`
adapter and a real local `git init`-ed repository (hermetic, no network — same convention as
`test_connectors_git.py`). Confirms `submitted_by=connector:<id>`, a normal `submission` review
item visible through the real REST admin surface, then drains the real dispatch chain (mocked
LLM/no broker, matching `test_end_to_end_2b.py`/`2d.py`'s convention) through to `ingested` and
searchable, and resolves the submission item through `POST /review-items/{id}/resolve` — the same
endpoint, same action, no connector-specific branch anywhere in that path.

**Live-verified entirely through the real REST API** (via the load-balanced `gateway`/`nginx`
infra step 49 built), tying the whole track together in one continuous flow rather than
DB-script-verified pieces: `POST /connectors` configured a real connector against a real public
GitHub repo; a real dispatched `poll_connector` run against the real broker/worker discovered one
file. **A genuine, non-bug outcome surfaced here worth noting**: this run's real classification
landed at `pending_review` (a legitimate low-confidence/cross-check outcome, `03` §3, not a bug —
the same class of outcome seen repeatedly across this session's live checks) rather than
auto-classifying — resolved via `POST /review-items/{id}/resolve` with the target `document_type`,
exactly the admin-intervention path a manual submission would also take, which this run happened
to exercise for real rather than only the auto-classify path steps 52-55's own live checks had
already covered. From there: real curation, real indexing, `GET /sources` and `GET /search`
correctly showed the ingested, searchable content, and the submission review item resolved
through `POST /review-items/{id}/resolve` — the complete pipeline, entirely through the REST
surface a real admin/operator would use, no direct DB reads standing in for the "confirm it worked"
step this time. Cleaned up the throwaway workspace/connector/source/pages afterward.

**Spec touch-point** (applied): none required — this step's own text in phase2-tasklist.md already
specified exactly what to verify; nothing here needed a wording change.

## 60. Hardcoding Remediation (post-Phase-2 code-health pass)

Requested directly: a sweep of `src/karpwiki/` for hardcoded values that should be deployment
configuration instead, following [implementation-audit.md](implementation-audit.md)'s
code-vs-spec review. Found 7 real cases — a bare module constant standing in for what should be
a `KARPWIKI_*` env var, same category as every existing entry in `config.py` — plus one DRY/
missing-cap gap in the search path. All fixed directly (confirmed via AskUserQuestion rather than
assumed), not just documented, since every fix is small, additive, and follows a pattern already
used throughout this codebase.

**Seven env vars added to `config.py`**, each read live where used (never frozen into a
module-level constant at plain import time, except where the constant itself already lived at
module scope and is only ever read through that module's own name — the step-48
`_RATE_LIMIT_CATEGORIES` lesson, `09` this file's own step 48 entry): `CELERY_VISIBILITY_TIMEOUT_SECONDS`
(`tasks.py`'s `broker_transport_options`, previously a bare `600`), `OPENSEARCH_INDEX_NAME`
(`dedicated_index.py`'s `INDEX_NAME`, previously `"karpwiki-pages"`), `LLM_RETRY_ATTEMPTS`/
`LLM_RETRY_BASE_DELAY_SECONDS` (`llm.py`'s `retry_transient`, previously its own module
constants — now read from `config` on every call, not cached), `OIDC_JWKS_TIMEOUT_SECONDS`
(`auth.py`'s `OidcAuthenticator.__init__` default `httpx.AsyncClient` timeout, previously a bare
`5.0`), `GIT_CLONE_TIMEOUT_SECONDS` (`connectors_git.py`'s `_clone`, previously
`_CLONE_TIMEOUT_SECONDS = 60`), and `BULK_MOVE_BATCH_SIZE` (`bulk_move.py`'s `BATCH_SIZE`,
previously a bare `100` — kept as a module-level `bulk_move.BATCH_SIZE = config.BULK_MOVE_BATCH_SIZE`
since `api.py` reads it as `bulk_move.BATCH_SIZE`, an existing test monkeypatches that same
attribute name, and this module is imported once at process start like every other domain
module).

**Search limit DRY violation + missing cap, found in the same pass**: `search.search()` and
`dedicated_index.search()` each independently declared an identical, uncapped `limit: int = 20`
default — unlike every list endpoint's own `pagination.py` (`DEFAULT_LIST_LIMIT`/
`MAX_LIST_LIMIT`, enforced via `limit = min(limit, MAX_LIST_LIMIT)` inside `ingestion.py`/
`versioning.py`/`review.py`), a caller could pass an arbitrarily large `limit` straight into the
`LIMIT :limit` SQL clause. Added `DEFAULT_SEARCH_LIMIT = 20`/`MAX_SEARCH_LIMIT = 100` to
`search_result.py` — not `pagination.py`, since search isn't cursor-paginated (`09` §28's own
flagged gap, still open, `phase3-tasklist.md` step 66) and `search_result.py` is the module both
`search.py` and `dedicated_index.py` already import for their shared `SearchResult` type, avoiding
the same circular-import problem that put `SearchResult` there in the first place (`search.py` →
`dedicated_index.py` → `search_result.py`, one-directional). Both `search()` functions now enforce
`limit = min(limit, MAX_SEARCH_LIMIT)` before building their query, matching the existing list
endpoints' pattern exactly. `api.py`'s `search_endpoint`/`run_search` and `mcp_server.py`'s
`wiki_search` — the three callers `09` §14 names as sharing this contract — now default to
`DEFAULT_SEARCH_LIMIT` instead of each separately hardcoding `20`. This is a distinct finding from
`phase3-tasklist.md` step 66 (the four non-search list endpoints' missing cursor pagination) —
that gap is still open; this fix only adds a cap to `/search`'s existing non-paginated `limit`
param, not real pagination.

**Verification**: full test suite (529 tests) passes unchanged. `tests/test_llm.py`/
`test_ingestion.py` updated to monkeypatch `config.LLM_RETRY_BASE_DELAY_SECONDS` instead of the
now-removed `llm.LLM_RETRY_BASE_DELAY_S` module constant. Live-verified every new env var actually
overrides its config value and propagates to the consuming module (`bulk_move.BATCH_SIZE`,
`dedicated_index.INDEX_NAME`, `connectors_git`'s live `config.GIT_CLONE_TIMEOUT_SECONDS` read) via
a real interpreter session with the env vars set; live-verified `search.search()` accepts and
silently clamps an oversized `limit=99999` against the real dev Postgres database rather than
erroring or passing it through unbounded to the `LIMIT` clause.

**Spec touch-point**: none required — `09` §14's own pagination/rate-limit contract language
already covers "deployment-wide operational tuning belongs in config," and no spec document names
any of these seven values as anything other than an implementation-internal timeout/batch-size
constant.

## 61. Real Wiki Markdown Export to the Object Store (Phase 3 Step 57)

`01` §1's own architecture diagram names "wiki markdown export" as one of exactly three things the
Object Store holds, and `02` §2 specifies it precisely — a read-only, regenerated mirror at
`/{workspace_id}/wiki/...` (`overview.md`, `index.md`, `log.md`, `concepts/*.md`, `entities/*.md`,
`sources/*.md`, `comparisons/*.md`, `SCHEMA.md`), written whenever `wiki_page.current_version_id`
changes. Never built through Phase 1/2 — every DB-backed wiki page lived only in the Metadata DB.

**`wiki_export.py`** (new): `export_path(workspace_id, path)` builds `/{workspace_id}/wiki/{path}`
— `wiki_page.path` already matches the export layout exactly (`concepts/{slug}.md`,
`sources/{source_id}.md`, etc. — the same paths `curate.PAGE_DIRECTORY` and `_write_source_page`
already produce), so no separate mapping was needed. `write`/`delete` are thin
`objectstore.write_text`/`delete` wrappers. Called synchronously from `versioning.create_page`/
`write_version`, right after `page.current_version_id` is set — the same "compute-on-write,
non-transactional with the Metadata DB" pattern `_write_diff` already uses for page-version diffs
(§7 above), which `02` §2 explicitly permits ("not required to be transactional... which remains
the system of record"). `rollback` gets this for free since it calls `write_version` internally.

**Two design forks, both confirmed via AskUserQuestion before building:**

1. **`SCHEMA.md` is a placeholder, not real content.** `workspace.schema_ref` is still a bare
   pointer string (§26 above; phase3-tasklist.md step 59 is the real parse/store/version work),
   and it isn't a `wiki_page` at all — no `current_version_id` to hook a write off of. Chose to
   write a placeholder now (`write_schema_placeholder`, called from `workspaces.create`/`update`
   whenever `schema_ref` changes) rather than skip it entirely, so the file exists at the spec'd
   path even before step 59 gives it real content; the placeholder names the pointer it's standing
   in for and says explicitly what step will replace it.
2. **A rebuild-from-DB-truth backfill, not forward-only.** `02` §3 calls the export "a regenerated
   projection" — the same guarantee `search.reindex_pending` already gives the Full-Text Index —
   and the write-through hook above only fires on a page's *next* write, so every page created
   during Phase 1/2 would otherwise have zero exported file until next edited. `export_workspace`
   (new) re-derives every current page's mirror plus the `SCHEMA.md` placeholder from DB truth in
   one pass; safe to call again any time, same "rebuild, don't trust the projection" property
   `reindex_pending` already has. Not wired to any API endpoint or scheduled task — same as
   `reindex_pending`/`retry_errored`, which also have no caller anywhere in this codebase; an
   operational tool, not a request-path feature.

**`bulk_move.py` also cleans up the stale mirror.** A page move sets `page.workspace_id` before
calling `write_version` (already the existing pattern, for `_write_diff`'s own path), so the new
mirror lands correctly under the target workspace automatically — but the *old* workspace's copy
would otherwise be left behind, referencing a page that no longer lives there. Added an explicit
`wiki_export.delete(workspace_id=source_workspace_id, path=page.path)` right after the version
write, mirroring the identical reasoning `execute_batch` already applies to the dedicated
OpenSearch document cleanup two lines below it.

**Deliberately out of scope**: `index.md` gets no special handling — nothing creates a real
`index`-type page yet (step 60); once one does, it flows through the same write-through hook as
any other page, no changes needed here. `comparison` pages are the same story — `curate.
PAGE_DIRECTORY` has no entry for them because nothing creates one yet either.

**Verification**: `tests/test_wiki_export.py` (new, 9 tests) covers `export_path`, write-on-create,
overwrite-on-edit, `delete`, both `SCHEMA.md` placeholder branches (set/unset `schema_ref`, at both
`workspaces.create` and `.update`), the `export_workspace` backfill, and the `bulk_move` stale-mirror
cleanup — full suite (538 tests) green. Live-verified against real dev Postgres and the real
MinIO-backed S3 object store: created a real workspace/page, confirmed the mirror landed at the
real S3 path, edited it and confirmed the overwrite, deleted the mirror and confirmed
`export_workspace` restored it, then bulk-moved the page and confirmed the old workspace's copy was
gone and the new workspace's copy existed with the right content — independently re-checked via a
raw `fsspec.find()` against the real bucket, bypassing this codebase's own `objectstore.py` wrapper
entirely, rather than trusting only the Python-level assertions. Cleaned up the throwaway
`live57-src-*`/`live57-tgt-*` workspaces from dev Postgres afterward (left their now-orphaned
object-store files in place, matching this project's existing precedent of not cleaning up
object-store debris from prior live checks).

**Spec touch-point** (applied): none required — `02` §2's own text already permits everything built
here (non-transactional write, "regenerated projection" framing); the two AskUserQuestion forks
above are implementation decisions filling in what the spec deliberately left open, not deviations
from it.

## 62. FUSE-Mount Access (Phase 3 Step 58)

§12 above already decided the shape — read-only, opt-in per workspace via `access_policy`, scoped
to exactly the wiki export (step 57) — but it was never built. This step builds it, with one real
scope question resolved via AskUserQuestion before any code was written: an actual mount is an OS
syscall requiring a kernel-level FUSE driver installed on the host (macFUSE on macOS, `fuse3` on
Linux) — system software installation, a materially different kind of action than anything else in
this codebase. Confirmed scope: build every real piece of app logic (the AuthZ grant, the
scoped/read-only filesystem view, a real CLI entry point using `fsspec.fuse.run`), but do not
install a FUSE driver or perform a live mount in this session.

**`AccessPolicy.fuse_access`** (new column, migration `7ab85057a869`, `server_default=false` then
dropped — same round-tripped, backfill-safe pattern `e139b033f7f3` already established for
`workspace.dedicated_index`) — orthogonal to `role`, never widens what a role already permits and
is never implied by one, matching §12's "not automatic for every existing reader/contributor."
`workspaces.grant` gained an optional `fuse_access: bool | None = None` param — omitted leaves an
existing grant's value unchanged (mirrors `workspaces.update`'s own "only supplied fields change"
convention), matching `False` on a brand-new grant. `POST /workspaces/{id}/access-policy` passes it
through; `GET` now returns it in every grant's body.

**`wiki_mount.py`** (new): `check_fuse_access(session, principal_keys, workspace_id)` — a plain
`AccessPolicy` query gating on `fuse_access.is_(True)`, raising `FuseAccessDenied` (a
`PermissionError` subclass) otherwise — same `principal.policy_keys` group-aware pattern
`auth.effective_role`/`has_role` already use. `scoped_filesystem(workspace_id)` builds a view
rooted at exactly `/{workspace_id}/wiki/` via fsspec's own `DirFileSystem` (confirmed its real
constructor signature directly against the installed `fsspec==2026.7.0`, not assumed from training
data) — never `sources/`/`diffs/`/`assets/`, matching `02` §2's own scope boundary, since those are
simply outside the wrapped root's path space.

**`fsspec.fuse.run` has no read-only option of its own** — read its actual source directly (not
assumed): its `FUSEr` operations class calls `write`/`create`/`mkdir`/`rmdir`/`unlink`/`chmod`
straight through to whatever filesystem object it's handed, with no read-only flag anywhere in
`run()`'s signature. `_ReadOnlyFileSystem` (new, in `wiki_mount.py`) is what actually makes
"never write access" (`09` §12's own words) true: it wraps any fsspec filesystem, blocks every
mutating method by name, and blocks `open()` in any non-read mode — verified this happens *before*
the real backend is ever touched (see Verification below), not just that the call eventually fails.

**Deferred import, confirmed necessary, not just cautious**: `import fsspec.fuse` (needed inside
`run_mount` to actually call `fsspec.fuse.run`) transitively imports `fuse` (the `fusepy` PyPI
package), whose own top-level `ctypes.util.find_library("fuse")` call raises `OSError: Unable to
find libfuse` at import time — confirmed directly in this dev environment (macOS, no macFUSE
installed) — if libfuse isn't present. Deferring the import to inside `run_mount` only (not at
module top level) keeps `check_fuse_access`/`scoped_filesystem`/`_ReadOnlyFileSystem` — the actual
app logic this step needed to build — importable and testable regardless. `fusepy` itself installs
cleanly via plain `pip` (pure Python, no build step) — only *importing* it needs the system
library — so it's declared as a new `fuse` extra in `pyproject.toml`
(`pip install karpwiki[fuse]`), not the base dependency list every gateway/worker process would
otherwise be forced to carry for a capability only the standalone mount CLI uses.

**Identity resolution reuses `mcp_server._resolve_stdio_principal` directly**, rather than
duplicating it: a FUSE-mount process is the same shape as the MCP stdio transport — one local
process, one caller, no per-request headers — so `run_mount` imports the (module-private but
reusable, no MCP-specific coupling) function rather than inventing a second `KARPWIKI_*_TOKEN`/
`_USER`/`_GROUPS` convention for the same problem.

**Verification**: `tests/test_wiki_mount.py` (new, 7 tests) — `check_fuse_access` denies with no
grant, denies a role-only grant (`fuse_access=False`), allows a real grant, allows via a group
grant; `scoped_filesystem` serves real content rooted at the right prefix and blocks every mutating
call tried (`open` write modes, `rm`, `mkdir`, `touch`). `tests/test_workspaces.py`/
`test_workspaces_api.py` gained matching coverage for `workspaces.grant`'s new param and the API's
pass-through (plus fixed one pre-existing exact-dict response assertion that the new `fuse_access`
field would otherwise have broken). Full suite (550 tests) green. No test anywhere imports
`fsspec.fuse` or performs a real mount, per the confirmed scope. Live-verified against real dev
Postgres and the real MinIO-backed S3 object store, plus the real REST API through the rebuilt
`gateway`/`nginx` containers: `check_fuse_access` correctly denied/allowed against real grants,
`scoped_filesystem` read real S3-backed content, and a real write/`rm` attempt against the real
backend was blocked *before* touching it — re-read the file afterward to confirm it was genuinely
unchanged, not just that the call raised. `POST`/`GET /workspaces/{id}/access-policy` through nginx
correctly granted and listed `fuse_access: true`. Cleaned up the throwaway `live58-*`/`live58api-*`
workspaces from dev Postgres afterward.

**Spec touch-point** (applied): none required — `09` §12 already specifies this step's full scope;
nothing built here diverges from it.

## 63. Real SCHEMA.md Storage and Parsing (Phase 3 Step 59)

§26 above flagged this as "a self-contained feature on the scale of a track of its own" — this
step is that track. Three real design forks confirmed via AskUserQuestion before any code was
written, since the step touches enough surface area (models, migrations, API contract,
five modules' consumer wiring) that guessing wrong on any one would mean rework across all of it.

**Fork 1 — `schema_ref`'s meaning.** Repurposed as the current `SchemaVersion`'s id (a real FK,
`Workspace.current_schema_version_id`), no longer a caller-settable free-text pointer — a
deliberate, small breaking API change: `schema_ref` is gone from `CreateWorkspaceRequest`/
`UpdateWorkspaceRequest`, but the JSON response key stays `schema_ref` (now derived) for API
stability. `workspace.schema_ref`'s old column is dropped outright, not migrated — every existing
value was already just a placeholder string, never real content (§26).

**Fork 2 — rollback.** `01` §7 says SCHEMA.md changes should be "auditable and reversible";
versioning covers "auditable," and a `schema.rollback` was added (mirrors `versioning.rollback`
exactly) to cover "reversible" for real, alongside `POST /workspaces/{id}/schema/rollback`.

**Fork 3 — the classification confidence-gate ordering**, the one §27 explicitly flagged as
"needs revisiting once SCHEMA.md thresholds are real." Traced before asking: `result.document_type`
(the model's raw output label) is already known before `classify.route`'s confidence check runs,
so its owning workspace can be resolved right there via `document_types.workspace_for_type` — no
bigger pipeline restructuring needed, just moving one lookup earlier and reusing it (rather than
re-querying) on the accept path, since `routing.document_type is result.document_type` whenever
`routing.accepted`. Confirmed and built this way; the classifier's own `resolve_model` call stays
platform-default-only, since classification is what determines the workspace in the first
place — there is no schema to read yet at that point, a real (not cut-corner) constraint.

**`schema.py`** (new): `WorkspaceSchema` (Pydantic) mirrors 09 §6's own template exactly, but
**every field is optional with no default value duplicated from any other module** — mirroring
`ingestion.DEFAULT_MIN_CONFIDENCE`/`dedup.DEFAULT_NEAR_DUPLICATE_SCORE`/etc. into this module
would either force a circular import (`schema.py` would need `ingestion.py`, which needs
`schema.py` for the confidence-gate override) or create silent drift between two copies of the
same number. Every consumer instead treats a `None` field exactly like an already-existing
directly-injected `None` override: fall back to its own constant, same as before this step.
`document_types` is parsed but explicitly **not authoritative** — reconciling it against the real
`document_type` table would be materially bigger scope (validation, sync-on-write, conflict
resolution) than this step; `retention.page_version_max_count` is parsed and stored but nothing in
this codebase enforces a version-count cap anywhere (checked directly, not assumed).

**`SchemaVersion`** (new table, migration `7eb53cee0b95`) — versioned like `page_version` but not
one: no `page_type`, no `wiki_page` row, `content` is plain YAML text (not markdown+frontmatter).
Same circular-FK shape `wiki_page.current_version_id` already established (`use_alter=True`);
round-tripped clean against real dev Postgres (upgrade → downgrade → upgrade), same discipline as
every migration since the step-51 bug. Dropping the old `schema_ref` column outright (not
migrating its values) is deliberate, documented in the migration itself.

**Rewiring, module by module**: `ingestion.classify_source` (confidence gate, above);
`ingestion.check_duplicates`'s `near_duplicate_score`, sourced at its one real call site
(`tasks.py`'s `_curate`) alongside the new `ingestion.resolve_ingestion_policy`; four
`llm.resolve_model` call sites (`ingestion.py`'s `curate_source` and merge-resolution,
`advisor.py`'s existing-duplicate merge and contradiction check) now pass
`schema.as_dict(await schema.load(...))` instead of always `None`; all five `tasks.py` detector
task wrappers (`_detect_superseded_sources`/`_existing_duplicates`/`_orphans`/`_contradictions`/
`_staleness_tiered`) read live schema overrides — except plain `_detect_staleness`, which stays
unwired since `09` §6's template has no flat `threshold_days` field to read (only the tiered
variant's `high_traffic_days`/`low_traffic_days` exist, and that variant is the one beat actually
schedules).

**`ingestion.resolve_ingestion_policy`** closes the second, smaller gap the tasklist step names:
`09` §13's "a connector's policy may only tighten, never relax" rule, unenforceable until a
workspace's own policy was real content. The tasklist's own text says wire this into
`connector_polling.poll_connector` — traced through and that's not where the gating decision
actually happens: `poll_connector` only ever creates a `raw_source` unconditionally (03 §2's
"indistinguishable from any other submission"), no gate to enforce. The real `auto`/`gated`
decision lives in `check_duplicates`'s "no concerns found" branch, at curate time — that's where
this function is actually called from, with a correction note left in the tasklist's own step 59
text rather than silently building it somewhere else without saying why.

**A real bug caught live, not by the test suite — the most notable finding of this step.**
`llm.resolve_model`'s `((schema or {}).get("llm") or {}).get(role, {}).get("model")` relies on
`.get(role, {})`'s default applying whenever `role` is missing — but `schema.as_dict()` (a real
`WorkspaceSchema.model_dump()`) sets *every* optional field explicitly, including
`llm.<role>: None` for an unconfigured role. The key is present with value `None`, not absent, so
the default never applies and `None.get("model")` raises `AttributeError`. This shipped in the
first version of this step's code, passed the full test suite (592 tests) unchanged, and only
surfaced on the very first real curator call against a real per-workspace schema during live
verification — every existing test in `test_llm.py` had hand-built its own schema dicts with keys
either fully absent or fully present, never explicitly `None`, so nothing had ever exercised this
shape before a real `schema.as_dict()` output did. Fixed with `.get(role) or {}` (treats "absent"
and "explicitly `None`" the same), plus a regression test using the same explicit-`None` shape.
Rebuilt and redeployed the `gateway`/worker containers with the fix, then re-ran the exact live
check that had failed — it completed cleanly the second time. **Worth remembering generally**:
a hand-built test fixture that happens to omit a key is not equivalent to a real serializer that
emits the key with an explicit `None` — don't assume dict `.get(key, default)` behaves the same
against both shapes without checking which one production code actually produces.

**Verification**: `tests/test_schema.py` (18, new), `tests/test_schema_api.py` (8, new),
`tests/test_ingestion_policy.py` (6, new), `tests/test_task_schema_wiring.py` (7, new), plus
targeted additions to `test_ingestion.py` (workspace-aware confidence gate, 3 tests),
`test_curate_orchestration.py` (real `llm.resolve_model` wiring, 1 test), `test_workspaces.py`/
`test_workspaces_api.py` (the `schema_ref` API-shape change). Full suite: 593 tests green. The
`karpwiki_test` database needed a manual `DROP SCHEMA public CASCADE`/recreate mid-step — its
`Base.metadata.drop_all`/`create_all` fixture (not Alembic-driven) tried to drop the new
`fk_workspace_current_schema_version` constraint by name on a DB whose actual tables predated this
step's model changes; a one-time, expected consequence of a circular-FK rename against a
pre-existing local test DB, not a bug. Live-verified against real dev Postgres, real MinIO, and
real `gpt-5-nano` through the rebuilt `gateway`/`nginx`/worker containers (details above, including
the bug/fix/re-verify cycle). Cleaned up the throwaway `live59-*` workspace and all its data
(including `page_index` rows this time — the first live check in this session to actually reach
real search indexing before cleanup) afterward.

**Spec touch-point** (applied): none required — `01` §7 and `09` §6/§26/§27 already specify
everything built here; the three AskUserQuestion forks above fill in what those sections
deliberately left open (exact API shape, rollback inclusion, gate ordering), not deviations.

## 64. Real `index.md` Catalog Page and Catalog-Match Boost (Phase 3 Step 60)

`search.py`'s own comment had flagged this as an accepted gap since Phase 1: `04` §3's
catalog-match boost was approximated as a `tsvector` weight tier on `description` (baked into
the same score `index_page` already computes), standing in for a real catalog page that never
existed. This step builds both real halves: the page, and a real, separate boost step.

**`curate.render_index_body`** (new, pure) — four sections (Concepts/Entities/Sources/
Comparisons), each `[title](path) — description`, mirroring `render_overview_body`/
`render_log_body`'s exact shape. `overview`/`index`/`log` are excluded — structural pages, not
catalog members, the same distinction `curate.PAGE_DIRECTORY` and
`advisor.ORPHAN_CANDIDATE_PAGE_TYPES` already draw.

**`ingestion.refresh_index`** (new, public — called from `api.py`) queries each category's
published pages via raw `->>` frontmatter extraction (`versioning.list_pages`/`search.search()`'s
own established convention, not the ORM-level JSONB comparator no other query in this codebase
uses), alphabetical by title, and upserts `index.md` via the existing `_upsert_singleton`. Wired
at the same three refresh points `refresh_log` already has — `curate_source`, the rollback
endpoint, and bulk-move (both workspaces) — since any of those can change a page's title,
description, or workspace membership, all of which the catalog must reflect. No dedicated
backfill for pre-existing workspaces, matching `overview.md`/`log.md`'s own precedent from
Phase 1 (neither ever got one either) — a workspace only gets a real `index.md` once it next runs
one of those three operations.

**The real catalog-match boost** lives in `search.search()`'s own SQL, as a genuine second stage
after the base `ts_rank_cd` score — matching `04` §3's mermaid diagram, which draws "matches an
index.md catalog entry?" as a distinct step after retrieval, not folded into it (which is exactly
what the old weight-tier approximation did). A candidate gets `CATALOG_MATCH_BOOST` (a
multiplicative 1.3x, not additive — `ts_rank_cd`'s absolute scale varies with document
length/term frequency, so a flat addend would be wrongly-scaled for some candidates; no specific
magnitude is given anywhere in spec/, this is this implementation's default) only when BOTH hold,
checked via a real `EXISTS` subquery: (1) a real `page_link` row from that workspace's `index.md`
to the candidate — `page_links.sync` already creates this automatically the moment `index.md`'s
real markdown links get written, so this is a genuine structural fact, not assumed or
recomputed; and (2) the query's tsquery matches specifically *that candidate's own*
title+description text (recomputed inline via `to_tsvector`, not a coarse "does the query match
`index.md` *anywhere*" check) — real per-page precision. A coarse whole-document check was
considered and rejected: since one workspace's `index.md` holds every page's catalog entry in a
single document, a query matching any ONE entry would otherwise indiscriminately boost every
catalogued page, not just the one whose entry actually matched.

**Deliberately scoped to the shared Postgres index only.** `dedicated_index.py`'s OpenSearch path
keeps its existing (pre-step-60, weight-tier-only) ranking — replicating the `page_link` join
inside an OpenSearch query would mean denormalizing that relational data into the OpenSearch
document itself, a materially bigger addition than this step takes on. Matches `04` §4's own
existing precedent that a dedicated workspace's federated score is already an accepted
approximation (min-max normalization at merge time, not true fusion) — one more acknowledged gap
in the same category, not a new kind of shortcut.

**Verification**: `tests/test_curate.py` (+2: `render_index_body`'s sections and empty-category
handling), `tests/test_curate_orchestration.py` (+4: catalog contents after a real curate,
structural-page exclusion, regeneration-not-appending, and a rollback's restored title/description
reflected in the catalog), `tests/test_search.py` (+2, the most load-bearing: one proves the
boost fires — two pages with byte-identical description text, only one linked from a real
`index.md`, and the catalogued one's score is *exactly* `CATALOG_MATCH_BOOST` times the
other's, not just "higher"; the other proves the boost does NOT fire on a merely-catalogued but
non-matching page, by comparing its score against an identical control page in a second
workspace with no `index.md` at all — equal scores confirms nothing leaked in). Also updated one
pre-existing test's docstring (`test_description_weight_ranks_a_description_hit_over_a_body_only_hit`,
renamed from `test_catalog_match_boosts_a_description_hit_over_a_body_only_hit`) to stop
describing the now-superseded approximation as if it were still the catalog-boost mechanism —
the test itself still passes unchanged (the weight tier is untouched, just no longer
mislabeled). Full suite: 601 tests green. Live-verified against real dev Postgres, real MinIO,
and real `gpt-5-nano` through the rebuilt `gateway`/worker containers: a real ingest produced a
real `index.md` with real, alphabetized catalog entries across all four sections; confirmed
6 real `page_link` rows from it directly against dev Postgres; a real `GET /search` query ranked
the catalogued, matching page above its peers through the real ranking SQL. Cleaned up the
throwaway `live60-*` workspace afterward.

**Spec touch-point** (applied): `04` §3's mermaid diagram and prose already specify the two-stage
shape this step builds to; no wording change needed.

## 65. Structured-Data Curator Treatment (Phase 3 Step 61)

`curate.py`'s own module docstring had deferred this since Phase 1: every source got the
narrative summary+citations treatment regardless of `content_shape`. `07` §1's two treatments
(§1.1's table, §1.3's four-step extraction guidance) were fully specified from the start — this
step is purely the missing half of an already-designed mechanism, not a new design.

**`curate.StructuredCuratedContent`/`StructuredField`/`render_structured_source_body`** (new) —
metadata-first: a structure table (name/type/description per field), a one-sentence intent
statement, and a provenance section (source file, `artifact_identity`, `source_version` — the
same fields `03` §3's deterministic pre-step already extracts and `03` §4's duplicate-version
detection already consumes, now finally read by something on the *ingest* side too). Reuses
`CuratedPage` for the defined-entity pages (`07` §1.3 step 4) rather than inventing a parallel
type — the exact same create-or-update-by-title matching `_write_curated_page` already does for
narrative content applies unchanged, since both content shapes ultimately produce the same
`list[CuratedPage]` shape.

**One deliberate simplification, not a corner cut**: `07` §1.3 step 2 describes the intent
statement as "1-3 sentences," but `StructuredCuratedContent.intent_statement` is prompted and
validated as a single sentence. Reasoning: it becomes the page's frontmatter `description`
directly (`01` §6's own "one-sentence summary" contract, the same field every other page type
already respects), and — via step 60's `index.md` machinery, which draws every catalog entry
from `description` — it's also what `07` §1.1 itself calls index.md's "**one-line** intent
statement." Making it genuinely one sentence keeps both contracts satisfied with one field
rather than inventing a second, longer one solely for the page body; nothing in `07` §1.3's own
guidance ("what question it answers, what system/process it supports, who owns/produces/consumes
it") actually requires more than one well-written sentence to satisfy.

**`ingestion.curate_source` branches on `source.content_shape`** (new `structured_call` parameter,
`call_structured_curator_model`, mirroring `call`/`call_curator_model`'s existing shape exactly):
`structured_data` sources call the new structured prompt and `_write_structured_source_page`;
`narrative` sources are completely unchanged, still calling `call`/`_write_source_page` exactly
as before this step. Both branches converge back into the same shared `pages` create-or-update
loop, `overview.md`/`log.md`/`index.md` refresh, and reindex dispatch immediately after — the
branch is scoped to exactly "which LLM call and which page-body renderer," nothing else in the
pipeline needed to know the difference. `content_shape` stays a `raw_source` attribute, never a
frontmatter field or a new `page_type` (`07` §1.1's own explicit framing), so no model/migration
change was needed anywhere in this step.

**Verification**: `tests/test_curate.py` (+4: structure-table rendering, no-fields-extracted
handling, and both new models' validation), `tests/test_curate_orchestration.py` (+4: the
structured page's body/description/tags, defined-entity-page creation, the intent statement
reaching `index.md`'s catalog entry via step 60's existing machinery, and an explicit regression
check that the narrative path's `description`/tags are unchanged). Full suite: 609 tests green.
Live-verified against real dev Postgres, real MinIO, and real `gpt-5-nano` through the rebuilt
containers: submitted a real 7-field JSON config; the deterministic pre-step correctly tagged
`content_shape=structured_data` and extracted real `artifact_identity`/`source_version` from the
JSON's own `name`/`version` fields; the real curator call produced a genuinely accurate 7-row
structure table (correct types and descriptions for every field, not just names), a coherent
intent statement, and a real defined-entity page for the config artifact itself — all of it
flowing correctly into `index.md`'s real catalog entry via step 60's existing, unmodified
machinery. Cleaned up the throwaway `live61-*` workspace afterward.

**Spec touch-point** (applied): none required — `07` §1 already specifies everything built here
in full; the one-sentence intent-statement simplification is documented above as a reasoned
implementation choice, not a spec change.

## 66. Binary Document Format Ingestion — PDF, DOCX (Phase 3 Step 78)

Found live, not by either completeness audit pass: Deepak asked directly whether DOCX/PDF/CSV/TXT
ingestion had been considered. Checked rather than assumed — CSV and TXT already worked (plain
text, decode cleanly), but PDF and DOCX did not: every text-producing call in the ingest path
(`classify.detect_content_shape`, `ingestion.classify_source`/`curate_source`/the merge-resolution
path) used a bare `payload.decode("utf-8", errors="replace")`. For a binary PDF/DOCX this never
raises — it silently substitutes most of the file with U+FFFD placeholder characters and feeds the
garbled result straight to the Classifier/Curator LLM calls, producing plausible-looking but
meaningless output with no visible error anywhere. No spec section names PDF/DOCX support as in
scope, deferred, or excluded — a genuine unaddressed gap, not a documented boundary the way (say)
legacy `.doc` now is.

**`doc_extract.py`** (new) — `detect_binary_format` sniffs real magic bytes (`%PDF-` for PDF; the
ZIP local-file-header bytes plus a `.docx` extension for DOCX, since bare ZIP magic is shared by
every Office Open XML format and plain `.zip`), and `extract_text` is the new canonical
raw-bytes-to-text step for the whole ingest path: real extraction (`pypdf`/`python-docx`) for a
recognized format, a plain UTF-8 decode otherwise, `None` when neither works. A corrupt/truncated
file whose magic bytes matched but whose container doesn't actually parse gets the same `None`
outcome as never recognizing the format at all — "couldn't extract," not a crash.

**Explicitly scoped to modern DOCX only, not legacy `.doc`.** The pre-2007 `.doc` format is OLE2/
CFB binary, not ZIP/XML — a materially different parsing problem with no maintained pure-Python
reader, and not "the most common" format the direct question named. A `.doc` file is simply
neither UTF-8-decodable nor PDF/DOCX-shaped, so it gets the same `None`/rejected outcome as any
other genuinely unsupported binary — a real, stated boundary, not a silent one.

**Every internal `payload.decode("utf-8", errors="replace")` call site now routes through
`doc_extract.extract_text`** — `classify.detect_content_shape` (feeding the same
`_parses_as_data` structural check real extracted text instead of a lossy decode), and all three
of `ingestion.py`'s own decode sites (`classify_source`, the `merge` duplicate-resolution path,
`curate_source`). Each keeps an `or ""` defensive fallback rather than trusting `extract_text`
can never return `None` at this point — cheap insurance, not the real gate.

**The real gate is `ingestion.store`** — "the one entry point every submission source goes
through" (03 §2, its own existing docstring) — rejecting with a new `UnsupportedContentError`
before a `raw_source`, an object-store write, or any DB row exists at all. This one change point
covers every current and future submission path uniformly: `api.py`'s `POST /sources` catches it
and returns a real `400`; `connector_polling.poll_connector`'s per-item loop now catches it
per-item and skips just that one discovered item (recording `items_skipped` in
`last_run_detail`, a new key added to that dict — two existing exact-dict-equality tests in
`test_connector_polling.py` updated accordingly) rather than losing the whole poll run to one bad
file — defense in depth, since `connectors_git.py`'s own adapter already filters undecodable
files before they become a `DiscoveredItem`, but a future adapter type might not. MCP's
`wiki_submit` needed no change: it only ever accepts pasted text (already documented as
`stdio`'s own scope boundary), which is always valid UTF-8 by construction — it can't hit this
gate at all.

**Tracked in `phase3-tasklist.md` as step 78** (new track 3f, inserted before the Phase 3 closing
verify — renumbered from 78 to 79, the only renumbering needed since nothing yet referenced "step
78" by number) rather than left as an undocumented drive-by fix, matching this project's standing
discipline for any real, non-trivial code change — even one found outside the two formal audit
passes gets the same tracked-step treatment step 66 (the pagination gap) already established as
precedent for a late-discovered finding.

**Verification**: `tests/test_doc_extract.py` (new, 12 tests — real hand-built PDF bytes and a
real in-memory `python-docx`-generated DOCX, not mocks; corrupt-container and legacy-`.doc`
cases), `tests/test_store.py` (new, 3), `tests/test_api.py` (+3: a real DOCX upload accepted, a
plain-text upload, a genuinely unsupported binary rejected with `400`), `tests/
test_connector_polling.py` (+1, plus 2 existing tests' exact-dict assertions updated for the new
`items_skipped` key). Full suite: 628 tests green. Live-verified against real dev Postgres and
real `gpt-5-nano` through the rebuilt containers: a real DOCX runbook and a real PDF each produced
a genuinely coherent, accurate classifier summary quoting real content back (proof of clean
extraction, not garbled placeholder text) — the DOCX ran the complete pipeline through to
`ingested` and searchable; a real PNG-header binary was correctly rejected with a real `400` at
the real REST API, confirmed via `pip show` inside the rebuilt container that `pypdf`/
`python-docx` were genuinely installed, not just declared in `pyproject.toml`. Cleaned up the
throwaway `live62-*` workspace afterward.

**Spec touch-point** (applied): none required — no spec section named PDF/DOCX support as in
scope or excluded; this closes a real, previously-unaddressed gap rather than deviating from
anything spec/ already said.

## 67. Search Partial-Failure / Degraded-Result Contract (Phase 3 Step 62)

§14 above specified the contract in full when API conventions were first settled — "a
partially-served search returns 200 with the results it has plus `\"partial\": true` and
`\"unavailable\": [<workspace_id>, ...]`... mandatory, not advisory" — but steps 25/26 (building
federated search and the dedicated-index backend) never actually wired the exception handling
that contract requires. Found on a fresh re-read of `09` §14 against the current code during this
step's own prep, not flagged as an accepted gap by either completeness audit pass — a real,
previously-unnoticed contract gap in the same shape step 66's pagination finding already was.

**`api.run_search`** (the shared Common Gateway logic both `GET /search` and MCP `wiki_search`
call) wraps `search.search` (shared Postgres) and `dedicated_index.search` (OpenSearch) in
separate `try`/`except Exception` blocks rather than one shared one — a failure in either backend
degrades only that pool to an empty result and records its own resolved workspace ids into a
shared `unavailable` list, while the other backend's real results still get served. Two different
recovery needs, handled differently: a shared-Postgres failure rolls the *session* back
(`await session.rollback()`) before the `query_log` write that follows — a failed raw-SQL
statement on `session.execute` can leave the transaction unusable for anything else on that same
session, the identical failure mode `bulk_move`'s own halt-without-rollback batching in this same
file already established the fix for (`# noqa: BLE001 — 09 §11's halt-without-rollback`, cited
directly as precedent in the new code's own comment). A dedicated-index (OpenSearch) failure
needs no such rollback — it's a wholly separate client, never touches the SQLAlchemy session at
all.

**`partial`/`unavailable` are omitted, not `false`/`[]`, when nothing degraded** — read `09` §14's
"single-workspace operations... never carry the field" as the general pattern for the non-partial
case, not literally scoped to single-workspace-only. `unavailable` is built from a Python `set`
(the dedicated-workspace-id lookup) unioned with a `list` (shared-workspace ids) — sorted before
returning for deterministic response ordering, since set iteration order isn't guaranteed.

**A stale module docstring claim fixed while touching this exact code**: `api.py`'s own top-of-
file docstring still said "Not implemented here, deliberately: dedicated-index score
normalization (04 §4) is step 26 — this endpoint only ever queries the one shared index" —
factually false since step 26 landed (this exact function has called both `search.search` and
`dedicated_index.search` since then). Removed rather than left standing while directly editing
the function it was describing incorrectly.

**Verification**: `tests/test_federated_search_api.py` (+4): a down dedicated backend degrades to
the shared results plus `partial`/`unavailable`; a down shared backend degrades to the dedicated
results *and* proves the session genuinely recovered (the `query_log` write immediately after the
failure still succeeds — not just that the HTTP response looked right); a fully successful search
carries neither field at all; both backends down at once returns a fully partial, empty result
naming both workspaces. Full suite: 632 tests green. **Live-verified with a real backend outage,
not a mock**: seeded a real shared-index workspace and a real dedicated-index workspace, each with
a real indexed page sharing a search term, confirmed a real `GET /search` through the real gateway
returned both — then actually stopped the real `opensearch` container (`docker compose stop
opensearch`) mid-session and re-ran the identical query: a real `200`, the shared result still
present, `"partial": true`, and the exact down workspace id in `"unavailable"`. Restarted
`opensearch`, waited for it to report healthy again, and re-ran the same query once more — full
recovery, both results present, neither field carried, no gateway restart needed anywhere in the
cycle. Cleaned up the throwaway `live62s-*`/`live62d-*` workspaces afterward.

**Spec touch-point** (applied): none required — `09` §14 already specifies this contract in full;
this step closes the gap between that text and the code, not a deviation from it.

## 68. Read-Time Link Resolution + Cross-Workspace AuthZ Re-Check (Phase 3 Step 63)

§31 above named this exactly: "Read-time link resolution is out of scope — no caller exists yet
... resolving them at read time is the future endpoint's job." `GET /pages/{id}` (Phase 2 step 43)
is that endpoint, and it never got the follow-up. `01` §1's own Workspace Resolution table names
the contract directly: "Gateway re-checks the caller's AuthZ against the *target* workspace before
resolving a link from one workspace's page into another."

**`api._resolve_page_links`** (new) reads the already-parsed `page_link` rows for a page — no
markdown re-parsing needed at read time, since `page_links.sync` (step 28) already keeps them
current on every version write — and applies the same access check `_reader_page` itself uses for
a direct fetch of each target: `contributor` if the target is still `draft` (not just a
workspace-level check), inlined rather than calling `_reader_page` again since the target
`WikiPage` row is already in hand from the join. This closes a slightly stricter reading of `01`
§3 than a workspace-only check would: a link pointing at a draft page needs the identical elevated
scope a direct `GET /pages/{draft_id}` would require, treating "can this principal reach this
target *page*" as the real question, not just "does this principal have any role in that
*workspace*."

**A link that fails the check is omitted, not included-but-flagged.** `01` §3 says AuthZ is
re-checked "before resolving" — read literally, an unauthorized target's existence (its id, path,
even that a link once pointed there) is never confirmed to the caller. This is a real design
choice, not the only reasonable one (a flagged-inaccessible entry would also satisfy "re-checks
AuthZ before resolving" in a looser reading), but omission is the more conservative, spec-literal
choice and costs nothing extra to implement.

**Archived-workspace targets are never excluded on that basis alone** (`01` §3: "Cross-workspace
links... into an archived workspace continue to resolve normally") — `has_role` doesn't
distinguish workspace status at all, so this falls out without any special-casing.

**Wired into both `GET /pages/{id}` and the MCP `wiki_get_page` tool** — a new `"links"` field on
each response, left alongside the unchanged raw `content` field (the stored document verbatim,
embedded markdown link syntax un-rewritten) rather than replacing it. `wiki_get_page` duplicates
the two-line call (matching this module's own established "eight thin tools duplicate REST logic
directly" pattern, `mcp_server.py`'s own docstring) rather than routing through a shared
`run_get_page` the way `wiki_search`/`wiki_resolve_review_item` do — `_page_body`'s only other
caller was already `get_page_endpoint` alone, so introducing a shared wrapper here would be
premature abstraction for two call sites that already look the same.

**A stale docstring flag closed, not left stale**: `09` §31's own "no caller exists yet" note
above is now accurate history rather than an open flag — this step is the caller.

**Verification**: `tests/test_pages_sources_api.py` (+7): same-workspace resolution, cross-
workspace resolution when the caller has real access, omission when the caller lacks it, omission
of a link to a still-draft target for a mere reader (present for a contributor), a dangling link
target produces no entry (already guaranteed by `page_links.sync` never writing a row for one),
and a page with no links returns an empty list, not a missing key. `tests/test_mcp_server.py`
(+1): `wiki_get_page` returns the identical resolved-link shape. Full suite: 639 tests green — no
existing test needed updating, since nothing previously asserted an exact-dict equality on `GET
/pages/{id}`'s response. Live-verified against real dev Postgres through the rebuilt `gateway`
container: seeded two real workspaces with a real cross-workspace link, the caller initially
holding no grant at all in the target workspace — confirmed a real request returned `"links": []`
while the raw `content` field still showed the written link text verbatim; granted real `reader`
access via a direct DB write and re-ran the identical request — the same link resolved this time,
no gateway restart needed anywhere in the cycle. Cleaned up the throwaway `live63a-*`/`live63b-*`
workspaces afterward.

**Spec touch-point** (applied): none required — `01` §1's own table and `09` §31's own flag
already specify this behavior in full; this step closes the gap, not a deviation from either.

## 69. Stuck-Pipeline Sweep Detector (Phase 3 Step 64)

**Only three `PipelineState`s can ever be observed persisted in a "stuck" resting state:
`submitted`, `classified`, `ingesting`.** Traced from the real transaction boundaries, not
assumed off the state diagram: `tasks._classify`/`_curate` each wrap their entire unit of work in
exactly one `db.session_scope()` (`async with session.begin(): yield session`), which commits only
on successful exit and rolls back entirely on any exception. `classifying` and `duplicate_check`
each exist only *inside* one of those transactions, alongside the exit transition that leaves
them — a crash there always rolls back to whichever state existed before that task started, the
same finding step 33's own live kill-test already recorded ("killed worker-classification
mid-task... the source sat at `submitted` for minutes afterward," not `classifying`). There is
nothing for this detector to find in either state, and nothing for an admin to abort there — `05`
§2 names "a source stuck mid-pipeline" as a gap without specifying which states qualify; this is
the concrete answer, derived rather than guessed.

**Retry needs zero new pipeline-transition edges.** `submitted` is exactly the state a lost first
dispatch leaves a source in — re-`classify_source.delay()`-ing it directly is safe. `classified`
and `ingesting` are already re-entrant by design: `tasks._curate`'s own `if pipeline_state is
classified: run check_duplicates / elif ingesting: call curate_source directly` branching exists
for exactly this re-dispatch shape (an admin-resolved duplicate already lands a source at
`ingesting` and re-enters this same task). Retry for all three is "call the right existing task
again," nothing more.

**Abort needs exactly one new, precedented edge set.** `pipeline.py`'s `ABORTABLE_IF_STUCK =
{submitted, classified, ingesting}` — the identical three-state set `find_stuck_sources` scans —
each gain a direct `-> rejected` transition, added via the same fold-in-a-loop pattern the module
already uses for `FAILABLE -> error`. `rejected` was previously reachable only through
`pending_review` (`_RESUME_FROM_REVIEW`, itself never exercised by any real code path — confirmed
by grep, no caller transitions `pending_review -> classifying/duplicate_check` or `error ->
pending_review` today); this widens where an admin can decline a source without touching that
existing, still-dormant edge set at all.

**A new `review_item.kind`: `ReviewKind.stuck`** (migration `7cc67060951f`, `ALTER TYPE
review_kind ADD VALUE`). None of the five existing kinds fit "a source parked mid-pipeline" —
`submission`/`classification`/`duplicate` are all per-source at the point a source *needs*
attention, not per-batch after the fact; `reindex`/`prune` are the closest shape (batched,
workspace-scoped) but carry their own kind-specific resolution vocabulary already. Confirmed with
the user directly (a rejected first attempt at this question, then a clarifying "do we have abort
and retry" that led to the transaction-boundary tracing above, then a second, simpler question)
rather than assumed.

**A global sweep, not per-workspace like the other five detectors.** `find_stuck_sources` scans
across every workspace in one call, and `run_stuck_pipeline_detector`/its Celery wrapper
(`tasks.detect_stuck_pipelines`, own `maintenance-stuck-pipeline-detector` beat entry, default
hourly via `KARPWIKI_MAINTENANCE_STUCK_PIPELINE_INTERVAL_HOURS`) take no `workspace_id` at all —
a `submitted`-stuck source has no workspace yet (`raw_source.workspace_id` is nullable exactly
for this reason, `03` §1), so the existing `_dispatch_daily_detectors`'s per-workspace fan-out
(which assumes every detector it calls takes one) cannot cover it. One batched item per *run*,
`workspace_id=None` — the same shape `submission`/`classification` items already use (`09` §22),
not a gap; resolution authorization already handles a `None`-workspace item via
`any_workspace_with_role`, so no new AuthZ code was needed.

**Detection threshold sits well above the crash-recovery window on purpose**:
`KARPWIKI_STUCK_PIPELINE_THRESHOLD_HOURS` (default 1h) is ~6x
`CELERY_VISIBILITY_TIMEOUT_SECONDS` (600s/10min default, `09` §36) — long enough that a genuine
worker crash's own automatic redelivery has time to self-heal first, so this only fires for what
that mechanism can't explain (a broker message genuinely dropped, dispatch code never reached).
"How long stuck" is read off each source's latest `ingestion_log.created_at`, not
`raw_source.created_at` (which only ever records original submission time, not time-in-current-
state).

**Resolution split mirrors `resolve_reindex`/`resolve_prune`'s existing circular-import
avoidance, plus one new wrinkle.** `advisor.resolve_stuck` is bookkeeping-only (validates
kind/action, calls `review.resolve`) for the same reason those two are: `tasks.py` already imports
`ingestion.py`, so `advisor.py` can't dispatch Celery tasks itself. `retry`'s actual re-dispatch —
reading each source's *recorded* state out of `item.detail["sources"]` and calling
`classify_source.delay`/`curate_source.delay` accordingly — happens in `api.run_resolve_review_item`
after commit, extending the same post-commit dispatch block `reindex`'s own page-list dispatch
already uses. `abort`'s pipeline-side work — the actual `-> rejected` transition per source —
happens one layer earlier than reindex/prune's pattern, inside `ingestion.resolve_review_item`'s
new `stuck` branch, by calling the *existing* `ingestion.reject_source` (added step 13, unchanged)
directly for each source still sitting in an abortable state at resolution time. Reusing it rather
than re-deriving the same three lines (transition, `raw_source.status = rejected`, conditional
placeholder-page rewrite) in advisor.py was deliberate: `advisor.py` can't import `ingestion.py`
(the reverse import already exists), but `ingestion.py` calling its own function is free, and
`reject_source`'s existing `if source.workspace_id is not None` guard already does exactly the
right thing for a workspace-less `submitted`-stuck source (skips the placeholder rewrite, since
none exists yet). A source that progressed past its recorded state before an admin got to it
(finished on its own between detection and resolution) is silently skipped rather than forced
backward — checked against the source's *current* `pipeline_state`, not the snapshot in
`item.detail`.

**`pipeline.py`'s state machine genuinely widened**: `submitted -> rejected` is now a legal
`reject_source` transition where it previously raised `IllegalTransition` —
`tests/test_placeholder.py`'s `test_reject_is_legal_only_from_pending_review` asserted the old,
narrower behavior and was rewritten (`test_reject_is_legal_from_the_stuck_pipeline_abortable_states`
covers the new positive case; `test_reject_is_illegal_from_a_state_nothing_ever_finds_a_source_
resting_in` keeps the negative case alive, moved to `classifying` — a state that remains provably
unreachable-at-rest either way).

**Verification**: `tests/test_advisor.py` (+11): `find_stuck_sources` past/within threshold,
ignores `classifying`/`duplicate_check`/`pending_review`, config-default threshold;
`run_stuck_pipeline_detector` creates a workspace-less item, no-findings, skip-if-open;
`resolve_stuck` bookkeeping-only, wrong-kind, unsupported-action. `tests/test_dispatch.py` (+4,
through the real `POST /review-items/{id}/resolve` endpoint): retry dispatches `classify_source`
for a `submitted` source and `curate_source` for a `classified` one, abort rejects with no
dispatch, dismiss leaves the source untouched. `tests/test_tasks.py` (+3): task/beat-schedule
registration, `_detect_stuck_pipelines` raises a workspace-less item against the real task
database. `tests/test_placeholder.py`: 1 test rewritten into 2 (above). Full suite: 658 tests
green. Live-verified against the real dev stack: applied migration `7cc67060951f` directly against
dev Postgres, rebuilt and restarted `gateway`/all three affected workers/`celery-beat`; confirmed
`maintenance-stuck-pipeline-detector` registered in the live beat process. Seeded a real
`submitted` source with a `created_at`/`ingestion_log` entry backdated 3 hours, ran
`tasks.detect_stuck_pipelines.apply()` in the live `worker-maintenance` container — it raised a
real, workspace-less review item. Resolved it `retry` through the real gateway (`POST /review-
items/{id}/resolve` via nginx on :8080) — `worker-classification`'s own logs show it genuinely
picked up the re-dispatched `classify_source` task off the real Redis broker (it then failed on a
`FileNotFoundError` against the object store, expected: the seeded row was a synthetic DB-only
fixture with no real object body, not a defect in the dispatch path itself). Seeded a second
source at `classified` in a real throwaway workspace, resolved it `abort` through the same live
endpoint — confirmed both `raw_source.pipeline_state` and `.status` flipped to `rejected` and its
placeholder page was rewritten to the rejected-page content, all through `reject_source`'s reuse
path. Cleaned up all seeded rows and the throwaway workspace afterward.

**Spec touch-point** (applied): `05` §2's named gap ("a source stuck mid-pipeline needs a sweep
detector — not built yet") is closed. No deviation from `03` §1's transition table beyond the
one documented, precedented widening above.

## 70. Fine-Grained (page_type) Access Control, and Step 65's Global-Admin Question Resolved
(Phase 3 Steps 65 and 70)

Built together, per `phase3-tasklist.md` step 65's own instruction ("resolve alongside step 70
... not before it, since that's the first feature where the answer might actually matter").

**Step 65 resolved: no separate global-admin/cross-workspace primitive is needed — building the
real fine-grained feature confirms this rather than just re-asserting `09` §22's original
reasoning.** Every grant, scoped or not, stays `(workspace_id, principal, scope)` — page_type
scoping is a pure *narrowing* of what a workspace-level role already covers, never a *widening*
across workspaces. "Global admin across all workspaces" (`06` §3's principal table) still has no
concrete caller three phases in; `any_workspace_with_role`'s "admin in at least one workspace"
workaround remains sufficient for every workspace-less case that exists. Formally closed, not left
open a fourth phase.

**Scope dimension: `page_type` only, for real; `tag` scoping is a named, deferred gap.** `07` §2
says "per-`page_type` *or* per-tag" — `page_type` is a stable column directly on `WikiPage`, free
at every call site that already has the page loaded; `tags` live in per-version `frontmatter`
JSONB, which would force a join onto several currently-simple endpoints (`GET /pages/{id}`, link
resolution, the version-history admin gates) for a feature this step's own spec text treats as
optional between the two. Matches this project's existing precedent for scoping a real primitive
tightly and naming the rest (`doc_extract`'s legacy-`.doc` exclusion, `dedicated_index`'s
approximations) — confirmed via AskUserQuestion before building, not assumed.

**Schema: `access_policy` gained a `scope` column** (migration `88ee7671b581`; PK widened to
`(workspace_id, principal, scope)`) rather than a new table — one table the AuthZ layer already
reads, `scope=""` (the server-default backfill for every pre-existing row) meaning exactly what
every grant meant before this step. A non-empty `scope` is always `f"page_type:{value}"`
(`auth.page_type_scope`, the one place this string is built). Confirmed via AskUserQuestion.

**Enforcement model: opt-in restriction, admin bypass.** A `page_type` becomes restricted the
moment *any* scoped grant exists for it in a workspace — an untouched workspace behaves exactly as
before this step, by construction (`auth._scope_is_restricted`). Once restricted, the plain
workspace-wide role is no longer sufficient on its own for that type; the principal needs its own
matching `page_type:<value>`-scoped grant at the required rank. Workspace `admin` always bypasses
— an admin already controls the workspace's own grants, including this one, so gating admin
against its own configuration would be circular. This is the one part of the design not offered as
a fork to the user — the spec's "visible only to a subset of readers" framing has no other
coherent reading once opt-in-per-type is the shape.

**`effective_role`/`has_role`/`any_workspace_with_role` needed a real, necessary correction, not
just an addition**: before this step they implicitly meant "workspace-wide" by never filtering on
`scope` at all; once `scope` became a real PK component, a principal holding *only* a narrow
`page_type:X` grant would otherwise have silently counted as holding that role across the whole
workspace (e.g. for submission's `any_workspace_with_role`). Fixed by filtering every one of these
three to `scope == ""` explicitly — `effective_role` also gained an optional `scope` parameter
(default `""`, so every pre-step-70 call site is byte-for-byte unaffected) so the new page-scoped
checks could reuse it directly rather than duplicating the role-reduction logic.

**Two new `auth.py` functions, not one, because point-checks and list-checks have genuinely
different cost shapes**: `has_role_for_page(principal, page, required)` for the single-page case
(`_reader_page`, `_resolve_page_links`'s per-target check) — one workspace-role lookup, one
restriction-exists check, one scoped-role lookup, all cheap for exactly one page.
`visible_page_types(principal, workspace_id, required)` for the list/search case — batches to
*two* queries total regardless of result-set size (every restricted scope in the workspace, then
this principal's own scoped grants), avoiding an N+1 that a naive per-result `has_role_for_page`
call would otherwise cause on `GET /pages`/`GET /search`.

**`GET /pages` and `wiki_list_pages` intersect the filter at the query level, not a post-fetch
filter** — `api._visible_page_type_filter` narrows the caller's own optional `page_type` request
(or the full type set, if none) against `visible_page_types`, and the *narrowed* list is what
`versioning.list_pages` actually queries with. This keeps cursor pagination exactly correct (a
post-filter would silently return fewer than `limit` items on a page boundary even when more
visible ones exist beyond it). One real subtlety: `versioning.list_pages` already treats an empty
`page_types` list as "no filter" (falsy-list shortcut) — so a principal who can see *zero* matching
types needs the caller to short-circuit to an empty result explicitly, never pass `[]` through, or
that shortcut would silently return everything instead of nothing. `mcp_server.py`'s `wiki_list_pages`
duplicates only the thin call-site wiring (this module's own established convention for the eight
simpler tools) — the actual filtering logic is the one shared `api._visible_page_type_filter` call.

**`GET /search` post-filters instead**, a deliberate, narrower exception to the above: results
span multiple workspaces in one federated call, each with its own independent restriction
configuration, and `search.search()`/`dedicated_index.search()` both take one flat `page_types`
filter applied uniformly across every workspace in the call — pushing per-workspace visibility down
into that shared query would mean either N separate backend calls (breaking the existing
one-call-covers-every-workspace batching `04` §4 relies on) or denormalizing scope state into the
query itself. Post-filtering `results` once per distinct `workspace_id` present (not per result)
keeps this to a bounded number of extra lookups and accepts the same "can return fewer than
`limit` even when more exist" approximation this codebase already carries elsewhere for omission-
based filters (`_resolve_page_links`, the partial-failure degradation). A filtered-out result is
also never written to `query_log` — it was never really shown, matching the "never confirm an
inaccessible target's existence" reasoning `_resolve_page_links` already established.

**Grant/revoke API**: `GrantAccessRequest` gained an optional `page_type` field, orthogonal to
`fuse_access` exactly the way `fuse_access` is already orthogonal to `role` — omitted grants
workspace-wide (unchanged from every grant before this step); naming one upserts a separate,
narrower row. `DELETE .../access-policy/{principal}` gained an optional `page_type` query
parameter with the identical default-omitted-means-workspace-wide shape, so the endpoint's
existing no-argument behavior is byte-for-byte unchanged. `_access_policy_body` gained a
`"page_type"` key (`null` for a workspace-wide row) rather than exposing the raw `scope` string,
keeping the wire format symmetric with the request.

**Verification**: `tests/test_auth.py` (new, 12 tests) — the two new `auth.py` functions unit-
tested directly (unlike every other AuthZ primitive in this codebase, tested only through
endpoints; these two have enough real internal logic to warrant it): unrestricted falls back to
workspace role, no role at all denies, a restricted type denies a plain workspace reader, grants
the scoped principal (including via a group), admin always bypasses, `visible_page_types`'s
admin/unrestricted/restricted-with-and-without-grant cases, and a regression proving
`effective_role`'s new `scope` parameter defaults to today's workspace-wide behavior unchanged.
`tests/test_workspaces.py` (+4): scoped grants are separate rows, upsert targets the right one,
revoke-by-scope leaves the workspace-wide grant untouched. `tests/test_workspaces_api.py` (+3):
the REST grant/revoke surface for `page_type`. `tests/test_pages_sources_api.py` (+6): `GET
/pages/{id}` and `GET /pages` list enforcement, including the explicit-filter-for-an-invisible-
type-returns-empty case and the admin-bypass case. `tests/test_federated_search_api.py` (+2):
search omits/includes a restricted result. `tests/test_mcp_server.py` (+1): `wiki_list_pages`
parity. Two pre-existing tests needed a real, necessary update, not a workaround: the PK widened
from a 2-tuple to a 3-tuple, so `session.get(AccessPolicy, (ws, principal))` in
`test_connectors.py`/`test_connectors_api.py` became a 3-tuple with `scope=""`, and
`test_workspaces_api.py`'s one exact-dict-equality assertion gained the new `"page_type": None`
key. Full suite: 686 tests green. Live-verified against the real dev stack: applied the migration
directly, rebuilt/restarted `gateway`; seeded a real workspace with a real `concept` page and a
real `entity` page, a plain admin, and a plain reader — confirmed the reader saw both before any
restriction. Granted a real scoped `page_type:entity` reader grant to a *different* principal
through the live `POST /workspaces/{id}/access-policy` endpoint — the plain reader immediately lost
both list and direct-fetch access to the entity page (`GET /pages` dropped it, direct `GET
/pages/{id}` returned a real 403), while the real admin's direct fetch still succeeded (bypass).
Granted the plain reader the same scope — access came back on the next request, no gateway restart
anywhere in the cycle. Revoked just the scoped grant via `DELETE .../access-policy/{principal}?
page_type=entity` — confirmed an explicit `?page_type=entity` list request now correctly returned
an empty result (not "no filter") while the reader's own workspace-wide grant and its `concept`
visibility stayed untouched. Cleaned up the throwaway `live70-ws` workspace afterward.

**Spec touch-point** (applied): `07` §2's fine-grained access control roadmap item is built (the
`page_type` half; `tag` remains a named, deferred gap). `phase3-tasklist.md` step 65's own
global-admin question is formally resolved: not needed, closed rather than carried forward again.

## 71. API Pagination-Contract Gap Resolved: Documented, Not Extended (Phase 3 Step 66)

Step 66's own text left the fix genuinely open ("a design call for whoever picks up this step").
Checked with Deepak directly before choosing, first surfacing the full inventory of every list
endpoint's pagination status (six already cursor-paginated, each backed by a real `created_at`
column; the four gap endpoints plus `/search` without one), then the real cost each option
carries — closing the gap wasn't the fork; how to close it was.

**Resolved: document as deliberately unpaginated, capped instead of cursor-paginated — not
extend real cursor pagination.** The deciding fact, found while investigating: none of the four
affected tables (`DocumentType`, `Connector`, `Workspace`, `AccessPolicy`) has a `created_at`
column at all — `DocumentType`'s PK is `type_code` (a string), `AccessPolicy`'s is a three-string
composite `(workspace_id, principal, scope)`, and `Workspace`/`Connector` simply never got one.
Real cursor pagination would have meant four new migrations (mirroring `RawSource.created_at`'s
own precedent from step 43), a materially bigger lift than "extending an existing pattern" first
suggested. Weighed against that cost: all four lists are deployment-*configuration* cardinality
(a workspace's own taxonomy, connector count, grant count; a deployment's own workspace count),
not append-heavy content tables like `page_version`/`raw_source`/`review_item` — confirmed
directly with Deepak, who doesn't expect hundreds of workspaces or connectors, and for
`document-types` specifically: it's an admin-console-only view (no reader-facing consumer, no
agent/automation touches it — classification reads the taxonomy through a *separate*,
intentionally-unfiltered `document_types.list_active()` call, unrelated to this question), and
its realistic ceiling tracks workspace count rather than growing independently.

**Mechanically**: each of the six underlying list functions
(`document_types.list_for_workspace(s)`, `connectors.list_for_workspace(s)`,
`workspaces.list_for_principal`, `workspaces.list_access`) gained a `limit: int =
DEFAULT_LIST_LIMIT` parameter, clamped via `min(limit, MAX_LIST_LIMIT)` and a plain SQL
`.limit(...)` — the exact same constants (`pagination.py`) every cursor-paginated endpoint
already clamps against, just without the cursor half. The four REST endpoints (`GET
/document-types`, `GET /connectors`, `GET /workspaces`, `GET /workspaces/{id}/access-policy`) and
the MCP `wiki_list_workspaces` tool now accept an optional `limit` query param/argument and pass
it straight through — no `next_cursor` key in any response, ever, matching `/search`'s own
existing "capped, no cursor" contract precedent exactly (`DEFAULT_SEARCH_LIMIT`/
`MAX_SEARCH_LIMIT`, added in the post-Phase-2 hardcoding-remediation pass).

**Verification**: `tests/test_document_types.py`/`test_connectors.py`/`test_workspaces.py` (+2
each): `limit` actually caps the underlying query on both the single-workspace and
multi-workspace/aggregate variants. `tests/test_document_types_api.py`/`test_connectors_api.py`/
`test_workspaces_api.py` (+1 each, the latter covering both `/workspaces` and
`/workspaces/{id}/access-policy`): the REST `?limit=` param caps results and the response
carries no `"next_cursor"` key at all (not `null` — genuinely absent, same as `/search`'s own
convention). Full suite: 696 tests green (10 new), everything else unaffected — every existing
call site's result set already sits under `DEFAULT_LIST_LIMIT` (50), so this changed no observed
behavior at default settings. Live-verified against the real dev stack: rebuilt/restarted
`gateway` (no migration — no schema changed), seeded a real workspace with 2 document types, 2
connectors, and 3 access-policy grants, confirmed `?limit=1` against all four live endpoints each
returned exactly 1 item with no `next_cursor` key present, and the unlimited default call
returned all rows. Cleaned up the throwaway `live66-ws` workspace afterward.

**Spec touch-point** (applied): `09` §14's cursor-pagination contract now explicitly excludes
these four (and `/search`, already documented) rather than silently falling short of it —
resolves the gap `phase3-tasklist.md` step 66 named, per the option the spec itself left open.

---
Previous: [08-implementation-stack.md](08-implementation-stack.md) · Back to: [00-overview.md](00-overview.md)
