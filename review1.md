# Specification Review — Completeness, Clarity & Tech Stack

Review of `spec/00` through `spec/07` (1,552 lines). Overall the spec is thorough and internally
consistent — no broken cross-references, and the requirements-traceability table (00 §7) holds up
against the actual section numbers. The items below are gaps/ambiguities worth a decision before
implementation planning.

## 1. Completeness Gaps

**Status (2026-06-15): items 2–5 resolved via edits to `spec/01`–`03`, `07`. Items 1, 6, 7
deferred as enhancements — not yet scheduled.**

1. **Connectors are under-specified relative to how often they're referenced.** They're a
   first-class submission path (03 §2: "Connectors... submit it the same way") and configured in
   Repository Management (05 §7: schedule, credentials, ingestion policy), but there's no section
   describing *how* a connector run executes — which worker pool (01 §1's Async Layer doesn't list
   one for connector polling), and how "discovers new/changed content" is detected (polling vs.
   webhook vs. diff-against-last-sync). Connectors are also **absent from the roadmap** (07 §6) —
   no phase calls out a connector framework as a deliverable.

   **Status: Deferred (enhancement).**

2. **Dedup at `duplicate_check` may compare apples to oranges for `structured_data` sources.** The
   Classifier produces "a short summary" for *every* source during classification (03 §3 step 1),
   and that summary is what's run as the near-duplicate similarity query (03 §4) against existing
   wiki pages. But for `structured_data` sources, existing pages are indexed by their **intent
   statement** (07 §1.1) — a different kind of text than a generic summary of raw schema/config
   bytes — and the intent statement isn't produced until the Curator Agent runs at ingest time (07
   §1.3 step 2, *after* `duplicate_check`). Worth deciding: should the Classifier produce an
   intent-statement-shaped summary for `structured_data` sources specifically, so dedup compares
   like-with-like?

   **✅ Resolved.** Rather than reshaping the Classifier's summary, the Classifier now extracts
   `artifact_identity`/`source_version`/`source_modified_at` for `structured_data` sources (03 §3,
   new step 4; fields added to `raw_source`, 02 §3). `duplicate_check` (03 §4) gained a new branch:
   same `artifact_identity` with an older `source_version`/`source_modified_at` → `kind=duplicate`,
   `severity=low`, `proposed_action=supersede` pre-filled. Admin confirmation applies the existing
   `supersede` resolution, updating the existing `source`/`entity` pages in place via new
   `page_version`s (01 §5). The residual cross-artifact near-duplicate case (different identity,
   merely similar) is left on today's generic-summary lexical check — accepted as out of scope for
   now.

3. **`workspace.status = read_only`** (01 §3 workspace record) is introduced but never
   differentiated from `archived`. 01 §3's lifecycle section defines `archived` as "read-only,
   excluded from default search/ingestion routing but still queryable" — which sounds identical to
   what `read_only` would mean. Either `read_only` needs its own distinct trigger/meaning (e.g.,
   "temporarily read-only during migration, still in default search") or it should collapse into
   `archived`.

   **✅ Resolved.** Removed — `workspace.status` is now `active | archived` (01 §3).

4. **`raw_source.submitted_by`** is referenced in prose (03 §2: `submitted_by =
   connector:<connector_id>`) but missing from the conceptual field list for the `raw_source`
   table in 02 §3. Worth adding for consistency — it's the field that distinguishes end-user vs.
   connector submissions.

   **✅ Resolved.** Added `submitted_by` (`user:<id> | connector:<connector_id>`) to the
   `raw_source` field table (02 §3), alongside the `artifact_identity`/`source_version`/
   `source_modified_at` fields from item 2.

5. **`review_item.status` values aren't enumerated.** 02 §3 lists `status` as a field; 03 §5
   mentions `status=open` and a default disposition of "acknowledge." A full enum (`open`,
   `resolved`, `dismissed`, ...?) would help 05 §1's "filterable by ... status" and 05 §8's "open
   item counts and age" dashboards.

   **✅ Resolved.** `review_item.status` is now `open | resolved`; the per-kind resolution action
   is recorded in a new `resolved_action` field (`acknowledge`, `merge`, `reject`, `supersede`,
   `keep_both`, etc.). Also added the previously-missing `severity` field to the `review_item`
   table (02 §3), since 03 §4 and 05 §1 already referenced it. 03 §5 updated to use
   `resolved_action=acknowledge` terminology.

