# Admin Guide

How to actually operate a karpwiki deployment day to day: setting up a workspace, resolving the
review queue, managing connectors and access, and reading the operational dashboards. `00` §1
scopes this repo to "admin console scope, not pixel-level UI design" — there is no admin UI, so
every task here is a real REST call (curl examples throughout). Most of this guide is REST-only by
design — workspace/schema/access-policy/connector management and the `/metrics/*`/`/analytics/*`
dashboards have no MCP counterpart, matching this codebase's consistent "admin governance
configuration, not an agent-facing operation" scoping. The review queue (§2) and version history
(§5) are the exception — both have real MCP tools, noted inline. For running the platform itself,
see [`deployment-guide.md`](deployment-guide.md); for backup/restore, see
[`backup-and-dr.md`](backup-and-dr.md) — neither is repeated here.

Every example below assumes an authenticated admin request. In the dev stack that means a header
(`-H "X-Karpwiki-User: <you>"`); in a real deployment it's a bearer JWT once OIDC is configured
(`deployment-guide.md` §5).

## 1. Creating and configuring a workspace

**Create it.** Whoever calls this becomes that workspace's own admin automatically (`06` §1) —
there's no separate bootstrap step:

```bash
curl -X POST -H "X-Karpwiki-User: you" -H "Content-Type: application/json" \
  -d '{"workspace_id": "eng-docs", "name": "Engineering Docs"}' \
  http://localhost:8080/workspaces
```

**Give it a taxonomy.** A document type routes content during classification (`03` §3) — create
one per category you want the Classifier to be able to choose:

```bash
curl -X POST -H "X-Karpwiki-User: you" -H "Content-Type: application/json" \
  -d '{"type_code": "eng.runbook", "workspace_id": "eng-docs", "description": "Operational runbooks"}' \
  http://localhost:8080/document-types
```

`POST /document-types/{type_code}` updates it (including reassigning it to a different workspace —
that needs admin in *both*); `DELETE /document-types/{type_code}` removes it.

**Give it a real `SCHEMA.md`.** New workspaces start with a placeholder — nothing has to be
"none configured yet" for long. Either write real content directly (`content` is a YAML document,
not markdown — the required top-level `workspace_id` field must match the URL):

```bash
curl -X POST -H "X-Karpwiki-User: you" -H "Content-Type: application/json" \
  -d '{"content": "workspace_id: eng-docs\ningestion_policy: auto\n", "change_summary": "initial schema"}' \
  http://localhost:8080/workspaces/eng-docs/schema
```

...or bootstrap from a predefined template (`phase3-tasklist.md` step 75 — currently `policy` and
`engineering-docs`, each pre-tuned for that kind of content: `GET /workspace-templates` lists what's
available, `GET /workspace-templates/{name}?workspace_id=eng-docs` returns ready-to-POST content
with `workspace_id` already filled in):

```bash
curl -H "X-Karpwiki-User: you" \
  "http://localhost:8080/workspace-templates/engineering-docs?workspace_id=eng-docs" \
  | jq -r .content > schema.yaml
curl -X POST -H "X-Karpwiki-User: you" -H "Content-Type: application/json" \
  -d "{\"content\": $(jq -Rs . < schema.yaml)}" \
  http://localhost:8080/workspaces/eng-docs/schema
```

Every write is a real, versioned history entry — `GET /workspaces/{id}/schema/versions` lists them,
`POST /workspaces/{id}/schema/rollback` (body: `{"target_version_id": "..."}`) restores an earlier
one as a new version (never edits history in place — `01` §7: "auditable and reversible").

**Grant access.** `Role` is `reader` | `contributor` | `admin`, workspace-wide by default:

```bash
curl -X POST -H "X-Karpwiki-User: you" -H "Content-Type: application/json" \
  -d '{"principal": "casey", "role": "reader"}' \
  http://localhost:8080/workspaces/eng-docs/access-policy
```

