# Implementation Audit — Phase 1 & 2 (2026-08-19)

Requested directly: review the Phase 1/2 implementation against the specs and tasklists;
document missing items, deviations, redundancies, and dead code; missing items become planned
tasks, everything else gets documented for further action rather than fixed silently or
immediately. This file is the record of that review. It does not change any code.

Two passes, run separately since they check different things:

1. **Spec-vs-tasklist pass** (2026-08-19, earlier the same day): does what `phase1-tasklist.md`/
   `phase2-tasklist.md` claim as "Done" actually satisfy what specs `00`–`09` require? This pass
   found the gaps now in [phase3-tasklist.md](phase3-tasklist.md) track 3a steps 57–65 (real wiki
   markdown export, FUSE-mount access, real `SCHEMA.md` storage, the real `index.md` catalog page,
   structured-data curation, search partial-failure handling, read-time link resolution, a
   stuck-pipeline detector, and the global-admin grant question) — see that file for the full
   write-up of each; not repeated here.
2. **Code-vs-spec and code-health pass** (this file's own findings, below): two parallel audits —
   one re-verifying the actual source code against specs `01`–`09` directly (not the tasklists'
   own self-description of themselves), and one sweeping `src/karpwiki/` for dead code,
   unused/orphaned artifacts, and redundant logic.

## 1. Missing items

One new item found by this pass, beyond what track 3a already covers — added as
[phase3-tasklist.md](phase3-tasklist.md) **step 66**:

**API pagination-contract gap.** `09` §14 states cursor pagination
(`{"items": [...], "next_cursor": <string|null>}`, `limit`/`cursor` params) as the contract for
list endpoints, and `GET /search`'s own lack of it is explicitly flagged as a deliberate,
documented gap (`09` §28). Four other list endpoints have the identical gap but were never
flagged anywhere:

| Endpoint | File:line |
|---|---|
| `GET /document-types` | `api.py:907` |
| `GET /connectors` | `api.py:1010` |
| `GET /workspaces` | `api.py:1100` |
| `GET /workspaces/{id}/access-policy` | `api.py:1193` |

Each returns only `{"items": [...]}` — no `next_cursor`, no `limit` param, no way to page past
whatever `list_for_workspace`/`list_for_workspaces`/etc. returns. Low real-world severity today
(these lists are naturally small and bounded — a deployment's own workspace/connector/grant
counts — not append-heavy like `page_version`/`raw_source`), but the *written* contract is
silently narrower than what four endpoints actually implement, the same shape of gap `/search`'s
own already gets called out for. See step 66 for the two ways to close it (build real pagination,
or explicitly document these four as deliberately unpaginated).

No other missing items found beyond what track 3a already tracks. The code-vs-spec pass sampled
role-gating across `/pages`, `/pages/{id}/versions`, `/pages/{id}/rollback`, review-item
resolution, document-types, and connectors against `06` §1's caller table, and `Idempotency-Key`
coverage against `09` §14 — all consistent with spec, no gaps found beyond the pagination one
above.

## 2. Deviations and discrepancies

**None found beyond the pagination gap in §1 above.** The code-vs-spec pass specifically looked
for code that does something materially different from what a spec document says *without* that
difference being documented as a deliberate decision somewhere in `09-implementation-notes.md`'s
numbered sections — and for "Done" claims in `phase1-tasklist.md`/`phase2-tasklist.md` that no
longer match what the current code actually does (drift from a later step's refactor that an
earlier step's note was never updated to reflect). Checked especially the cross-cutting
concerns most exposed to this kind of drift over 56 steps: the error envelope shape (`ApiError`/
`_envelope`), the `X-Request-Id` header, `ReviewKind`'s five kinds, and idempotency handling —
all still match their originating decision-log text exactly. Every other place where the code
does something narrower or different from a spec's literal wording turned out to already have a
"Spec touch-point" or an "Alternative considered" paragraph in `09-implementation-notes.md`
explaining why — this project's own discipline of writing that down at the time it was decided,
rather than leaving it implicit, held up under a second, independent check.

## 3. Redundancies (code-level, minor)

Two small, low-risk findings — neither breaks anything, both are worth a cleanup pass whenever
someone is already touching the relevant file, not urgent enough to justify a Phase 3 roadmap
step of their own.

**`enforce_rate_limit`'s 429 response hand-builds the error envelope instead of reusing
`_envelope`/`ApiError`.** `api.py`'s rate-limit middleware (around line 196–206) constructs
`{"error": {"type": "rate_limited", ...}}` directly as a `JSONResponse`, duplicating the exact
shape `_envelope(request, exc)` (line 99) already produces for every route-handler-raised
`ApiError`. This is middleware, not a route handler, so it can't simply `raise ApiError` and let
FastAPI's `@app.exception_handler(ApiError)` catch it (line 224) — but `_envelope` itself is a
plain function taking `(request, exc)`, not tied to that dispatch mechanism, so
`enforce_rate_limit` could call `_envelope(request, ApiError(429, "rate_limited", "Rate limit
exceeded."))` directly and then attach the rate-limit-specific headers on top, instead of
re-declaring the same dict shape a second time. Cosmetic, not a bug — the two shapes are
currently identical — but a future change to the error envelope's shape would need to remember to
update both places instead of one.

## 4. Dead / unused code (code-level, minor)

An unusually clean result for a codebase this size (~9,000 lines, 35 modules) — the dead-code
sweep found **zero** fully-unreferenced functions, classes, or constants, **zero** orphaned
imports, and **zero** genuine copy-pasted-logic duplication worth consolidating (same-named
functions across modules — `create`/`search`/`reindex`/`history` etc. — are all legitimate
per-module CRUD conventions or task-wrapper/orchestration pairs, not duplication; pagination
helpers are already centralized in `pagination.py` and reused everywhere). Two model fields are
write-only, both low-severity and both plausibly intentional rather than dead:

- **`PageVersion.diff_ref`** (`models.py:255`) — written on every version write
  (`versioning.py:141,151`) but never read back anywhere; the diff *view* recomputes from
  `content` directly instead (`versioning.py:355`'s own comment confirms this). `02` §3 names the
  field/file as the archival provenance record, which is a legitimate reason for it to stay
  write-only forever — but nothing currently confirms that's the intent versus an oversight.
  Worth a one-line docstring note either way, whenever someone is next in `versioning.py`.
- **`IndexStatus.last_content_version`** (`models.py:286`) — written once (`search.py:106`),
  asserted in one test (`test_search.py:47`), but no application code (API, dashboard, or
  detector) ever reads it back. A real, if minor, candidate for either removal or for wiring into
  a real consumer (e.g. the index-health dashboard's "stuck" detection, which currently reuses a
  different missing-timestamp proxy per `09` §39/§47).

Neither needs a Phase 3 roadmap step — they're small enough to resolve as a drive-by fix whenever
`versioning.py`/`search.py`/`monitoring.py` is next being worked on, not scheduled work of their
own.

## Method

Two parallel fork agents ran independently (same session, same full context of this project's
history and conventions): one AST-extracted every module-level function/class/constant in
`src/karpwiki/` (385 names) and grepped each for real call sites across `src/` and `tests/`, ran
`pyflakes` for import checks, and checked every `config.py` constant and a sample of `models.py`
columns for real read-sites; the other read `01`–`09` and `09-implementation-notes.md` directly
against the current source for `api.py`'s auth-gating, pagination, error-envelope, and
idempotency behavior. Every finding reported here was independently re-verified by direct
`grep`/`Read` before being written up — nothing in this document is taken on a subagent's word
alone.

---
Previous: [phase3-tasklist.md](phase3-tasklist.md) · Back to: [00-overview.md](00-overview.md)