6. **MCP delegation model for `wiki_submit`** isn't detailed. 06 §2 says it lets "an agent submit
   a document on a user's behalf" with `contributor` role — but 06 §3's auth table only covers
   direct principal types (end user via SSO, API/MCP client via API key/OAuth client-credentials).
   On-behalf-of delegation (agent acting as a specific end user vs. as its own service principal)
   is a distinct auth pattern not addressed. This matters for attribution (`author: user:<id>` vs
   `system:` in the versioning model, 01 §5) and for the `submission` review item's "who submitted
   this."

   **Status: Deferred (enhancement — Phase 2, Full API+MCP surface).**

7. **Classifier confidence score** — 03 §3 and 03 §1 gate routing/review on "confidence ≥ the
   workspace's configured threshold." For an LLM-based classifier, *how* this confidence number is
   produced/calibrated (self-reported by the LLM? a secondary scoring pass? log-prob based?) isn't
   addressed. This is more an implementation question than a spec gap, but the whole
   low-confidence review path depends on this number being meaningful — worth at least a one-line
   note on the intended approach.

   **Status: Deferred (enhancement — implementation detail, similar to the NFR placeholders in
   06 §6).**

## 2. Clarity / Minor Issues

- **04 §3 vs. §4 soft tension**: §3 states "there's only one retrieval path (lexical)... no fusion
  of heterogeneous signal types — and so none of the score-comparability problems fusion exists to
  solve." §4 then describes a score-normalization/merge step when a dedicated index instance is
  involved. Not contradictory (different scopes — single index vs. federated with dedicated
  indexes), but §3's blanket framing could use a forward-pointer to §4's caveat so a reader doesn't
  think the dedicated-index case was overlooked.

- **`SCHEMA.md` has no example template.** 01 §7 describes its contents conceptually (taxonomy
  slice, page conventions, curator rules, thresholds, dedup sensitivity) but there's no sample
  document, unlike the required-frontmatter YAML block in 01 §6. Useful for an implementer, not
  strictly required.

- **`diff_ref` format** (01 §5, 02 §3) is left unspecified — reasonable for a vendor-neutral spec,
  but worth flagging since it has implementation implications (stored diff blob vs. computed on
  read).

## 3. External Pattern References — Flagged Per Your Instruction

**✅ Resolved (2026-06-15).** All 8 locations below were reworded to vendor-neutral rationale —
the underlying design reasoning (async pipeline states, resolve-then-retrieve MCP tools,
popularity-tiered scheduling, thin MCP adapter, argument normalization, footnote citations,
connector source types, cross-workspace reference graph) is preserved without naming external
products. Karpathy references were left untouched — that's the spec's named foundational pattern
(`00` §1 Purpose), a different category from these precedent-citations. Verified: no remaining
`context7|gbrain|wiki-r2|llmwiki-research` matches across `spec/*.md`.

Original findings, for reference — the **spec documents themselves** cited an external product
("Context7") and two environment-tooling conventions (`wiki-r2` / `llmwiki-research`) as design
inspiration in several places:

| Location | Reference |
|---|---|
| `00-overview.md` §2, Principle 2 | `[[wiki-r2]]`/`[[llmwiki-research]]` tool conventions |
| `00-overview.md` §2, Principle 6 | "modeled after Context7's observed lifecycle" |
| `00-overview.md` §2, Principle 10 | "the resolve→retrieve convention seen in Context7's MCP tools" |
| `01-architecture-and-data-model.md` §6 | "conventions already used by the `llmwiki-research`/`wiki-r2` tooling" |
| `03-ingestion-and-review-workflows.md` §2 | "the source types Context7 supports are a useful reference list" |
| `05-admin-backend-and-maintenance.md` §2 | "adapted from Context7's popularity-tiered refresh model" |
| `06-api-mcp-and-scaling.md` §2 | "mirroring how Context7's MCP package wraps its hosted API" (x2), "as with Context7, the MCP layer should tolerate near-miss parameter..." |
| `07-additional-features-and-roadmap.md` §4 | "per the `wiki-r2`/`llmwiki-research` conventions observed in this environment" |

