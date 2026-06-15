# Enterprise Wiki Platform — Specification

A specification for an **Enterprise Wiki Platform** that adapts Andrej Karpathy's
[LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — a small,
LLM-maintained knowledge base built from raw sources, a curated wiki, and a schema — into a
multi-workspace, horizontally scalable system for enterprise use.

## Background

- **Core idea**: raw sources → curated wiki → schema, with ingest / query / lint as the three
  operations (Karpathy's pattern).
- **Enterprise extensions**: multi-workspace partitioning by document type, a common gateway
  fronting all storage/indices/logs, async ingestion with human review, federated lexical search
  (no vector index), versioning/rollback, and dual API + MCP interfaces. As there is no reference implementation of Karpathy's wiki available, this is an attempt to define the detailed specifications for the implementation (and possible reference implementation).

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

## Reference Implementation

[`spec/08-implementation-stack.md`](spec/08-implementation-stack.md) is an optional appendix
pinning a concrete Python stack (FastAPI, Celery, PostgreSQL, fsspec, Pydantic AI, etc.) to the
vendor-neutral roles defined in `00`–`07`. [`spec/09-implementation-notes.md`](spec/09-implementation-notes.md)
follows up with concrete design decisions for a handful of implementation-readiness gaps
(pipeline-state tracking, connector execution, MCP delegation, a `SCHEMA.md` example, `diff_ref`
format).
