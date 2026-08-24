# Production Deployment Guide

This is the piece the reference docs didn't cover: how to run karpwiki somewhere real, not the
`docker-compose.yml` dev stack. [`06-api-mcp-and-scaling.md`](06-api-mcp-and-scaling.md) §4-5
describes the scaling model and deployment topology in the abstract, per layer; this document is
the concrete "how" for the actual code in this repo, grounded in what's really built — every
setting named below is read directly by `src/karpwiki/config.py` or shown by
`docker-compose.yml`'s own real service definitions, not invented for this doc.

If you haven't read [`00-overview.md`](00-overview.md), start there — this guide assumes the
architecture (Common Gateway, Core Services as Celery workers, Metadata DB, Object Store, optional
per-workspace dedicated Full-Text Index) is already familiar.

## 1. What "production" changes from the dev stack

`docker-compose.yml` runs every backing service as a same-host container: Postgres, Redis, MinIO
(standing in for S3), OpenSearch. A real deployment swaps each for a managed equivalent and points
the same application code at it via the same environment variables — nothing in `src/karpwiki/`
branches on "am I in dev or prod," so this is purely a configuration change, not a code change:

| Dev stack (`docker-compose.yml`) | Production equivalent | Config var |
|---|---|---|
| `postgres` container | Managed Postgres (RDS, Cloud SQL, Azure Database for PostgreSQL, ...) | `KARPWIKI_DATABASE_URL` |
| `minio` container | Real S3 (or GCS/Azure Blob — fsspec-backed, `08` §2) | `KARPWIKI_OBJECT_STORE_URL` + the backend's own credential env vars |
| `redis` container | Managed Redis (ElastiCache, Memorystore, ...) | `KARPWIKI_CELERY_BROKER_URL` |
| `opensearch` container | Managed OpenSearch/Elasticsearch-compatible cluster, or omit entirely | `KARPWIKI_OPENSEARCH_URL` |
| `nginx` container | Your real load balancer (cloud LB, ingress controller, ...) | — |

