# Specification Review: Completeness, Clarity, Consistency, and Gaps

Date: 2026-06-15
Scope reviewed: spec/00-overview.md through spec/07-additional-features-and-roadmap.md
Reference baseline: Karpathy's LLM Wiki gist (raw/wiki/schema, ingest/query/lint pattern)

## Executive Summary

The specification is strong on architecture depth and enterprise operationalization of the LLM Wiki idea. It preserves key Karpathy principles (immutable raw sources, LLM-maintained wiki, schema-driven behavior, index/log discipline) while adding multi-workspace routing, review workflows, and scaling concerns.

Main concerns are not about missing high-level concepts; they are about contract-level consistency and implementation precision. The largest risks are enum/state inconsistencies and underspecified retrieval score-merging behavior.

## Findings (Ordered by Severity)

### 1) Review-item schema inconsistency across documents

Issue:
- The Metadata DB schema omits `classification` from `review_item.kind`, while ingestion and admin flows explicitly require it.

Evidence:
- spec/02-storage-and-indexing.md: conceptual `review_item.kind` lists `submission|duplicate|reindex|prune`.
- spec/03-ingestion-and-review-workflows.md: defines and uses `kind=classification`.
- spec/05-admin-backend-and-maintenance.md: queue includes `classification` items.

Impact:
- Different teams can implement divergent enums in DB/API/UI, causing runtime failures and unresolved queue items.

Recommendation:
- Define a single canonical enum set and reference it from all docs; update spec/02 to include `classification`.

---

### 2) Page-status model conflicts with ingestion placeholder states

Issue:
- Global page status is defined as `draft|published|archived`, but ingestion flow introduces placeholder page statuses/labels like `processing` and `error`.

Evidence:
- spec/01-architecture-and-data-model.md: required frontmatter `status: draft | published | archived`.
- spec/03-ingestion-and-review-workflows.md: placeholder source page status/marking includes `processing` and `error`.

Impact:
- Ambiguity over whether `processing/error` are frontmatter status values, derived UI state, or pipeline state only.

Recommendation:
- Separate concerns explicitly:
  - keep page status enum stable (`draft|published|archived`),
  - model ingest visibility with a distinct field (e.g., `ingestion_state` / `processing_state`) and document rendering rules.

---

### 3) Dedicated-index score normalization is acknowledged but not specified

Issue:
- Federated search describes normalization for dedicated index scores before merging, but leaves algorithm and tie-break behavior undefined.

Evidence:
- spec/04-search-and-retrieval.md: merge step says normalize dedicated-index scores into shared scale, with example-style wording.

Impact:
- Relevance ranking may differ significantly between implementations/backends; hard to validate search quality and reproducibility.

Recommendation:
- Specify a normative merge contract: normalization formula, tie-break order, deterministic stable sorting, and evaluation criteria.

---

### 4) Index consistency/state semantics need tighter wording

Issue:
- The consistency section mixes statements about serving previous indexed content with transitions to stale/pending without precise ordering semantics.

Evidence:
- spec/02-storage-and-indexing.md: commit point and eventual-consistency window language around `indexed` and `stale` behavior.

Impact:
- Operators may disagree about expected query behavior during reindex windows.

Recommendation:
- Add explicit transition ordering and serving contract table:
  - when writes happen,
  - what search serves at each `index_status`,
  - expected max lag/SLO and alert thresholds.

## Completeness Review

### What is complete and strong

- Clear architecture decomposition with gateway/services/async/storage layers.
- Strong ingestion-to-review operational model with admin controls.
- Good treatment of versioning and rollback as non-destructive.
- Practical API + MCP surface mapping and scaling strategy.
- Roadmap includes governance, reliability, and quality extensions.

### Intentionally incomplete (and correctly marked as such)

- Exact API request/response schemas are out of scope.
- Numeric NFR targets are placeholders pending organizational sizing.

These are acceptable omissions for an architecture-level spec, but they are blockers for implementation kickoff unless complemented by follow-on docs.

## Clarity Review

Strengths:
- Overall narrative is coherent and references are well-structured.
- Diagrams and traceability mapping are helpful and mostly consistent.

Needs improvement:
- Normative vs optional language is sometimes mixed.
- State and enum definitions are distributed across files without a single canonical contract section.

Recommendation:
- Add a short "Normative Contracts" appendix containing canonical enums, transitions, and authoritative source-of-truth statements.

## Consistency With Karpathy LLM Wiki Pattern

Aligned:
- Immutable raw sources as source of truth.
- LLM-maintained interlinked markdown wiki.
- Schema-guided behavior.
- Ingest/query/lint as the core loop.
- `index.md` and `log.md` as first-class operating artifacts.

Intentional divergence (acceptable if explicit):
- Query-time synthesis is delegated to consuming agents, while platform retrieval remains lexical and non-LLM in-path. This is a product boundary choice that should remain explicitly documented for stakeholders.

## Missing Details to Add (Implementation Readiness Gaps)

1. Canonical enums and transitions
- `review_item.kind`, `review_item.status`, page status vs ingestion/pipeline states, allowed transitions, and rejection/error handling.

2. API contract details
- Idempotency keys, pagination and cursor format, error schema, retry semantics, partial-failure responses, rate-limit headers.

3. Security/connector operational details
- Credential storage/rotation model, secret scope, connector permission boundaries, audit fields for connector actions.

4. Cross-workspace lifecycle semantics
- Behavior of links and search exposure when a workspace is archived/deleted/migrated.

5. Data governance specifics
- Retention defaults, query-log privacy policy, anonymization, legal hold precedence vs pruning lifecycle.

6. Taxonomy migration procedure
- How re-routing document types affects existing pages/sources, bulk move safeguards, rollback plan.

7. Retrieval quality governance
- Relevance test set ownership, acceptance metrics, and regression process for ranking changes.

## Suggested Priority Plan

P0 (before implementation starts):
- Fix enum/state inconsistencies (Findings #1 and #2).
- Define federated scoring/merge contract (Finding #3).

P1 (during API design):
- Publish API/MCP request-response schemas and operational semantics.
- Add security and connector credential model.

P2 (before production hardening):
- Governance/compliance operational runbooks.
- Retrieval quality benchmarking and release gates.

## Final Assessment

Overall quality: High at architecture level; Medium for implementation readiness.

The specification is conceptually complete for design intent and strongly aligned with the Karpathy pattern, but it needs a compact normative contract layer (enums, transitions, and ranking semantics) to avoid integration drift across backend, admin UI, search, and MCP surfaces.
