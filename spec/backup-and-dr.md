# Backup & Disaster Recovery Procedures

`07` §3: "Periodic snapshots of the Metadata DB and object store per workspace; documented
point-in-time restore. Because workspaces are independently partitioned ([06](06-api-mcp-and-scaling.md)
§4), restore can be scoped to a single workspace." (`phase3-tasklist.md` step 77.)

**Scope, confirmed via `AskUserQuestion`**: documentation only, no new application code. Backup
and restore of a database and an object store are normally operated with the storage layer's own
tooling (`pg_dump`/managed-provider snapshots, S3 versioning/replication) and a runbook, not
reimplemented in application code — and this reference deployment has one shared Postgres database
(no real per-workspace physical partitioning to snapshot separately; `06` §4's "partitioning/
sharding" describes a *scaling* mechanism for a larger deployment, not this implementation's actual
topology). This document is the procedure `07` §3 calls for. It does not change any code.

This is deliberately lighter-weight than, and distinct from, Phase 4's full multi-region/DR
topology (`07` §6) — backup/restore procedure for a single deployment, not a second active region.

## 1. What needs backing up

Two independent stores, per `01` §1's architecture:

- **Metadata DB** (Postgres) — the system of record (`02` §3). Every table, all workspaces
  together in one database in this reference deployment.
- **Object Store** (S3-compatible; MinIO in the dev stack, `KARPWIKI_OBJECT_STORE_URL`) — raw
  sources, page-version diffs, and the wiki markdown export mirror (`02` §2), all namespaced under
  a real `/{workspace_id}/...` prefix per workspace.

**The wiki markdown export mirror is a *third*, already-existing recovery aid for wiki content
specifically** — `02` §2 frames it as "usable independent of the Platform's database," and step 57
built `wiki_export.export_workspace` as a from-DB-truth *regenerated projection*, not an archival
copy. It is not a substitute for a real Metadata DB backup (the mirror only ever reflects the
*current* version of each page, never history — `page_version` rows are the only place prior
versions live), but it means a lost-and-restored-from-an-older-snapshot Metadata DB can have its
*current* wiki content rebuilt for free by re-running `export_workspace` once service is back,
rather than needing the object-store snapshot to carry that specific content forward too.

## 2. Metadata DB — snapshot

Every table with a `workspace_id` column is listed below, split by whether that column is direct
or requires a join — real, not hand-waved, since a workspace-scoped restore (§4) depends on this
being complete:

**Direct `workspace_id` column**: `workspace`, `document_type`, `raw_source` (nullable — a source
`submitted` but not yet classified has none), `wiki_page`, `schema_version`, `review_item`
(nullable, same reason as `raw_source`), `ingestion_log`, `admin_action_log`, `lint_log`,
`storage_snapshot`, `page_index`, `access_policy`, `connector`.

**Joined via `wiki_page.workspace_id`** (no `workspace_id` column of their own): `page_version`,
`index_status`, `page_link` (either side — a link crossing workspaces belongs to neither cleanly;
see the caveat in §4), `query_feedback` (via `page_id -> wiki_page`).

**Not cleanly scopable to one workspace at all**: `query_log.resolved_workspaces` is an array — one
search call can span several workspaces. A workspace-scoped restore necessarily either includes
rows that also reference other workspaces, or excludes them; §4 documents this as an accepted
limitation rather than silently getting it wrong. `idempotency_record` is principal/endpoint-scoped,
not workspace-scoped, and holds only short-lived replay bodies — excluded from workspace-scoped
restore by design, not an oversight.

**Full-database snapshot** (the routine, periodic backup unit — real command against the dev stack;
substitute the real host/credentials for a deployed environment):

```bash
docker compose exec -T postgres pg_dump -U karpwiki -Fc karpwiki > karpwiki-$(date +%Y%m%d-%H%M%S).dump
```

`-Fc` (custom format) is what `pg_restore` below needs — a plain-SQL dump works for full restore
too, but only the custom format supports `pg_restore`'s selective-table restore used in §4.

