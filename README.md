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
cp .env.example .env                     # then fill in OPENAI_API_KEY
docker compose up -d                     # PostgreSQL + Redis + MinIO
pip install -e '.[dev]'                  # Python 3.11+
alembic upgrade head                     # create the schema
pytest                                   # 1a step-6 verification
```

Configuration is environment variables, listed with their defaults in
[`.env.example`](.env.example). `.env` is gitignored and loaded automatically; real environment
variables always win over it, so a deployment passes its own and needs no file.

**Durable state lives in named Docker volumes**, so `docker compose down` does not destroy it
(`down -v` does):

| Volume | Holds | Why it must persist |
|---|---|---|
| `pgdata` | Metadata DB | System of record ([02 §3](spec/02-storage-and-indexing.md)) |
| `objectstore` | MinIO — raw sources, page-version diffs, assets | Raw sources are immutable originals nothing can regenerate, and every citation points at one |

Redis has no volume on purpose: it is only the Celery broker here, and queued work is re-derivable
from `raw_source.pipeline_state` in Postgres ([09 §3](spec/09-implementation-notes.md)).

MinIO also means local development exercises the same `s3://` fsspec path a deployment uses rather
than a `file://` path production never takes. Console at `localhost:9001` (`karpwiki` /
`karpwiki-dev-secret`). Tests are the deliberate exception — [conftest.py](tests/conftest.py)
points them at a throwaway temp directory so runs stay hermetic and leave nothing behind.

Tests need a `karpwiki_test` database (`createdb karpwiki_test`, or
`docker exec karpwiki-postgres-1 psql -U karpwiki -c 'CREATE DATABASE karpwiki_test;'`). Override
`KARPWIKI_DATABASE_URL`, `KARPWIKI_OBJECT_STORE_URL`, and `KARPWIKI_CELERY_BROKER_URL` to point at
other backends.

The two agents' models are configuration, resolved per role
([09](spec/09-implementation-notes.md) §16) — `openai:gpt-5-nano` in every environment. Neither has
a default in code: an unset role raises `ModelNotConfiguredError` rather than silently picking a
model, and a workspace's `SCHEMA.md` may override either role. §16 records what this cost-first
choice trades and the three signals that should trigger raising the curator's tier.

**`OPENAI_API_KEY` is never read by this codebase** — the OpenAI SDK and Pydantic AI read it from
the environment themselves, so the Platform never holds or logs it. Locally it comes from `.env`;
in a deployment the secrets manager injects it at container start.

## Reference Implementation

[`spec/08-implementation-stack.md`](spec/08-implementation-stack.md) is an optional appendix
pinning a concrete Python stack (FastAPI, Celery, PostgreSQL, fsspec, Pydantic AI, etc.) to the
vendor-neutral roles defined in `00`–`07`. [`spec/09-implementation-notes.md`](spec/09-implementation-notes.md)
follows up with concrete design decisions for a handful of implementation-readiness gaps
(pipeline-state tracking, connector execution, MCP delegation, a `SCHEMA.md` example, `diff_ref`
format).
