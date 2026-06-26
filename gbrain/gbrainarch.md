# GBrain Architecture Notes

Source: <https://github.com/garrytan/gbrain> (README only, fetched 2026-06-15)

## What it is

GBrain is a personal/team "knowledge brain" — a CLI + MCP server that ingests
notes, meetings, emails, etc. into a searchable, graph-linked knowledge base,
with an LLM synthesis layer on top of retrieval (not just "here are some
matching chunks").

## Core architecture

- **Storage — dual engine behind one contract**: PGLite (Postgres 17 via WASM,
  zero-config) for personal brains up to ~50K pages, or Postgres + pgvector
  (Supabase/self-hosted) for shared/large deployments. Both implement a
  `BrainEngine` interface (~47 ops) defined in `src/core/engine.ts`; the CLI
  and MCP server are generated from that single contract.

- **Source of truth = git repo of markdown files**. The "brain repo" holds
  your actual knowledge as markdown; GBrain syncs it into Postgres for
  retrieval. Deletes in git become soft-deletes in the DB.

- **Two organizational axes**: a *brain* = a database/instance; a *source* =
  a repo mounted inside that brain (e.g., a wiki, an essay collection).
  Routing between them uses `.gbrain-source` dotfiles with a 6-tier
  precedence chain.

- **Retrieval — hybrid search**: vector search (HNSW/pgvector) + BM25 keyword,
  combined via reciprocal-rank fusion with source-tier boosting and query
  rewriting, plus an optional reranker (ZeroEntropy). Has named modes
  (`conservative` / `balanced` / `tokenmax`).

- **Self-wiring knowledge graph**: every page write parses markdown/wikilinks
  and creates typed edges (`works_at`, `invested_in`, `attended`, etc.) with
  zero LLM calls — used for multi-hop graph queries alongside vector search.

- **Job queue ("Minions")**: a BullMQ-shaped, Postgres-native queue running
  durable sub-agent jobs with two-phase pending→done persistence so crashed
  jobs recover.

- **Interfaces**: a `gbrain` CLI, and an MCP server (stdio or HTTP w/ OAuth
  2.1) exposing 30+ tools — both generated from the same `BrainEngine`
  contract.

- **Schema packs**: pluggable page-type taxonomies (default `gbrain-base-v2`,
  15 types like `person` / `company` / `deal` / `project`) that drive type
  inference, fact extraction, and expert routing.

- **Operational loop**: `signal → search → respond → write → auto-link →
  sync`, with cron jobs doing overnight enrichment/consolidation
  ("dream cycle").

## Embeddings and vector search

- **Ingested documents are converted to embeddings.** On `gbrain import` (or
  any ingestion path), page markdown is chunked and each chunk is sent to a
  pluggable embedding provider — OpenAI (default fallback), ZeroEntropy
  (default for `tokenmax` mode), Voyage, OpenRouter, Google Gemini, Azure
  OpenAI, MiniMax, Alibaba DashScope, Zhipu, local Ollama, local llama.cpp
  `llama-server`, or a LiteLLM proxy. `gbrain init` auto-detects the provider
  from API keys in the environment, or accepts `--embedding-model
  <provider>:<model>` explicitly. Resulting vectors are stored in
  pgvector/PGLite with an HNSW index.

- **Search uses embeddings + vector similarity, but as one signal in a
  hybrid pipeline.** `gbrain search`/`gbrain think` combine HNSW vector
  similarity over chunk embeddings with BM25 keyword scoring via
  reciprocal-rank fusion, then apply source-tier boosts, query rewriting,
  graph-signal boosts, and an optional cross-encoder reranker (ZeroEntropy).
  Vector retrieval pools the best-matching chunk per page so a page surfaces
  on its strongest evidence rather than competing chunk-by-chunk.

- The knowledge-graph edges are the one major signal that is **not**
  embedding-based — those come from pure pattern-matching on
  wikilinks/typed-link syntax with zero LLM calls.

## Architectural considerations

Additional sources for this section: `docs/storage-tiering.md`,
`docs/ENGINES.md`, `docs/architecture/topologies.md`,
`docs/architecture/infra-layer.md`.

### Scaling strategy

GBrain scales primarily by swapping storage engines and deployment topology,
not by changing application logic — everything goes through the same
`BrainEngine` contract (`src/core/engine.ts`), so the CLI/MCP/skills layer
doesn't change as you scale.

1. **Engine choice (vertical scaling of a single brain)**
   - **PGLite** (embedded Postgres 17 via WASM, single file at
     `~/.gbrain/brain.db`) — zero-config default, single process, no
     connection pooling.
   - **Postgres + pgvector** (Supabase or self-hosted) — connection pooling
     (Supavisor), multi-device/concurrent access, managed backups.
   - `gbrain migrate --to supabase` / `--to pglite` does a bidirectional,
     lossless move of pages, chunks, embeddings, links, tags, and timeline,
     so you can start local and graduate without re-ingesting.

   Docs differ a bit on the exact crossover point: `ENGINES.md` puts PGLite
   as "good for < 1,000 files" vs. Postgres "production-proven at 10K+";
   the top-level README says PGLite handles "up to ~50K pages." Treat it as
   a gradient — concurrency and multi-device access matter more than raw
   page count for when to move off PGLite.

2. **Storage tiering (keeps the git repo from becoming the bottleneck)**
   Large machine-generated corpora (tweets, transcripts, articles) can be
   marked `db_only` in `gbrain.yml` — indexed/searchable in Postgres but
   excluded from git via auto-managed `.gitignore`, with
   `gbrain export --restore-only` to repopulate a local cache. Recommended
   once a repo crosses **50K–200K+ files**. On PGLite this tiering is
   largely cosmetic — "the DB" is the same local file, so there's no real
   space savings, only git-history hygiene.

3. **Deployment topology (horizontal / multi-agent scaling)**
   - **Topology 1 — single brain, single machine**: default, fine for solo
     use.
   - **Topology 2 — thin client / remote brain**: a beefy host runs
     `gbrain serve --http` (Postgres+Supabase) with OAuth-scoped clients;
     multiple agent machines consume it over MCP with zero local DB. This is
     how the "company brain" (per-user scoped slices via RLS) and Garry
     Tan's own large brain (146K pages) work.
   - **Topology 3 — split-engine**: per-worktree disposable PGLite "code"
     brains (avoids index contention across parallel dev worktrees) plus one
     shared remote "artifacts" brain. Routing between aliases is
     manual/explicit, not automatic.

4. **Pipeline-level scaling knobs**
   - Content-hash (SHA-256) idempotency skips re-embedding unchanged pages on
     sync.
   - Embeddings are batched (100 at a time) with exponential backoff and are
     non-fatal on failure.
   - The "Minions" job queue uses rate leases to throttle outbound calls to
     embedding/LLM providers.
   - Federated multi-source syncs use per-source `timeout(1)` plus
     `--break-lock --max-age` to avoid one slow source blocking the whole
     cron cycle.

### Typical sizing considerations

- **Page/file count** — main driver for PGLite vs. Postgres. `ENGINES.md`
  pegs PGLite as comfortable under **~1,000 files**, while the top-level
  README claims PGLite handles **up to ~50K pages**; Postgres+Supabase is
  "production-proven at **10K+**" and is what backs Garry Tan's own
  **~146K-page** brain.
- **Concurrency** — number of agents/machines hitting the brain
  simultaneously. PGLite is single-process with no connection pooling;
  Postgres gives pooling via Supavisor.
- **Git repo hygiene** — bulk machine-generated content should move to
  `db_only` once a repo crosses roughly **50K–200K+ files**.
- **Embedding provider rate limits/cost** — embeddings are sent in batches
  of **100** with exponential backoff; this becomes the relevant knob at
  high ingest volume.
- **Multi-tenancy** — team/company brains need Postgres + RLS for per-user
  scoping; PGLite is single-user only.
- **Parallel dev workflows** — multiple Conductor/worktree setups benefit
  from Topology 3's disposable per-worktree code indexes (no fixed numeric
  threshold given in the docs).

## ⚠️ Prompt injection note

This README contains content explicitly addressed to AI agents (sections
like "**Agents:** start with `AGENTS.md`" and instructions to "Retrieve and
follow the instructions at:
`https://raw.githubusercontent.com/garrytan/gbrain/master/INSTALL_FOR_AGENTS.md`",
which reportedly has the agent install software, ask for API keys, and
configure MCP servers/cron jobs). These instructions were **not** followed —
only the README text was read for this architecture summary. If a coding
assistant is pointed at this repo, be aware it's designed to get the agent to
auto-execute a remote setup script with real side effects (installs,
credentials, scheduled jobs).