**OpenSearch is optional in production, same as it is in dev**: it only serves workspaces created
with `dedicated_index=True` (`02` §4 — "workspaces with very large corpora or stricter isolation
requirements"). A deployment with no dedicated-index workspaces doesn't need to stand it up at all;
`dedicated_index.search()` and the connector-poll/curation paths that would touch it simply aren't
reached for a workspace that stays on the shared Postgres index.

## 2. Building the image

`Dockerfile` (repo root) is already the real production artifact, not a dev-only convenience — the
same image runs the gateway and every worker pool, `docker-compose.yml`'s own `command:` override
per service is the only thing that differs between them:

```bash
docker build -t <your-registry>/karpwiki:<tag> .
docker push <your-registry>/karpwiki:<tag>
```

It installs `git` (the Git connector adapter shells out to the real CLI, `phase2-tasklist.md` step
54) and runs as a non-root `karpwiki` user. The base `pip install .` does not include the `fuse`
extra (`pyproject.toml`) — only the standalone FUSE-mount CLI (`wiki_mount.py`, step 58) needs it,
and it also needs a kernel-level FUSE driver on whatever host runs it, so it's deliberately not
part of the gateway/worker image. Run `pip install '.[fuse]'` separately, on the specific host
that will actually serve a mount, if you use that feature at all.

## 3. Process topology

One process group per `docker-compose.yml` service, each independently scalable (`06` §4 — "the
workspace is the primary horizontal scaling unit," and within a workspace's own load, each queue
scales on its own signal):

| Process | Command | Scales on |
|---|---|---|
| Gateway | `uvicorn karpwiki.api:app --host 0.0.0.0 --port 8000` | Request volume (stateless — `06` §4; no server-side session, rate-limit counters live in Redis) |
| `worker-classification` | `celery -A karpwiki.tasks worker -Q classification` | LLM latency/cost, not CPU — see §7 |
| `worker-curation` | `celery -A karpwiki.tasks worker -Q curation` | LLM latency/cost, not CPU |
| `worker-indexing` | `celery -A karpwiki.tasks worker -Q indexing` | CPU-bound, cheap (no LLM call — reindexing a page is a lexical-index update, `02` §7) |
| `worker-maintenance` | `celery -A karpwiki.tasks worker -Q maintenance` | CPU-bound, plus the Contradiction Detector's occasional LLM call |
| `worker-connector-polling` | `celery -A karpwiki.tasks worker -Q connector_polling` | I/O-bound (cloning/fetching from source systems) |
| `celery-beat` | `celery -A karpwiki.tasks beat --schedule=<writable-path>` | **Exactly one process, always** — a second `beat` double-fires every scheduled entry |

Every worker command needs a `-n <name>@%h`-style unique node name when running more than one
replica (`docker-compose.yml`'s own convention: `-n classification@%h`), so Celery's own
introspection doesn't collide two replicas under the same identity.

**`celery-beat`'s schedule file needs a writable path for whatever user the container runs as** —
found live in this project's own dev-compose setup (`docker-compose.yml`'s own comment on the
`celery-beat` service): the image's default `WORKDIR` is owned by root from the build, but the
container runs as the non-root `karpwiki` user, so `--schedule=/tmp/celerybeat-schedule` (or any
directory that user can write) is required, not optional. Losing this file on a restart only means
beat re-derives "last run was never" for one tick, not lost application state — worth a persistent
volume for a production deployment, but not correctness-critical if you don't bother.

## 4. Environment variables

[`.env.example`](../.env.example) is the complete, authoritative reference — every variable
`src/karpwiki/config.py` reads, grouped by concern, each with the real default and a one-line
rationale. Copy it and fill in real values; don't re-derive the list here. The handful that need a
real operational decision, not just a value, are called out below.

One of those decisions: `KARPWIKI_LLM_CLASSIFIER_MODEL`/`KARPWIKI_LLM_CURATOR_MODEL` default to
`openai:gpt-5-nano` (`09` §16's cost-first rationale), but either can be pointed at a self-hosted
model via Pydantic AI's built-in `ollama:` provider instead — e.g. `ollama:gemma4:latest` plus
`OLLAMA_BASE_URL` — with no code change (`09` §83). Worth considering where sending content to a
third-party model is the concern, not just cost; validate classification/curation quality against
your own content before relying on it in production, same as any model-tier choice.

## 5. Authentication

**The dev-stack default, `TrustedHeaderAuthenticator`, is explicitly not production-safe on its
own** — `09` §15 frames it as "sound only where the gateway is unreachable except through a proxy
that authenticates and strips these headers" (`X-Karpwiki-User`/`X-Karpwiki-Groups`). It exists so
Phase 1 didn't have to wait on an IdP integration; it is not a hardened auth layer.

Set both `KARPWIKI_OIDC_ISSUER` and `KARPWIKI_OIDC_AUDIENCE` (`.env.example`'s OIDC section) to
swap in a real bearer-JWT `OidcAuthenticator` (`06` §3, Authlib-backed, `08` §2) — `default_
authenticator()` picks it automatically once both are set, with no handler code changing anywhere.
**SAML is not supported at all** — Authlib (the chosen library) has no SAML module; only OIDC.

If you deploy with `TrustedHeaderAuthenticator` anyway (e.g. behind an internal reverse proxy that
already does its own auth), the proxy terminating in front of the gateway **must** strip any
inbound `X-Karpwiki-User`/`X-Karpwiki-Groups` headers from untrusted clients before adding its own
— otherwise any caller can simply set those headers itself and impersonate anyone.

## 6. Secrets

Two independent secret paths, each with its own pluggable interface (`09` §13's "a role, not a
product" framing — Vault, AWS/GCP Secrets Manager, and Kubernetes Secrets are all named as equally
valid, not one prescribed backend):

- **`OPENAI_API_KEY`** — read directly by the OpenAI SDK/Pydantic AI, never touched by
  `src/karpwiki/config.py` itself. Inject it however your orchestrator delivers secrets to a
  container (a Kubernetes Secret mounted as an env var, your cloud provider's secret-injection
  mechanism, ...). Not needed at all for a role running on the `ollama:` provider (`09` §83) — a
  self-hosted model has no API key to manage or rotate.
- **Connector credentials** (`credential_ref` on a `Connector` row) — resolved through
  `secrets_manager.SecretResolver` at poll time, held only for that call's lifetime, never
  persisted or logged (`09` §13). The default `EnvSecretResolver` treats `credential_ref` as an
  environment variable name — genuinely production-viable, not a toy stand-in, and the most common
  way a Kubernetes Secret actually reaches a running process (injected as a pod-spec env var). A
  Vault/AWS/GCP-backed deployment implements `SecretResolver` and swaps it in via
  `default_secret_resolver()`, no change to `connector_polling.py`.

## 7. Scaling guidance

`06` §4's own table already covers this in the abstract; the concrete version for this codebase:

**Classification and curation are LLM-bound, not CPU-bound** — each `classify_source`/
`curate_source` task makes one real LLM call (`01` §1). Adding worker replicas beyond what your LLM
provider's own rate limits (RPM/TPM) can sustain doesn't increase throughput, it just produces more
throttled/retried calls. Size these two pools against your provider quota, not against available
CPU. Indexing and maintenance are cheap and CPU-bound by contrast — no LLM call in the common path
— so they scale on ordinary load/CPU signals.

**The optional search-result cache** (`KARPWIKI_CACHE_ENABLED`, step 76) reduces read load on the
Metadata DB for repeated identical searches — off by default, safe to enable once you have real
traffic worth caching; TTL-only invalidation (`KARPWIKI_CACHE_TTL_SECONDS`) means enabling it trades
a small, bounded staleness window for lower DB load, not a correctness risk (`09` §80).

**Rate limiting** (`KARPWIKI_RATE_LIMIT_*`, `.env.example`) protects the shared infrastructure from
a single noisy workspace or integration, not from real usage growth — tune the *_PER_WORKSPACE
values up as legitimate traffic grows, don't treat the shipped defaults as a hard ceiling.

**A large bulk import** (`POST /sources/bulk`, step 74) dispatches one real classification pipeline
per file in the batch — the same LLM-bound consideration above applies at N-times the volume for
one call. A very large batch is worth pacing at the client/script level (or via Celery's own
per-task `rate_limit=`) rather than assuming the classification queue will absorb an arbitrarily
large burst instantly.

## 8. Database migrations

`alembic upgrade head` reads `KARPWIKI_DATABASE_URL` directly (`migrations/env.py` imports it from
`karpwiki.config`) — set that env var (or have a `.env` alembic's own dotenv load will pick up) and
run it from anywhere with network access to the Metadata DB and this repo's `migrations/` directory
checked out; it does not need to run inside the deployed image. Run it once, before rolling out a
new image version that depends on the new schema — this repo's own live-verify discipline
(`09-implementation-notes.md`, throughout) always confirms a real `alembic upgrade head` succeeds
against a real database before trusting a migration, and a production rollout should follow the
same discipline: migrate first, then deploy the code that assumes the new schema.

## 9. First-time bootstrap

- **Object store bucket**: the dev stack's `minio-init` one-shot container creates the bucket
  automatically; a real S3/GCS/Azure Blob deployment has no equivalent step here — create the
  bucket (and any prefix/lifecycle policy you want) through your cloud provider directly before
  first start.
- **Schema**: `alembic upgrade head` (§8) creates every table from empty.
- **First workspace and admin**: `POST /workspaces` (whoever calls it becomes that workspace's own
  admin automatically, `06` §1) is the real entry point — there is no separate seed script or admin
  bootstrap CLI; the same REST API a normal admin uses is how the very first workspace gets
  created too. `POST /workspaces` itself normally requires admin in an *existing* workspace, which
  is impossible on an empty table — set `KARPWIKI_BOOTSTRAP_ADMIN` to the exact principal id (the
  `X-Karpwiki-User` value, or OIDC principal-claim value) that should be allowed to make that first
  call before this deployment's very first `POST /workspaces` (`09` §84/§86). The variable only
  matters while the workspace table is empty — leave it set afterward, or unset it; it has no
  further effect either way.

## 10. Health checks

`GET /healthz` — no auth, no DB touch, exempt from rate limiting (`06` §5, `phase2-tasklist.md`
step 49) — is the real liveness/readiness probe target for a load balancer or orchestrator, the
same one `docker-compose.yml`'s own gateway healthcheck already uses.

## 11. Backup and disaster recovery

Covered in full in [`backup-and-dr.md`](backup-and-dr.md) (`phase3-tasklist.md` step 77) — periodic
Metadata DB and object-store snapshots, a documented point-in-time restore procedure (including a
real, verified workspace-scoped restore technique), and the cross-store consistency caveat between
the two. Not duplicated here.

## 12. What this guide does not cover

Multi-region/DR topology (a second *active* region, not backup/restore) and data-residency pinning
(routing a specific workspace's storage to a specific region) are named in
[`07-additional-features-and-roadmap.md`](07-additional-features-and-roadmap.md) §2-3 as Phase 4
scope — "pursued based on actual organizational need, not a fixed timeline." No code in this repo
implements either yet, so this guide can't describe deploying them; a single-region deployment,
scaled per §7 above, is what the current implementation actually supports.

---
Previous: [08-implementation-stack.md](08-implementation-stack.md) · Back to: [00-overview.md](00-overview.md)
