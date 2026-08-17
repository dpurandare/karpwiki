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

---
Previous: [08-implementation-stack.md](08-implementation-stack.md) · Back to: [00-overview.md](00-overview.md)