Add `"page_type": "entity"` to scope a grant to just that page type instead of the whole workspace
— the moment ANY scoped grant exists for a page type in a workspace, that type becomes restricted
for *everyone*: a plain workspace-wide role alone is no longer enough for it, only admin (who always
bypasses) or a principal's own matching scoped grant (`07` §2, step 70). Add `"fuse_access": true`
to also grant read-only FUSE-mount access to that workspace's wiki export (`09` §12, step 58) —
orthogonal to `role`, and the one feature that needs a kernel FUSE driver on whatever host actually
runs the mount (`deployment-guide.md` §2). `GET /workspaces/{id}/access-policy` lists current
grants; `DELETE /workspaces/{id}/access-policy/{principal}` revokes (add `?page_type=...` to revoke
only a scoped grant, leaving the workspace-wide one — if any — untouched).

## 2. The review queue

This is the actual day-to-day work. `GET /review-items?workspace_id=...&kind=...` lists open items
(both filters optional — a `submission`/`classification` item has no `workspace_id` yet, so it only
shows up in the unfiltered or `kind`-filtered view). Every item resolves the same way:
`POST /review-items/{id}/resolve` with `{"action": "...", "note": "..."}` (`note` optional). What
`action` means depends on the item's `kind` — this table is the real, complete reference. Both
calls have MCP equivalents (`wiki_list_review_items`, `wiki_resolve_review_item`) with the same
parameters, for an agent operating the queue directly.

| Kind | What it means | Valid `action` values |
|---|---|---|
| `submission` | Informational — every submission gets one, unconditionally (`03` §5) | `acknowledge` |
| `classification` | The Classifier's confidence was below threshold, or misrouted | the correct document type code (e.g. `"eng.runbook"`) |
| `duplicate` | A near-duplicate was found at ingest time, or two already-published pages match later | `reject`, `keep_both`, `supersede`, `merge` |
| `pii_review` | The regex scanner (step 71) found SSN/credit-card/credential/secret-key-shaped content | `acknowledge` (proceeds anyway — logged, per-source, never re-blocks the same content again), `reject` |
| `reindex` | The Maintenance Advisor flagged pages as stale, superseded-cited, or low-feedback (`05` §3) | `reindex now`, `dismiss` |
| `prune` (reason: `superseded_source_retention`) | A source has stayed superseded past the retention window | `delete superseded source`, `dismiss` |
| `prune` (reason: `orphaned`) | A page has no inbound links and no recent search traffic | `archive page`, `dismiss` |
| `prune` (reason: `contradicted_by`) | The Contradiction Detector found two pages disagreeing | `archive page`, `dismiss` |
| `stuck` | A source has sat mid-pipeline past the crash-recovery window (step 64) | `retry`, `abort`, `dismiss` |

A `classification` resolution needs admin in *the workspace the chosen document type belongs to*,
not just the item's own (often absent) workspace — you're choosing which workspace this document
lands in. Everything else that follows a resolution (re-dispatching classification, curation,
reindexing) happens automatically; you never manually trigger those steps.

```bash
curl -X POST -H "X-Karpwiki-User: you" -H "Content-Type: application/json" \
  -d '{"action": "eng.runbook"}' \
  http://localhost:8080/review-items/<review_id>/resolve
```

## 3. Connectors

A connector polls an external source system on a schedule and submits what it finds the same way a
manual upload would (`03` §2). The one real adapter today is `"git"`:

```bash
curl -X POST -H "X-Karpwiki-User: you" -H "Content-Type: application/json" \
  -d '{
    "workspace_id": "eng-docs",
    "type": "git",
    "config": {"repo_url": "https://github.com/your-org/runbooks", "branch": "main"},
    "credential_ref": "GIT_MAIN_TOKEN",
    "schedule": {"interval_minutes": 30},
    "ingestion_policy": "auto"
  }' \
  http://localhost:8080/connectors
```

