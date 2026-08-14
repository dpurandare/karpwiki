<p align="center">
  <img src="assets/icon.svg" width="120" height="120" alt="karpwiki icon">
</p>

# Enterprise Wiki Platform — Specification

A specification for an **Enterprise Wiki Platform** that adapts Andrej Karpathy's
[LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — a small,
LLM-maintained knowledge base built from raw sources, a curated wiki, and a schema — into a
multi-workspace, horizontally scalable system for enterprise use.

## Background

### Karpathy Wiki for Agentic AI

Andrej Karpathy's original pattern is a small, LLM-maintained wiki meant to be read and kept
up to date by an AI agent rather than a human editor. This spec keeps that agent-first design
center: the dual API + MCP interfaces mean any agentic AI system can query the curated wiki or
submit new raw sources for ingestion directly, without a human in the loop.

- **Core idea**: raw sources → curated wiki → schema, with ingest / query / lint as the three
  operations (Karpathy's pattern).
- **Enterprise extensions**: multi-workspace partitioning by document type, a common gateway
  fronting all storage/indices/logs, async ingestion with human review, federated lexical search
  (no vector index), versioning/rollback, and dual API + MCP interfaces. As there is no reference implementation of Karpathy's wiki available, this is an attempt to define the detailed specifications for the implementation (and possible reference implementation).

The specification in [`spec/`](spec/) is written to be implementable as-is — it can be handed
to an engineering team (or an AI coding agent) to build directly.

## Specification

The full specification lives in [`spec/`](spec/), as eight documents meant to be read in order:

| Doc | Title |
|---|---|
| [00](spec/00-overview.md) | Overview — purpose, design principles, scope, requirements traceability |
| [01](spec/01-architecture-and-data-model.md) | Architecture and Data Model |
| [02](spec/02-storage-and-indexing.md) | Storage and Indexing |
| [03](spec/03-ingestion-and-review-workflows.md) | Ingestion and Review Workflows |
| [04](spec/04-search-and-retrieval.md) | Search and Retrieval |
| [05](spec/05-admin-backend-and-maintenance.md) | Admin Backend and Maintenance |
| [06](spec/06-api-mcp-and-scaling.md) | API, MCP, and Scaling |
| [07](spec/07-additional-features-and-roadmap.md) | Additional Features and Roadmap |

Start with [`spec/00-overview.md`](spec/00-overview.md).

## Implementation

Phase 1 of [`spec/phase1-tasklist.md`](spec/phase1-tasklist.md) is being built in this repo under
[`src/karpwiki/`](src/karpwiki/). **1a (core architecture and data layer) is complete**: the seven
core Metadata DB tables, the append-only versioning model with non-destructive rollback, required
frontmatter validation, the object-store adapter, and the Celery queue definitions.

```bash
docker compose up -d                     # PostgreSQL + Redis
pip install -e '.[dev]'                  # Python 3.11+
alembic upgrade head                     # create the schema
pytest                                   # 1a step-6 verification
```

Tests need a `karpwiki_test` database (`createdb karpwiki_test`, or
`docker exec karpwiki-postgres-1 psql -U karpwiki -c 'CREATE DATABASE karpwiki_test;'`). Override
`KARPWIKI_DATABASE_URL`, `KARPWIKI_OBJECT_STORE_URL`, and `KARPWIKI_CELERY_BROKER_URL` to point at
other backends.

The two agents' models are configuration, resolved per role
([09](spec/09-implementation-notes.md) §16) — the same model in every environment:

```bash
export KARPWIKI_LLM_CLASSIFIER_MODEL=openai:gpt-5-nano
export KARPWIKI_LLM_CURATOR_MODEL=openai:gpt-5-nano
```

Neither has a default in code: an unset role raises `ModelNotConfiguredError` rather than silently
picking a model. A workspace's `SCHEMA.md` may override either role. §16 records what this
cost-first choice trades and the three signals that should trigger raising the curator's tier.

## Reference Implementation

[`spec/08-implementation-stack.md`](spec/08-implementation-stack.md) is an optional appendix
pinning a concrete Python stack (FastAPI, Celery, PostgreSQL, fsspec, Pydantic AI, etc.) to the
vendor-neutral roles defined in `00`–`07`. [`spec/09-implementation-notes.md`](spec/09-implementation-notes.md)
follows up with concrete design decisions for a handful of implementation-readiness gaps
(pipeline-state tracking, connector execution, MCP delegation, a `SCHEMA.md` example, `diff_ref`
format).
