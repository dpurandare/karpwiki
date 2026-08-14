# 09 — Implementation Notes (Design Decisions)

## 1. Purpose and Scope

`techfeasibility.md` (working doc, not part of this spec) flagged a set of implementation-
readiness gaps in `00`–`08`. The first pass (§3–7) covered questions concrete enough to answer as
design decisions, in the same spirit as `08`'s reference-implementation choices. A second pass
(§8–12) covers the remaining org/runtime decisions — retention defaults, classifier confidence
calibration, relevance-test ownership, taxonomy bulk-move safeguards, and FUSE access scope —
using values supplied by the organization adopting this spec. A third pass (§13) closes the
connector credential/security gap left open by §4, which covered connector *execution* only. A
fourth pass (§14–16) settles what Phase 1's first endpoints need before they can be written — API
conventions, the baseline auth scope, and the LLM model default. NFR targets specifically are
recorded in [06](06-api-mcp-and-scaling.md) §6, the placeholder table designated for that purpose.

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
| LLM provider and model | [00](00-overview.md) §3, [08](08-implementation-stack.md) §2 | Claude Opus 5 (`claude-opus-5`) for Classifier and Curator via Pydantic AI's provider abstraction; prompts ordered stable-prefix-first for caching |

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

thresholds:
  staleness:
    high_traffic_days: 90       # re-check sooner for frequently-queried pages (05 §2)
    low_traffic_days: 365
  classification:
    min_confidence: 0.75        # below this -> classification review item (03 §3, §9 above)
  dedup:
    near_duplicate_score: 0.85  # FTS "more like this" score, normalized 0-1 (03 §4)
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
provider-specific log-prob APIs. This score is **periodically recalibrated**: admin resolutions of
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

**Pagination.** Cursor-based, not offset. List endpoints accept `limit` (default 50, max 200) and
an opaque `cursor`, and return `{"items": [...], "next_cursor": <string|null>}`. The cursor encodes
the sort key plus a tiebreak id. Offset pagination is wrong for this data: `page_version`,
`raw_source`, and the log streams are append-heavy and partitioned (`02` §3), so rows inserted
mid-scan shift every later offset and a paging admin silently skips items.

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

## 16. LLM Provider and Model Default

`00` §3 puts LLM provider selection out of scope and `08` §2 picks Pydantic AI as the agent
framework without naming a model — leaving the Classifier (`03` §3) and Curator (`03` §6) with no
concrete model to run against.

**Decision**: **Claude Opus 5 (`claude-opus-5`) for both roles**, reached through Pydantic AI's
model-provider abstraction so `00` §3's provider neutrality holds — the model is a configuration
value, not a code dependency, and a workspace could run a different one.

Both roles suit the strongest tier available. The Classifier's output gates the pipeline
(`03` §3 step 6): a wrong `document_type` routes a source to the wrong workspace, and a
miscalibrated confidence either floods the review queue or waves bad classifications through.
The Curator's output *is* the product — the wiki pages users read and cite.

Cost is not the constraint here. At `06` §6's ingestion rate of 10–100 documents/hour, and Claude
Opus 5's $5/$25 per million input/output tokens, the LLM line is small next to the engineering
time a weaker classifier costs in review-queue volume. Trading down to a cheaper tier is a decision
for the adopting organization once real classification-accuracy data exists (`09` §9's calibration
loop is what produces that data), not a default to assume up front.

Two implementation notes that follow from the choice:

- **Structure the prompts for caching.** Both agents send a large stable prefix and a small
  variable suffix — the Classifier its system prompt plus the document-type taxonomy (`01` §3), the
  Curator its `SCHEMA.md` curator rules and tone (`01` §7, §6 above) — with the source document
  last. Caching is a prefix match, so the stable part must physically precede the variable part;
  ordered that way, per-workspace prefixes are cached across every document in a batch and read at
  roughly a tenth of input cost. A prefix must reach 512 tokens to cache at all on this model, which
  a taxonomy plus curator rules comfortably clears.
- **Size `max_tokens` for thinking plus output.** Thinking is on by default on this model and
  counts against the same ceiling, so a limit sized only for the expected `ClassificationResult`
  will truncate.

Confidence remains self-reported in the structured output per §9 — Pydantic AI's typed result
models are how both agents return it.

**Spec touch-point** (applied): `08` §2's LLM-layer row names the default model, and `08` §3's
LLM-layer notes record the caching and `max_tokens` guidance.

---
Previous: [08-implementation-stack.md](08-implementation-stack.md) · Back to: [00-overview.md](00-overview.md)
