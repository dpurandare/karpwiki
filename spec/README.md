# Specification

The full specification for karpwiki, as eight documents meant to be read in order:

| Doc | Title | What it covers |
|---|---|---|
| [00](00-overview.md) | Overview | Why this exists, how it extends Karpathy's pattern, design principles, scope, and requirements traceability |
| [01](01-architecture-and-data-model.md) | Architecture and Data Model | The layered architecture (Common Gateway, Core Services, clients) and the entities/data model behind pages, versions, and workspaces |
| [02](02-storage-and-indexing.md) | Storage and Indexing | Each storage role (object store, metadata DB, full-text index) and why there's deliberately no vector index |
| [03](03-ingestion-and-review-workflows.md) | Ingestion and Review Workflows | The pipeline from a submitted document to a curated wiki page, and where human review (duplicates, low-confidence classification) fits in |
| [04](04-search-and-retrieval.md) | Search and Retrieval | The single-stage lexical retrieval model — ranking and citation, no embeddings or LLM rerank |
| [05](05-admin-backend-and-maintenance.md) | Admin Backend and Maintenance | The Admin Console: review queue, workspace/repository management, version browser/rollback, and the Maintenance Advisor's detectors |
| [06](06-api-mcp-and-scaling.md) | API, MCP, and Scaling | The REST API surface, the MCP tool set, auth, rate limiting, and how each layer is meant to scale |
| [07](07-additional-features-and-roadmap.md) | Additional Features and Roadmap | Non-baseline features (content-shape-based ingestion, connectors, etc.) and the phased roadmap, including deferred Phase 4 scope |

Start with [00-overview.md](00-overview.md).

See the [project README](../README.md) for what's actually built and how to run it.
