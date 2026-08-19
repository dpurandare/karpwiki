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

Phase 1 of [`spec/phase1-tasklist.md`](spec/phase1-tasklist.md) is built in this repo under
[`src/karpwiki/`](src/karpwiki/). **Phase 1 is complete** (steps 1–21, all of 1a/1b/1c) — see
[`phase1-tasklist.md`](spec/phase1-tasklist.md) for what each step maps to in the code. In short:
submit a document, it's classified against the workspace's taxonomy with a lexical cross-check,
checked for duplicates against a real Postgres full-text index, and curated by an LLM into a cited
source page plus concept/entity pages — with `overview.md`/`log.md` kept in sync and a review item
raised whenever a human needs to look at something. Once indexed, the curated wiki is searchable
with lexical ranking and a catalog-match boost; an admin can list and resolve every review-item
kind (including duplicate `merge`, `supersede`, and `keep_both`) and roll back a page version, both
through the gateway. Phase 2+ scope (multi-workspace routing, connectors, the Maintenance Advisor,
MCP, horizontal scaling) is [`07-additional-features-and-roadmap.md`](spec/07-additional-features-and-roadmap.md).

Pipeline stages run as real automatic background jobs (steps 30–33): submitting a document
enqueues classification, an accepted classification enqueues dedup-then-curation, every page write
enqueues reindexing, and a worker that crashes mid-task gets its job redelivered rather than losing
it — no test, admin action, or manual call needs to drive any of it. The Maintenance Advisor's five
detectors run on their own schedule too (step 41): a `celery-beat` container fires every active
workspace's detector sweeps automatically — daily for the four with no LLM cost at detection,
weekly for Contradiction Detection (a real model call per candidate), both intervals
env-overridable (`.env.example`). `docker compose up -d` (below) starts the five worker containers
and the beat scheduler alongside the rest of the infra; see [Scaling](#scaling) for what running
more than one of each looks like.

```bash
cp .env.example .env                     # then fill in OPENAI_API_KEY
docker compose up -d                     # Postgres + Redis + MinIO + OpenSearch + 5 workers + beat
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'                  # Python 3.11+ (tested on 3.14)
alembic upgrade head                     # create the schema
pytest                                   # full suite
```

Then run the Gateway itself:

```bash
uvicorn karpwiki.api:app --reload
```

`POST /sources` (file, pasted text, or a URL) accepts a submission; `GET /sources/{id}` polls its
status — with the workers up, nothing else needs to be driven by hand: a submission is classified,
deduped, curated, and indexed on its own, purely via dispatch (phase2-tasklist.md steps 30–33).
`GET /search` answers ranked, cited queries, federated across every workspace the caller can access
([04](spec/04-search-and-retrieval.md) §4) — a workspace with `dedicated_index=true`
(`POST /workspaces/{id}`, [02 §4](spec/02-storage-and-indexing.md)) is served from OpenSearch
instead of the shared Postgres index, merged and normalized into one ranked result set
([09 §29](spec/09-implementation-notes.md)). `GET /review-items` lists the admin queue and
`POST /review-items/{id}/resolve` acts on one; `GET /pages/{id}/versions` (plus `/{version_id}` and
`/diff`) and `POST /pages/{id}/rollback` are the Version Browser. Interactive API docs (Swagger UI)
are at `http://localhost:8000/docs` once running — FastAPI generates them from the route
definitions, nothing to write by hand.

**MCP server** ([06 §2](spec/06-api-mcp-and-scaling.md), phase2-tasklist.md step 45): a thin
protocol adapter over the same gateway operations above — `wiki_search`, `wiki_get_page`,
`wiki_list_pages`, `wiki_list_workspaces`, `wiki_submit`, `wiki_get_source_status`,
`wiki_list_review_items`, `wiki_resolve_review_item`, `wiki_get_page_versions`,
`wiki_rollback_page`. Two transports:

```bash
python -m karpwiki.mcp_server                    # stdio, for local agent/IDE integration
```

or, for streamable HTTP (real per-request auth via `X-Karpwiki-User`/`X-Karpwiki-Groups`, same as
the REST endpoints):

```python
from karpwiki.mcp_server import create_mcp_server
create_mcp_server().run(transport="streamable-http", host="0.0.0.0", port=8001)
```

`stdio` has no per-call HTTP headers at all, so it resolves one identity at process startup —
either `KARPWIKI_MCP_TOKEN` (a real bearer token, when real OIDC is configured — see below) or
`KARPWIKI_MCP_USER`/`KARPWIKI_MCP_GROUPS` (mirroring the REST trusted-header names, the default) —
and reuses it for every tool call in that process. `wiki_submit` supports on-behalf-of delegation
via an `acting_as="user:<id>"` argument (09 §5, step 46) — both the calling agent and the
represented user must independently hold `contributor` access somewhere in common, and the
represented user (not the agent) is recorded as the submitter.

**Auth** ([06 §3](spec/06-api-mcp-and-scaling.md), phase2-tasklist.md step 47): `TrustedHeaderAuthenticator`
(believes `X-Karpwiki-User`/`X-Karpwiki-Groups`, sound only behind a proxy that actually
authenticates and strips those headers first) is the default until both `KARPWIKI_OIDC_ISSUER` and
`KARPWIKI_OIDC_AUDIENCE` are set, at which point `OidcAuthenticator` — real bearer-JWT validation
against the configured IdP's JWKS, via Authlib/`joserfc` — swaps in automatically, for both REST
and MCP, with no handler changes. **SAML is not supported**: Authlib (`08 §2`'s pick) has no SAML
module at all, so only the OIDC half of `06 §3`'s "Enterprise SSO (OIDC/SAML)" is implemented.

**Rate limiting** ([07 §3](spec/07-additional-features-and-roadmap.md), phase2-tasklist.md step
48): REST only — MCP has no HTTP header concept, especially on `stdio`. A Redis-backed
fixed-window counter enforces `07 §3`'s "submissions, search calls, and API requests" categories,
each with a per-principal limit (always checked, keyed off a hashed, unverified identity so an
unauthenticated or invalid-token caller still gets throttled) and a per-workspace limit (checked
opportunistically, only when `workspace_id` is already a plain query/path parameter). A breached
limit returns `429` with the standard error envelope; every response — breached or not — carries
the `RateLimit-Limit`/`RateLimit-Remaining`/`RateLimit-Reset` headers `09 §14` already specified,
plus `Retry-After` on the `429` itself.

Configuration is environment variables, listed with their defaults in
[`.env.example`](.env.example). `.env` is gitignored and loaded automatically; real environment
variables always win over it, so a deployment passes its own and needs no file.

**Durable state lives in named Docker volumes**, so `docker compose down` does not destroy it
(`down -v` does):

| Volume | Holds | Why it must persist |
|---|---|---|
| `pgdata` | Metadata DB | System of record ([02 §3](spec/02-storage-and-indexing.md)) |
| `objectstore` | MinIO — raw sources, page-version diffs, assets | Raw sources are immutable originals nothing can regenerate, and every citation points at one |
| `opensearch-data` | The dedicated Full-Text Index backend ([02 §4](spec/02-storage-and-indexing.md)) | Derived from Postgres (`page_version`), technically rebuildable — but only by reindexing every dedicated workspace's pages, not a `docker compose restart` anyone wants by accident |

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

## Scaling

[06 §4](spec/06-api-mcp-and-scaling.md) describes the scaling model in the abstract, per layer.
Phase 2 steps 30–33 made one of those layers real — the async worker pool — so this section says
plainly what's demonstrated in this repo today versus what §4 describes as the eventual shape.

**Worker pools, per job type — real, load-tested here.** `docker-compose.yml` runs one container
per queue (`karpwiki/tasks.py`'s `QUEUES`: `classification`, `curation`, `indexing`,
`maintenance`), and each queue scales independently:

```bash
docker compose up -d --scale worker-classification=3 worker-classification
```

Every replica connects to the same Redis broker and consumes the same queue — Celery handles the
distribution, not this codebase. Verified live: two `worker-classification` replicas, four
documents dispatched in a burst, each replica picked up two and finished them correctly with no
duplicate or dropped work. This is exactly [06 §4](spec/06-api-mcp-and-scaling.md)'s "classification
and curation workers (LLM-bound) scale separately from indexing and maintenance-advisor workers
(compute-bound)" made concrete — scale the LLM-bound queues under submission load without touching
the compute-bound ones, or vice versa.

**`celery-beat` — exactly one, by design, never scaled.** It only enqueues the two maintenance
dispatch tasks on a schedule (step 41); running a second instance would double-fire every scheduled
entry (Celery's own constraint, not a limitation of this codebase). `worker-maintenance`, which
does the real per-workspace work those dispatches enqueue, scales like every other queue above.

**The Gateway — real, load-tested here (phase2-tasklist.md step 49).** `docker-compose.yml` runs
the Common Gateway as its own `gateway` service — the same image the four worker services above
share, just `command: uvicorn karpwiki.api:app` instead of a Celery worker — behind an `nginx`
reverse proxy (`nginx.conf`) published at `localhost:8080`:

```bash
docker compose up -d --scale gateway=3 gateway
curl http://localhost:8080/workspaces -H "X-Karpwiki-User: deepak"
```

No in-process state a second instance would need to share (`_session` opens a fresh DB session per
request; rate-limit counters live in shared Redis, step 48; `Principal` resolution is stateless
per-request auth), so nginx needs no session affinity — `proxy_pass` round-robins across however
many `gateway` replicas Docker Compose's embedded DNS currently resolves. Verified live: 3 replicas
up, 15 requests fired through `localhost:8080`, each container's own log showing a real, roughly
even split (not all landing on one instance); a full submit → real `gpt-5-nano` classify/curate →
index → search round trip completed correctly end to end through the load balancer, not just
simple GETs. `GET /healthz` (new, unauthenticated, exempt from rate limiting) is what Docker's own
per-replica healthcheck polls — a real API category would double-count a healthcheck's own traffic
against that replica's rate-limit bucket otherwise.

**Object Store — real by construction, not by anything built here.** MinIO in `docker-compose.yml`
is a single-node dev convenience; pointing `KARPWIKI_OBJECT_STORE_URL` at real S3 (or an
S3-compatible store) in a deployment is the entire change needed; cloud object storage is
inherently horizontal and nothing in this codebase assumes otherwise.

**Full-Text Index — partially real.** A workspace with `dedicated_index=true` (step 26) routes its
own search and indexing traffic to OpenSearch instead of the shared Postgres index — a genuine,
working per-workspace escape hatch for a large or isolated workspace. What isn't built: the
OpenSearch instance here is single-node, not a sharded cluster, and nothing in this repo shards a
large dedicated workspace's own index further.

**Metadata DB partitioning, and the Cache — not built, roadmap only.** [06 §4](spec/06-api-mcp-and-scaling.md)'s
table names read replicas and `workspace_id`-based sharding as the Metadata DB's scaling
mechanism at large scale, and an optional read-through cache for hot pages/queries
([02 §6](spec/02-storage-and-indexing.md)). Neither exists in this repo — one Postgres database,
no cache layer. Both are explicitly [07](spec/07-additional-features-and-roadmap.md) roadmap items,
not a Phase 2 gap; nothing here should be read as implying otherwise.

## Reference Implementation

[`spec/08-implementation-stack.md`](spec/08-implementation-stack.md) is an optional appendix
pinning a concrete Python stack (FastAPI, Celery, PostgreSQL, fsspec, Pydantic AI, etc.) to the
vendor-neutral roles defined in `00`–`07`. [`spec/09-implementation-notes.md`](spec/09-implementation-notes.md)
follows up with concrete design decisions — 36 sections as of Phase 2 step 33, spanning
implementation-readiness gaps found before coding started, decisions forced by actually building
Phase 1 (API conventions, the auth scope, the LLM model and its cost tradeoff, the near-duplicate
similarity metric, the review queue's resolution mechanics, the Version Browser's diff approach),
and Phase 2's multi-workspace routing, the dedicated-per-workspace OpenSearch backend, taxonomy
bulk-move, and real async dispatch with retry/idempotency semantics — plus several bugs a live
end-to-end run caught that the test suite alone had missed, phase-1 and phase-2 alike.