`credential_ref` is a *pointer*, never the real secret — it names an environment variable the
worker process resolves at poll time (`deployment-guide.md` §6 covers swapping in a real secrets
backend). `ingestion_policy` (`"auto"` or `"gated"`) may only *tighten* the workspace's own policy,
never relax it (`09` §13) — a workspace set to `gated` stays gated for this connector even if you
set `"auto"` here. An auth failure disables the connector automatically (`state` becomes
`disabled_auth`, distinct from an admin-initiated `disabled`) and fires a real notification if
delivery is configured (`deployment-guide.md`'s webhook note) — re-enable it via
`POST /connectors/{id}` (`{"state": "enabled"}`) once the credential is fixed.

## 4. Bulk import and export

**Seeding a new workspace from an existing document repository**: `POST /sources/bulk` (multipart,
one `files` field per document) dispatches a real, independent classification pipeline per file in
one call — admin-gated, since it's a higher-leverage entry point than one document at a time. Each
file still goes through the exact same real classification/dedup/curation pipeline a manual
submission does; nothing here is a shortcut around review.

```bash
curl -X POST -H "X-Karpwiki-User: you" \
  -F "files=@runbook1.md" -F "files=@runbook2.md" \
  http://localhost:8080/sources/bulk
```

**Exporting a workspace** (migration, backup, or just handing someone a snapshot):
`GET /workspaces/{id}/export` streams a real `tar.gz` of everything under that workspace's object-
store prefix — the wiki markdown mirror, raw sources, and page-version diffs together:

```bash
curl -H "X-Karpwiki-User: you" http://localhost:8080/workspaces/eng-docs/export -o eng-docs.tar.gz
```

This is a point-in-time snapshot, not a backup *procedure* — see `backup-and-dr.md` for periodic,
scheduled backups.

## 5. Version history and rollback

Every page keeps its full version history (`01` §5): `GET /pages/{id}/versions` lists it,
`GET /pages/{id}/versions/{version_id}` fetches one, `GET /pages/{id}/versions/diff?from_version_id=
...&to_version_id=...` shows the diff between any two. Restoring a prior version is a real,
audited action — it writes a *new* version with the old content, never edits history in place:

```bash
curl -X POST -H "X-Karpwiki-User: you" -H "Content-Type: application/json" \
  -d '{"target_version_id": "<version_id>", "change_summary": "revert bad edit"}' \
  http://localhost:8080/pages/<page_id>/rollback
```

MCP equivalents: `wiki_get_page_versions(page_id, ...)` and `wiki_rollback_page(page_id,
target_version_id, change_summary=None)`.

## 6. Moving content between workspaces

If a document type's target workspace changes, the pages/sources already routed under the old one
don't move themselves — `POST /workspaces/{id}/bulk-move` does, in admin-supervised batches (`05`
§7). Always preview first:

```bash
curl -X POST -H "X-Karpwiki-User: you" -H "Content-Type: application/json" \
  -d '{"target_workspace_id": "policies", "page_ids": ["<id1>", "<id2>"]}' \
  http://localhost:8080/workspaces/eng-docs/bulk-move/preview
```

then the same body against `.../bulk-move` (no `/preview`) to actually execute it. Needs admin in
*both* workspaces — the one you're draining and the one you're filling.

## 7. Reading the operational dashboards

All admin-gated, all backend data (`00` §1's own scope note — no UI renders these, a real
dashboard would consume them):

| Endpoint | What it tells you |
|---|---|
| `GET /metrics/index-health` | `index_status` distribution per workspace/type; jobs stuck beyond threshold |
| `GET /metrics/ingestion-pipeline` | Queue depth, review-SLA breaches, error rate/throughput |
| `GET /metrics/search-performance` | p50/p95 search latency, cache hit rate (if the cache is enabled — `deployment-guide.md` §7) |
| `GET /metrics/storage-utilization` | Object store/DB/FTS-index size, with a real trend history |
| `GET /metrics/review-queue-health` | Open item counts and age, by kind and workspace |
| `GET /analytics/usage-trends` | Search/submission/feedback volume over time, plus active-workspace counts |

Each accepts an optional `?workspace_id=` to scope to one workspace instead of every workspace you
administer.

---
Previous: [08-implementation-stack.md](08-implementation-stack.md) · Back to: [00-overview.md](00-overview.md)
