# Phase 1 Task List — Foundation

Derived from [07](07-additional-features-and-roadmap.md) §6 (Phase 1: single workspace, basic
ingestion, lexical search, basic admin console), sequenced by dependency. Explicitly **excluded**
from this phase (per the roadmap, they're Phase 2+): multi-workspace routing, connectors,
Maintenance Advisor (staleness/orphan/dedup *background* detectors), full API+MCP surface,
horizontal-scaling infra, fine-grained RBAC.

## 1a — Core Architecture & Data Layer

1. Stand up PostgreSQL (Metadata DB) + object store (fsspec, local/S3 backend) —
   [02](02-storage-and-indexing.md) §2–3, [08](08-implementation-stack.md) §2.
2. Implement the core schema: `workspace`, `raw_source`, `wiki_page`, `page_version`, `page_link`,
   `index_status`, `review_item` — [02](02-storage-and-indexing.md) §3 field list. Single
   workspace row is enough for Phase 1.
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
   [03](03-ingestion-and-review-workflows.md) §2, end-user path only.
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
