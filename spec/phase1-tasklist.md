# Phase 1 Task List — Foundation

Derived from [07](07-additional-features-and-roadmap.md) §6 (Phase 1: single workspace, basic
ingestion, lexical search, basic admin console), sequenced by dependency. Explicitly **excluded**
from this phase (per the roadmap, they're Phase 2+): multi-workspace routing, connectors,
Maintenance Advisor (staleness/orphan/dedup *background* detectors), full API+MCP surface,
horizontal-scaling infra, fine-grained RBAC.

## 0 — Readiness Items (settle before the step they block)

Found by cross-checking the steps below against the specs they cite. **All four are now closed** —
each row records where the decision lives.

| # | Item | Blocks | Kind |
|---|---|---|---|
| 0.1 | **Classifier confidence threshold has no default.** [03](03-ingestion-and-review-workflows.md) §3 step 6 and [09](09-implementation-notes.md) §9 both say it lives in `SCHEMA.md`, but [09](09-implementation-notes.md) §6's template never defines the key — unlike dedup, staleness, and orphan, which all ship defaults. Add `thresholds.classification.min_confidence`. | Step 10 | **Done** — `0.75` starting default added to [09](09-implementation-notes.md) §6 |
| 0.2 | **Baseline auth is unscoped for Phase 1.** Step 2's schema list omitted `access_policy` and no step covered authn/authz, yet steps 19–20 build an admin console that presupposes an admin role. The exclusions above defer only *fine-grained* RBAC. | Step 7 | **Done** — [09](09-implementation-notes.md) §15: authorization ships in Phase 1, authentication is a pluggable provider (trusted-header now, OIDC/SAML in Phase 2) |
| 0.3 | **API conventions undefined.** Pagination/cursor format, error-response schema, idempotency keys, partial-failure shape, rate-limit headers — deferred in `techfeasibility.md` §3 to "the API design phase," which Phase 1 reaches at its first endpoint. | Step 7 | **Done** — [09](09-implementation-notes.md) §14 |
| 0.4 | **No LLM model chosen.** [00](00-overview.md) §3 puts the provider out of scope and [08](08-implementation-stack.md) picks Pydantic AI but no model. | Step 9 | **Done** — [09](09-implementation-notes.md) §16: the model is configuration per agent role. `openai:gpt-5-nano` for both roles in every environment. Still needs an OpenAI key in the secrets manager before step 9 runs |

| 0.5 | **A Classifier failure has no representable state.** [03](03-ingestion-and-review-workflows.md) §1's diagram admits `error` only from `ingesting`, matching §6's "on failure at any step" — whose steps are the *ingest operation's*. But the Classifier is an external API call, so transient failures are certain, and `classifying → error` is not a legal edge. | Step 9 | **Done** — [03](03-ingestion-and-review-workflows.md) §1: `error` is reachable from every state that runs work, transient failures retry inside the worker rather than becoming states, and `pending_review` resumes at the point the source left instead of always at `ingesting` |

Accepted for Phase 1, no action needed — recorded so they aren't rediscovered as surprises:

- **No relevance-regression signal.** [09](09-implementation-notes.md) §10 designates the search
  feedback loop as that signal, but it is [07](07-additional-features-and-roadmap.md) §4 —
  Phase 2+. Step 17's catalog-match boost is therefore tuned blind in Phase 1, and its magnitude is
  an unspecified constant. Acceptable at this scale.
- **`structured_data` is classified but not specially curated.** Step 9 produces `content_shape`,
  `artifact_identity`, and `source_version` per [03](03-ingestion-and-review-workflows.md) §3
  steps 3–4, but the Curator's structured-data treatment is
  [07](07-additional-features-and-roadmap.md) §1.3, outside Phase 1 — so step 12 treats every
  source as `narrative` while still storing those fields.

## 1a — Core Architecture & Data Layer

1. Stand up PostgreSQL (Metadata DB) + object store (fsspec, local/S3 backend) —
   [02](02-storage-and-indexing.md) §2–3, [08](08-implementation-stack.md) §2.
2. Implement the core schema: `workspace`, `raw_source`, `wiki_page`, `page_version`, `page_link`,
   `index_status`, `review_item`, `access_policy` — [02](02-storage-and-indexing.md) §3 field list.
   Single workspace row is enough for Phase 1; `access_policy` carries the three roles from
   [06](06-api-mcp-and-scaling.md) §3 per [09](09-implementation-notes.md) §15.
3. Implement the versioning model: every page write creates a `page_version`,
   `wiki_page.current_version_id` as a pointer, non-destructive rollback —
   [01](01-architecture-and-data-model.md) §5.
4. Implement required frontmatter validation (`status`, page type, etc.) —
   [01](01-architecture-and-data-model.md) §6.
5. Stand up the async job queue (Celery + Redis) — [01](01-architecture-and-data-model.md) §1,
   [08](08-implementation-stack.md) §2.
6. **Verify**: can create a workspace, write a `wiki_page` + `page_version` directly (no ingestion
   yet), and read it back with correct version pointer.

## 1b — Basic Ingestion (no Advisor, no connectors)

7. Build the submission entry point (upload file / paste text / submit URL) —
   [03](03-ingestion-and-review-workflows.md) §2, end-user path only. This is the first endpoint,
   so it also establishes the gateway's two cross-cutting pieces: principal resolution + role
   enforcement against `access_policy` (pluggable authenticator, trusted-header provider for
   Phase 1 — [09](09-implementation-notes.md) §15), and the API conventions in
   [09](09-implementation-notes.md) §14 (cursor pagination, error envelope, `Idempotency-Key`).
8. Implement the pipeline state machine on `raw_source.pipeline_state` —
   [03](03-ingestion-and-review-workflows.md) §1, [09](09-implementation-notes.md) §3
   (9-state enum as a denormalized pointer + `ingestion_log` history).
9. Build the Classifier step (LLM-based `document_type` + confidence via Pydantic AI) —
   [03](03-ingestion-and-review-workflows.md) §3, [08](08-implementation-stack.md) §2.
   Single-workspace case simplifies workspace resolution (only one target).
10. Implement low-confidence routing to `pending_review` —
    [03](03-ingestion-and-review-workflows.md) §1, §3.
11. Implement duplicate detection at `duplicate_check` (lexical similarity against existing
    pages) — [03](03-ingestion-and-review-workflows.md) §4.
12. Implement the Curator Agent's ingest step: raw source → concept/entity wiki pages,
    `overview.md`, `log.md` updates — [03](03-ingestion-and-review-workflows.md) §6.
13. Implement the placeholder `source` page lifecycle ("processing" → "awaiting review"/
    "rejected"/"error" → final) — [03](03-ingestion-and-review-workflows.md) §1 note on UI
    labels vs. frontmatter `status`.
14. Implement the `submission` and `classification` review items (always-on notice +
    low-confidence gate) — [03](03-ingestion-and-review-workflows.md) §5,
    [02](02-storage-and-indexing.md) §3 `review_item`.
15. **Verify**: submit a real document end-to-end and get a published, cited wiki page — or a
    review item if it's a duplicate/low-confidence.

## 1c — Lexical Search & Basic Admin Console

16. Build the Full-Text Index (PostgreSQL `tsvector`/GIN) over curated wiki pages + `index.md`
    catalog — [02](02-storage-and-indexing.md) §4, [04](04-search-and-retrieval.md) §1–2.
17. Implement single-stage lexical retrieval with catalog-match boost —
    [04](04-search-and-retrieval.md) §1, §3. (Skip §4 federated/cross-workspace merge — single
    workspace only.)
18. Implement the indexing lifecycle: write → `index_status` `pending`/`stale` → reindex job →
    `indexed` — [02](02-storage-and-indexing.md) §7–8.
19. Build the admin console's Review Queue view (list/filter open `review_item`s, resolve
    action) — [05](05-admin-backend-and-maintenance.md) §1.
20. Build the admin console's page version history view + manual rollback —
    [05](05-admin-backend-and-maintenance.md) §6 (rollback only; skip repository management,
    performance monitoring — later phases).
21. **Verify**: search returns ranked, cited results; an admin can see the queue, resolve a
    submission/duplicate/classification item, and roll back a page version.

## Exit Criteria

Matches [07](07-additional-features-and-roadmap.md) §6: a single workspace can ingest documents,
produce concept/entity pages, and answer search queries with citations — admin can review the
queue and page history.

---
Back to: [00-overview.md](00-overview.md)
