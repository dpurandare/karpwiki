# Contributor & Reader Guide

How to submit documents and search the wiki as a regular user — no admin role needed for anything
in this guide (submitting needs `contributor` in at least one workspace; searching and reading only
need `reader`). `00` §1 scopes this repo to backend/API only, so every task here is a real REST
call (curl examples throughout). If you're integrating an AI agent instead of calling the API
directly, see [`user-guide-agents.md`](user-guide-agents.md) — it covers the same operations over
MCP.

Every example assumes an authenticated request. In the dev stack that's a header
(`-H "X-Karpwiki-User: you"`); in a real deployment it's a bearer JWT once OIDC is configured.

## 1. Submitting a document

`POST /sources` accepts exactly one of three input modes — a file upload, pasted text, or a URL:

```bash
# A file
curl -X POST -H "X-Karpwiki-User: you" -F "file=@runbook.md" http://localhost:8080/sources

# Pasted text
curl -X POST -H "X-Karpwiki-User: you" -d "text=Drain the queue, then restart." http://localhost:8080/sources

# A URL
curl -X POST -H "X-Karpwiki-User: you" -d "url=https://example.com/doc.pdf" http://localhost:8080/sources
```

You don't say which workspace it belongs to — that's the Classifier's job, based on the workspace
taxonomies it's routing against. PDF, DOCX, CSV, and plain text are all real, correctly-extracted
formats; anything else is rejected immediately with a `400`, not silently garbled. Every submission
gets a real, unconditional review-item record (informational — an admin sees it was submitted, but
it doesn't block anything by itself):

```json
{"source_id": "...", "pipeline_state": "submitted", "filename": "runbook.md"}
```

**Retrying safely**: if a request times out and you're not sure it went through, resend it with an
`Idempotency-Key` header (any string you generate, unique per logical submission) — a retried
request with the same key returns the original result instead of creating a second submission.

**Submitting several documents at once** needs the admin-only bulk endpoint
(`user-guide-admins.md` §4) — this one is for a single document at a time.

## 2. Checking submission status

`GET /sources/{id}` — visible only to whoever submitted it (submitter-only, no exceptions, not even
for an admin browsing generally: "whether a source exists is not public"):

```bash
curl -H "X-Karpwiki-User: you" http://localhost:8080/sources/<source_id>
```

```json
{
  "source_id": "...", "pipeline_state": "classifying", "status": "active",
  "workspace_id": null, "filename": "runbook.md", "label": "processing"
}
```

`label` is the human-friendly summary worth watching, not `pipeline_state` (the internal state
name) — it only ever takes one of these values:

| `label` | What it means |
|---|---|
| `processing` | Still moving through classification, duplicate check, or curation — normal, no action needed |
| `awaiting review` | Paused for an admin to look at (low classification confidence, a possible duplicate, or flagged content) — nothing to do but wait |
| `published` | Done — real wiki pages exist and are searchable |
| `rejected` | An admin declined it |
| `error` | Something failed and was surfaced to an admin for retry |

If real notification delivery is configured for the deployment (`KARPWIKI_NOTIFICATION_WEBHOOK_URL`
in `.env.example`), you'll also get a real message when your submission is ingested, rejected, or
merged as a duplicate — polling `GET /sources/{id}` yourself is always available either way.

## 3. What actually happens after you submit

None of this is synchronous — submitting returns immediately, and everything below happens as real
background work, typically finishing in seconds to a couple of minutes depending on the LLM calls
involved:

1. **Classification**: an LLM reads the document and assigns it a document type (which also
   determines its workspace) and a confidence score.
2. If confidence is too low, or a workspace's own taxonomy check disagrees, it stops at **awaiting
   review** for an admin to assign the right type manually. Otherwise it continues automatically.
3. **Duplicate check**: compared against existing content in the target workspace. A real duplicate
   always stops for admin review, regardless of the workspace's policy; if the workspace requires
   review for everything (a `gated` policy, typically used for higher-stakes content like legal or
   compliance documents), it stops here too even with no duplicate found.
4. **Curation**: an LLM writes (or updates) a cited source page, plus whatever concept/entity pages
   the content warrants — typically 5–15 pages touched per document, not just one. `overview.md`
   and `log.md` update automatically.
5. **Indexed and searchable** — the final state, `published`.

If a step fails outright (not just "needs review"), it's surfaced to an admin as an `error` item for
retry — your document isn't silently dropped.

## 4. Searching

`GET /search` — federated across every workspace you can access, ranked, with real citations back
to source material (no vector search, no LLM-synthesized answer — this returns real page excerpts
for you or your agent to read directly, same as Karpathy's own pattern):

```bash
curl -H "X-Karpwiki-User: you" "http://localhost:8080/search?q=retry+with+backoff"
```

```json
{
  "query_id": "...",
  "items": [
    {
      "page_id": "...", "workspace_id": "eng-docs", "path": "concepts/retry-with-backoff.md",
      "page_type": "concept", "title": "Retry with Backoff", "score": 0.68,
      "excerpt": "...retry <b>with</b> exponential <b>backoff</b>...",
      "citations": ["[^1]: runbook.md, p. 2"]
    }
  ]
}
```

Useful filters, all optional: `workspace_id` (repeatable — omit to search everywhere you can
access), `page_type` (repeatable — `concept`, `entity`, `source`, `overview`, `index`, `log`,
`comparison`), `tags` (repeatable), `date_from`/`date_to`, `limit`. `include_drafts=true` needs
`contributor` access (not just `reader`) in the workspaces it applies to — an unreviewed page
shouldn't leak to a plain reader by default.

**Rate this result if you use it** — `POST /search/{query_id}/feedback` with
`{"page_id": "...", "rating": "up"}` or `"down"`:

```bash
curl -X POST -H "X-Karpwiki-User: you" -H "Content-Type: application/json" \
  -d '{"page_id": "<page_id>", "rating": "up"}' \
  http://localhost:8080/search/<query_id>/feedback
```

This isn't just courtesy — it's a real input to the Maintenance Advisor's staleness detector: a
page with enough ratings and a high enough down-vote share gets automatically flagged for an admin
to re-review. Feedback only counts from the principal who actually ran that search.

## 5. Reading a page directly

`GET /pages/{id}` — the same content `GET /search` excerpted from, in full, plus resolved links to
other pages it cites (a link you don't have access to is simply left out, never shown-but-blocked):

```bash
curl -H "X-Karpwiki-User: you" http://localhost:8080/pages/<page_id>
```

The seven page types you'll see, each with a real, distinct role (not just a label):

| Type | What it is |
|---|---|
| `concept` | An idea, pattern, or technique that isn't tied to one specific named thing (e.g. "Retry with Backoff") |
| `entity` | A specific named thing — a service, API, role, or document (e.g. "Order Service", "Data Retention Policy v3") |
| `source` | The curated, cited summary of one raw document you (or someone) submitted |
| `overview` | One per workspace — a running hub: source count, page count, recent updates |
| `index` | One per workspace — a categorized catalog of every concept/entity/source/comparison page, the same "read this first" entry point Karpathy's own pattern uses |
| `log` | One per workspace — an append-only history of ingestion/lint/admin activity |
| `comparison` | A page contrasting two or more concepts/entities directly |

**Browsing without a specific query**: `GET /pages?workspace_id=...` lists a workspace's pages
directly (filterable the same way search is) — useful for walking a workspace's catalog
programmatically instead of guessing search terms. Add `&page_type=index` to get that one
workspace's `index` page (its `page_id` isn't otherwise predictable), then `GET /pages/{id}` on it
for the same categorized catalog Karpathy's own pattern reads first, in one page.

---
Previous: [08-implementation-stack.md](08-implementation-stack.md) · Back to: [00-overview.md](00-overview.md)
