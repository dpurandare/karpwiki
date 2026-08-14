# 08 — Implementation Stack (Python Reference)

## 1. Purpose and Scope

Docs `00`–`07` are the authoritative, vendor-neutral architecture — per [00](00-overview.md) §3,
specific vendor/product selection is deliberately out of scope there, with each storage/service
role described generically and illustrated with example technologies.

This document is a separate, optional appendix: **one concrete reference implementation in
Python**, pinning a specific library or service for each role `00`–`07` describe generically.
Substituting any choice below does not require changes to `00`–`07`'s contracts — those documents
remain correct regardless of which implementation is built against them.

## 2. Stack at a Glance

| Role | Spec reference | Choice |
|---|---|---|
| Common Gateway / API | [01](01-architecture-and-data-model.md) §2, [06](06-api-mcp-and-scaling.md) §1 | FastAPI |
| Async Layer / Job Queue | [01](01-architecture-and-data-model.md) §1, [06](06-api-mcp-and-scaling.md) §4 | Celery, Redis broker (RabbitMQ is a drop-in alternative) |
| Metadata DB | [02](02-storage-and-indexing.md) §3 | PostgreSQL, via SQLAlchemy 2.0 (async) + Alembic |
| Full-Text Index | [02](02-storage-and-indexing.md) §4 | PostgreSQL full-text search (shared default); OpenSearch for per-workspace dedicated index instances |
| Object Store | [02](02-storage-and-indexing.md) §2 | fsspec, with `s3fs` / `gcsfs` / `adlfs` / local backends |
| Append-Only Log / Event Store | [02](02-storage-and-indexing.md) §5 | Time-partitioned tables in the Metadata DB (PostgreSQL) |
| Cache (optional) | [02](02-storage-and-indexing.md) §6 | Redis (can share the Celery broker instance via a separate logical DB) |
| MCP Surface | [06](06-api-mcp-and-scaling.md) §2 | Official `mcp` Python SDK |
| LLM Layer — Curator Agent / Classifier | [01](01-architecture-and-data-model.md) §1, [07](07-additional-features-and-roadmap.md) §1 | Pydantic AI, defaulting to Claude Opus 5 (`claude-opus-5`) for both agents ([09](09-implementation-notes.md) §16) |
| Auth | [06](06-api-mcp-and-scaling.md) §3 | Authlib (OIDC/SAML) + PyJWT (API keys) |

## 3. Notes on Selected Roles

### Full-Text Index ([02](02-storage-and-indexing.md) §4)

- PostgreSQL full-text search (`tsvector`/GIN indexes) is the shared default — same database as
  the Metadata DB, no extra service for the common case.
- Workspaces large or isolated enough to warrant a "dedicated index instance" (§4) use OpenSearch
  instead, chosen for its per-language analyzer support — useful for the multi-language roadmap
  item ([07](07-additional-features-and-roadmap.md) §4).
- The Search Service's score-normalization and tie-break logic
  ([04](04-search-and-retrieval.md) §4) applies when merging OpenSearch results from a dedicated
  workspace with the shared PostgreSQL-FTS results.

### Async Layer / Job Queue ([01](01-architecture-and-data-model.md) §1, [06](06-api-mcp-and-scaling.md) §4)

- Celery (Redis broker by default) provides separate worker pools per job type — classification,
  curation/ingest, indexing, maintenance advisor — matching the per-job-type pools in
  [06](06-api-mcp-and-scaling.md) §4, plus built-in periodic scheduling for the Maintenance
  Advisor's detectors ([05](05-admin-backend-and-maintenance.md) §2) and connector polling.

### LLM Layer — Curator Agent / Classifier ([01](01-architecture-and-data-model.md) §1, [07](07-additional-features-and-roadmap.md) §1)

- Pydantic AI structures both agents' outputs as typed Pydantic models — e.g. the Classifier's
  `document_type` + confidence + `content_shape` + `artifact_identity`/`source_version`
  ([03](03-ingestion-and-review-workflows.md) §3), and the Curator's structure-table/intent-statement
  extraction for `structured_data` sources ([07](07-additional-features-and-roadmap.md) §1.3).
- Its model-provider abstraction keeps the LLM provider swappable, consistent with
  [00](00-overview.md) §3's "LLM provider out of scope." The default model for both agents is
  Claude Opus 5 — a configuration value, not a code dependency
  ([09](09-implementation-notes.md) §16).
- Two consequences of that default for prompt construction: order each agent's prompt
  stable-prefix-first (system prompt + taxonomy or `SCHEMA.md` rules, *then* the source document)
  so the per-workspace prefix is cacheable across a batch, and size `max_tokens` for thinking plus
  output rather than output alone.
- Pydantic models can be shared between this layer and the FastAPI layer where useful (e.g., a
  `ClassificationResult` model used both as an LLM structured-output schema and an API response
  shape).

### Object Store ([02](02-storage-and-indexing.md) §2)

- fsspec with `s3fs` / `gcsfs` / `adlfs` / local backends covers every example technology listed
  in [02](02-storage-and-indexing.md) §1 (AWS S3, GCS, Azure Blob, MinIO, Cloudflare R2) through
  one API — each workspace's `storage_binding` ([01](01-architecture-and-data-model.md) §3) is just
  a URL (`s3://...`, `gs://...`, `az://...`, `file://...`).
- fsspec backends can be FUSE-mounted, directly enabling the "file-based agent access" path
  described in [02](02-storage-and-indexing.md) §2 — an agent gets real filesystem/grep access to
  the wiki export regardless of the underlying object store.
- Storage-tier/lifecycle policies (`active`→`archived`→cold storage, [02](02-storage-and-indexing.md)
  §2) remain provider-specific bucket configuration (infra-as-code, out of scope per
  [00](00-overview.md) §3) — unaffected by this choice.

### Append-Only Log / Event Store and Cache ([02](02-storage-and-indexing.md) §5–6)

- Both default to "no extra service": logs as time-partitioned PostgreSQL tables (the first
  example technology listed in [02](02-storage-and-indexing.md) §1), cache as Redis (already
  present as the Celery broker). Either can be split out to a dedicated store (Kafka, a separate
  Redis cluster) at scale without changing `00`–`07`'s contracts.

## 4. Python Dependencies (indicative)

- `fastapi`, `uvicorn`
- `celery`, `redis`
- `sqlalchemy[asyncio]`, `alembic`, `asyncpg`
- `opensearch-py` (dedicated-index workspaces only)
- `fsspec`, `s3fs`, `gcsfs`, `adlfs`
- `mcp`
- `pydantic-ai`
- `authlib`, `pyjwt`

---
Previous: [07-additional-features-and-roadmap.md](07-additional-features-and-roadmap.md) · Next: [09-implementation-notes.md](09-implementation-notes.md) · Back to: [00-overview.md](00-overview.md)
