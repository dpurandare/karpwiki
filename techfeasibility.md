# Technical Feasibility / Implementation-Readiness Concerns

These are **not spec defects** — `spec/00`–`09` is internally consistent (multiple review rounds
resolved or explicitly deferred every finding, and the last two open items were closed on
2026-08-14). These are items the spec deliberately leaves open (per `00` §3's vendor-neutral scope
and "numeric capacity planning... TBD by org" stance), or that surfaced during review as needing a
concrete answer before/during implementation. Hand this list to whoever is designing or coding
each area.

## Status (2026-06-15): all resolved or explicitly deferred

| Concern | Resolution |
|---|---|
| Connector execution model (§1) | `spec/09` §4 |
| Pipeline-state storage (§1) | `spec/09` §3 |
| Classifier confidence calibration (§1) | `spec/09` §9 |
| MCP on-behalf-of delegation (§2) | `spec/09` §5 |
| API contract details (§3) | `spec/09` §14 (general conventions — pagination, error envelope, idempotency, partial-failure, rate-limit headers); endpoint-specific detail lands as each endpoint is implemented |
| `SCHEMA.md` example (§4) | `spec/09` §6 |
| `query_log` retention/anonymization (§4) | `spec/09` §8 |
| Retention defaults / `legal_hold` (§4) | `spec/09` §8 |
| `diff_ref` format (§4) | `spec/09` §7 |
| Taxonomy bulk-move safeguards (§4) | `spec/09` §11 |
| Relevance test sets (§5) | `spec/09` §10 |
| Cross-backend normalization validation (§5) | Deferred — empirical check once both backends run with real content |
| FUSE-mount sandboxing (§6) | `spec/09` §12 |
| Storage-tier/lifecycle per-provider (§6) | No decision needed — informational note, budget as N configs |
| NFR targets (§7) | `spec/06` §6 |

## 1. Ingestion Pipeline & Connectors

- **Connector execution model is unspecified.** `03` §2 says connectors "discover new/changed
  content and submit it the same way," and `05` §7 covers *configuring* connectors (schedule,
  credentials, ingestion policy), but no doc says *how a connector run executes*: which async
  worker pool runs it (`01` §1's Async Layer doesn't list one), and how "discovers new/changed
  content" is detected — polling, webhook, or diff-against-last-sync. Also absent from the
  roadmap (`07` §6).

- **Where does the 9-state ingestion pipeline state (`03` §1) actually live?** `02` §3's
  `raw_source` table has `status` (`active|superseded|archived|rejected`) — a *lifecycle/
  retention* axis — but the pipeline-progress axis (`submitted → classifying → ... → ingested`,
  9 states) isn't a field on any table in `02` §3. `06` §1 says `sources/{id}` "get status"
  returns "Pipeline state from `03` §1." Plausible answer: derive it from the latest
  `ingestion_log` entry (`02` §5) rather than a new column — but this needs to be a deliberate
  design choice, not discovered mid-implementation.

- **Classifier confidence score — how is it produced/calibrated?** `03` §1 and §3 gate
  routing/review on "confidence ≥ the workspace's configured threshold," for an LLM-based
  classifier. Self-reported by the LLM, a secondary scoring pass, log-prob based? The
  low-confidence review path (`classification` review items) only works if this number means
  something consistent across workspaces.

## 2. Auth & Delegation

- **MCP on-behalf-of delegation for `wiki_submit` (`06` §2) is undefined.** `06` §3's auth table
  covers end users (SSO) and API/MCP clients (API key / OAuth client-credentials) as direct
  principals, but not an agent acting *as a specific end user* vs. *as its own service
  principal*. This affects `page_version.author` attribution (`user:<id>` vs `system:`, `01` §5)
  and the `submission` review item's "who submitted this" (`03` §5).

## 3. API Contract Details (out of scope for `00`–`08`, needed for `06`'s implementation)

- Idempotency keys (for `sources` submit / `review-items/{id}/resolve`).
- Pagination/cursor format for list endpoints (`pages`, `review-items`, `sources`).
- Error response schema and retry semantics.
- Partial-failure responses (e.g. a `search` call where one workspace's index is down).
- Rate-limit headers / response shape for the gateway's rate limiter (`01` §2, `07` §3).

## 4. Data Governance, Retention & Diffs

- **No `SCHEMA.md` example/template.** `01` §7 describes its contents conceptually (taxonomy
  slice, page conventions, curator rules, staleness/pruning thresholds, dedup sensitivity) but
  there's no sample document with starting/default values — an implementer needs *some* default
  thresholds to ship with.
- **`query_log` retention & anonymization policy** (`02` §5, `04` §8) — "subject to
  retention/privacy policy" is mentioned but no default is given.
- **`legal_hold` precedence is now defined (`07` §2) but retention *defaults* aren't** —
  how long are superseded sources / old page versions kept before the
  Superseded-Source Detector (`05` §2) proposes pruning, absent a workspace override?
- **`diff_ref` format unspecified** (`01` §5, `02` §3) — stored diff blob vs. computed on read
  has real storage-cost and `pages/{id}/versions/diff` latency implications (`06` §1).
- **Taxonomy bulk-move safeguards/rollback** (`05` §7's "bulk move workspace" admin action) —
  `05` §7 + the versioning model cover the mechanics, but not batch-size limits, dry-run/preview,
  or partial-failure handling for a large re-home operation.

## 5. Search & Retrieval Quality

- **Relevance test sets / regression process.** No owner or acceptance metric is defined for
  "did this change to ranking/boosting make search better or worse" (`04` §3–4). Needed before
  tuning the catalog-match boost or dedicated-index normalization in production.
- **Cross-backend score normalization in practice** (`08` §3, `04` §4): the spec specifies
  *that* dedicated-index (OpenSearch) scores get min-max normalized before merging with shared
  PostgreSQL-FTS scores, and the tie-break order — but BM25 (Postgres `ts_rank`) and OpenSearch's
  default scoring have different distributions/shapes. Min-max normalization alone may not
  produce comparable *rankings* even if scores land in `[0,1]` — worth a empirical check once
  both backends are running with real content.

## 6. Object Store / fsspec (`08` §3)

- **FUSE-mounting fsspec backends for "file-based agent access"** (`02` §2, `08` §3) is a real
  capability of the chosen stack, but mounting object storage as a filesystem for agent
  processes has its own sandboxing/permission-boundary questions (which agents, which
  workspaces, read-only enforcement) not addressed anywhere in `00`–`08`. Treat as opt-in and
  scope it per workspace's `access_policy` (`02` §3) before enabling.
- **Storage-tier/lifecycle policies remain per-provider** (`02` §2, `08` §3) — fsspec gives one
  API for reads/writes, but `active → archived → cold` tiering rules are still separate
  infra-as-code per backend (S3 lifecycle rules, GCS lifecycle, Azure tiers). Budget this as
  N separate configs, not one.

## 7. Non-Functional Requirements (Numeric)

`06` §6's placeholder table — peak search QPS, ingestion rate, pages per workspace, storage
volume, search latency SLA, review-item resolution SLA per `kind`, availability target — all
**TBD by org**. These drive concrete decisions the spec deliberately doesn't make: Metadata DB
partitioning strategy (`02` §3, `06` §4), whether a given workspace needs a dedicated FTS
instance (`02` §4), and worker-pool sizing (`06` §4). Needed before infra sizing.