I haven't edited these — they're load-bearing rationale in a few places (e.g., the ingestion
lifecycle states in 00 §2 Principle 6 are directly tied to "Context7's observed lifecycle"). If you
want these reworded to stand on their own (vendor-neutral rationale instead of "modeled after
X"), that's a separate editing pass — let me know and I can do it section by section.

## 4. Technology Stack — Confirmed Gap

I grepped all 8 spec docs for `python|fastapi|django|flask|pydantic|sqlalchemy|celery|uvicorn` —
**zero matches**. This isn't an oversight: `00-overview.md` §3 explicitly puts "Specific
vendor/product selection (storage, search, LLM provider)" **out of scope**, describing it as the
"agreed vendor-neutral, logical components approach." Every storage role in 02 §1 lists "example
technologies" generically, and 06 doesn't mandate REST vs. GraphQL or any server framework. So the
spec is consistently vendor- *and* language-neutral — there's currently no place where a "Python
stack" decision would even be recorded.

If a Python implementation is now a real decision, the choices that map onto the spec's roles are:

| Spec role | Python-stack decisions needed |
|---|---|
| Common Gateway / API (06 §1) | Web framework — e.g. FastAPI fits the REST + async + OpenAPI shape implied |
| Async Layer (01 §1, 06 §4) | Job queue — Celery / RQ / Dramatiq / arq (asyncio-native) |
| Metadata DB (02 §3) | Driver/ORM — SQLAlchemy + Alembic, or raw driver |
| Full-Text Index (02 §4) | Given "no vector index" is a core principle: Postgres FTS (no extra service, same DB as Metadata DB) vs. a dedicated search engine + its Python client |
| Object Store (02 §2) | S3-compatible client library |
| MCP Surface (06 §2) | Python MCP server SDK — needs to support both `stdio` and streamable HTTP per the spec's requirement |
| LLM Layer — Curator/Classifier (01 §1) | LLM provider SDK |
| Auth (06 §3) | OIDC/SAML library for enterprise SSO, JWT validation for API keys |

## 5. Open Questions For You

1. ~~**External references (§3 above)** — leave as-is, or reword to vendor-neutral rationale?~~ —
   Answered: reworded across all 8 locations (see §3).
2. ~~**Tech stack (§4)** — capture as a new `08-implementation-stack.md` mapping each vendor-neutral
   role to a specific Python choice (keeping 00–07 vendor-neutral with a "reference implementation"
   pointer), or inline notes in the relevant sections, or defer until we talk through the choices?~~
   — Answered: new `08-implementation-stack.md` added. Choices: FastAPI, Celery+Redis, PostgreSQL
   (SQLAlchemy 2.0 async + Alembic), PostgreSQL FTS + OpenSearch (dedicated workspaces), fsspec
   (s3fs/gcsfs/adlfs/local), official `mcp` SDK, Pydantic AI, Authlib + PyJWT.
3. ~~Any of the completeness gaps in §1 you want addressed now vs. logged for later?~~ — Answered:
   items 2–5 resolved, items 1/6/7 deferred (see §1 status markers).

## 6. External Review (`review2.md`) — Findings and Resolutions

A second reviewer's pass (`review2.md`, 2026-06-15) focused on contract-level consistency
(enums, transitions, scoring/merge semantics). All actionable findings resolved same day:

| Finding | Resolution |
|---|---|
| #1 `review_item.kind` missing `classification` | **✅ Resolved.** Added to the enum (02 §3) — `submission\|classification\|duplicate\|reindex\|prune`. |
| #2 Page `status` vs. ingestion placeholder labels (`processing`/"awaiting review"/etc.) | **✅ Resolved.** 03 §1 now notes these UI labels are distinct from frontmatter `status`, which stays `draft` until `ingested` (Curator sets `published`). |
| #3 Dedicated-index score merge — tie-break unspecified | **✅ Resolved.** 04 §4 now specifies stable sort by normalized score desc, then `workspace_id`, then `page_id`. Min-max normalization itself was already specified — the reviewer's broader ask for a full "normative merge contract" appendix was declined as over-specification relative to 00 §3's vendor-neutral scope. |
| #4 `02` §8 consistency-model wording didn't match §7's state diagram ordering | **✅ Resolved.** Reworded to: write immediately marks `index_status` `stale`/`pending`, serving previous version until reindexed. |
| "Missing details" item 4: cross-workspace links into archived/deleted workspaces | **✅ Resolved.** 01 §3 now notes links into `archived` workspaces keep resolving (still queryable); deletion-time cleanup is part of that admin action. |
| "Missing details" item 5b: legal hold vs. pruning/erasure | **✅ Resolved.** Added a **Legal hold** row to 07 §2, exempting flagged content from `prune` and the erasure workflow. |

**Declined / not new scope:**
- "Canonical enums and transitions" (item 1) — fully covered by #1/#2 above plus the existing 03 §1 state machine.
- API contract details — idempotency/pagination/error schema/rate-limit headers (item 2) — explicitly out of scope per 00 §3 ("exact API request/response schemas").
- Connector credential storage/rotation (item 3) — bundled with the already-deferred connector execution model (§1 item 1 above).
- Retention/query-log privacy specifics (item 5) — already deliberately deferred to `SCHEMA.md`/org policy, same pattern as the 06 §6 NFR placeholders.
- Taxonomy bulk-move safeguards (item 6) — mostly covered by 05 §7 + the versioning model's rollback; remaining detail is implementation-level.
- Retrieval quality governance / relevance test sets (item 7) — operational process for the org, analogous to the 06 §6 NFR placeholder ("TBD by org").