**Cadence**: periodic, per `07` §3's own wording — a daily `pg_dump` plus continuous WAL archiving
(`archive_mode`/`archive_command`, or a managed provider's built-in point-in-time-recovery feature)
is the standard way to get real point-in-time granularity between daily snapshots; a bare daily
`pg_dump` alone only ever restores to the moment each dump was taken. Scheduling either is an
infrastructure/ops concern for the deployment, not application code — no new Celery task here,
consistent with `02` §5's own precedent that `query_log.purge_older_than` and similar maintenance
jobs are real functions with nothing scheduling them until a deployment decides to.

## 3. Object Store — snapshot

The dev stack's `mc` (MinIO Client) alias, already configured by the `minio-init` service:

```bash
docker compose exec -T minio-init mc mirror --overwrite local/karpwiki/ /backup/karpwiki-objectstore-$(date +%Y%m%d)/
```

A deployed environment backed by real S3 (or GCS/Azure Blob, per `08` §2's fsspec-backed choice)
should prefer the store's own **versioning + cross-region replication** over a periodic `sync`/
`mirror` job — point-in-time object recovery without a separate snapshot pipeline, and `objectstore.
py`'s own module docstring already notes objects are write-once by convention (raw sources, diffs)
except the wiki export mirror (deliberately overwritten on every write, `02` §2) — versioning
naturally captures every state of that one exception too, not just the write-once paths.

**Workspace-scoped snapshot** is a direct prefix copy, no filtering logic needed — every object
already lives under `/{workspace_id}/...` (`02` §2's own path scheme, confirmed throughout steps
57/74):

```bash
docker compose exec -T minio-init mc mirror --overwrite local/karpwiki/objectstore/<workspace_id>/ /backup/<workspace_id>-$(date +%Y%m%d)/
```

## 4. Point-in-time restore

**Full restore** (disaster recovery — the whole deployment is being rebuilt):

```bash
# Metadata DB
docker compose exec -T postgres pg_restore -U karpwiki -d karpwiki --clean --if-exists < karpwiki-<timestamp>.dump

# Object store
mc mirror --overwrite /backup/karpwiki-objectstore-<date>/ local/karpwiki/
```

Then confirm `alembic current` matches the schema the application code expects before starting the
Core Services/workers back up — a snapshot taken mid-migration, or restored against a newer
codebase than the one that wrote it, needs `alembic upgrade head` run first (the same discipline
this project already applies to every live-verify session in `09`).

**Workspace-scoped restore**, per `07` §3's own explicit callout that this should be possible
without touching the rest of the deployment:

1. **Object store**: `mc mirror --overwrite /backup/<workspace_id>-<date>/ local/karpwiki/objectstore/<workspace_id>/` —
   a direct prefix restore, no other workspace's objects are touched.
2. **Metadata DB**: `pg_restore`'s `--table` flag restores selected tables from a custom-format
   dump, but does not filter *rows* within a table — a workspace-scoped DB restore needs a
   row-filtered re-import, not a bare `pg_restore` invocation. The real technique: restore the
   dump into a **scratch database** (`pg_restore -d karpwiki_scratch`), then row-copy just the
   target workspace's rows back into the live database with `psql`'s `\copy`, once per table from
   §2's own list, direct-column tables filtered by `WHERE workspace_id = '<id>'` and joined tables
   filtered by joining `wiki_page` the same way §2 lists them. Illustrative for one direct-column
   table:

   ```sql
   \copy (SELECT * FROM wiki_page WHERE workspace_id = '<id>') TO 'wiki_page.csv' CSV HEADER
   -- against the live database, after deleting/reconciling any existing rows for <id>:
   \copy wiki_page FROM 'wiki_page.csv' CSV HEADER
   ```

   Restore tables in FK dependency order (`workspace` and `document_type` first; `page_version`/
   `index_status`/`page_link`/`query_feedback` last, since they reference `wiki_page`) — the same
   ordering discipline this project's own live-verify cleanups already follow in reverse (`09`
   §77's documented lesson about `psql -c` batching applies equally here: run each table's restore
   as its own statement, verify, don't assume a bundled script's per-statement success implies a
   committed result).
3. **`page_link` rows crossing the restored workspace's boundary** (§2's "not cleanly scopable"
   note): a link from a page in the restored workspace to a page in an *unrestored* workspace, or
   vice versa, may end up stale relative to whichever side wasn't touched. Accepted, not silently
   wrong: `page_links.sync` (`02` §3) re-derives these rows from the current markdown on the next
   write to either page, and `api._resolve_page_links`'s read-time AuthZ re-check (`01` §3, step
   63) means a stale link is never served to an unauthorized caller even in the meantime — it is
   simply omitted or resolved against whichever content is actually current on each side.
4. **`query_log` rows spanning the restored workspace and others** (§2's array-column note):
   accepted as included in a workspace-scoped export by "if this workspace is anywhere in
   `resolved_workspaces`," not further split — splitting a single historical search event across
   two restore operations has no clean, correct answer, and `query_log` is analytics/audit data,
   not data the running application depends on for correctness.

## 5. A real cross-store consistency caveat, documented rather than silently assumed away

The Metadata DB and object store are **not restored atomically together** — no cross-store
transaction exists between them today (`02` §8's own Consistency Model already names the Metadata
DB as "the commit point," with the object store's wiki mirror "not required to be transactional
with the Metadata DB write"). A DB snapshot and an object-store snapshot taken even a few minutes
apart, then both restored, can leave the restored state referencing an object key that did not yet
exist at DB-snapshot time (a page created after the DB snapshot but before the object-store
snapshot) or omitting one that did (the reverse ordering). Practical mitigation: take the object
store snapshot **immediately after** the DB snapshot, never before — a page write's DB commit
always precedes its mirror write (`wiki_export.write`'s own "compute-on-write, non-transactional"
ordering), so a DB snapshot is never *ahead* of the mirror that was live at the same moment, only
possibly behind it; restoring both then re-running `wiki_export.export_workspace` (§1) as a final
repair pass — its own documented purpose — closes whatever gap remains for wiki content
specifically. Raw sources and diffs have no such repair pass (nothing regenerates them from DB
state), so this ordering discipline matters most for anything not yet reflected in the wiki mirror
at snapshot time.

---
Previous: [08-implementation-stack.md](08-implementation-stack.md) · Back to: [00-overview.md](00-overview.md)
