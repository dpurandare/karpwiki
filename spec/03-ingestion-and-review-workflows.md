# 03 — Ingestion and Review Workflows

This document covers the path from "someone has a document" to "the wiki reflects it", including
where admin review fits — both the **always-on submission notice** and the **actionable**
review items (duplicates, low-confidence classification).

## 1. Pipeline States

```mermaid
stateDiagram-v2
    [*] --> submitted: end user upload,\nor connector discovers new/changed content
    submitted --> classifying
    classifying --> classified: type + workspace assigned
    classifying --> pending_review: confidence below threshold
    classified --> duplicate_check
    duplicate_check --> ingesting: no duplicate found,\nworkspace policy = auto
    duplicate_check --> pending_review: duplicate found (always),\nOR workspace policy = gated
    pending_review --> ingesting: admin approves
    pending_review --> rejected: admin rejects
    ingesting --> ingested: Curator Agent completes
    ingesting --> error: processing failure
    error --> pending_review: surfaced to admin for retry/triage
    ingested --> [*]
    rejected --> [*]
```

| State | Meaning | Raw source status | Wiki visibility |
|---|---|---|---|
| `submitted` | Source stored in object store, `raw_source` record created | `active` | A placeholder `source` page is created immediately (marked "processing") — the document is "in the wiki" from this point, per the requirement that submissions are added right away. |
| `classifying` | Classifier assigns `document_type` (+ confidence) and resolves `workspace_id` | `active` | placeholder `source` page |
| `classified` | Type + workspace confirmed (confidence ≥ threshold) | `active` | placeholder `source` page |
| `duplicate_check` | Compare against existing content in target workspace | `active` | placeholder `source` page |
| `pending_review` | A review item blocks further automatic processing | `active` | placeholder `source` page, marked "awaiting review" |
| `ingesting` | Curator Agent runs the **ingest** operation (§6) | `active` | placeholder `source` page being updated |
| `ingested` | Curator finished; wiki pages updated, indices marked `pending` | `active` | `source` page + touched concept/entity pages, `overview.md`, `log.md` |
| `rejected` | Admin declined ingestion | `rejected` (retained, excluded from search/ingestion) | placeholder `source` page marked "rejected" with reason |
| `error` | A step failed | `active` | placeholder `source` page marked "error", surfaced to admin |

**Note on `status` vs. these markings.** The quoted labels above ("processing", "awaiting review",
"rejected", "error") describe how the placeholder `source` page is presented in the UI during the
pipeline — they are not values of the page frontmatter `status` field (`draft|published|archived`,
[01](01-architecture-and-data-model.md) §6), which remains `draft` until the Curator Agent's
finalization step (§6 step 2) sets it to `published` on `ingested`.

## 2. Submission

Two entry points feed the same pipeline:

- **End users**: upload a file, paste text, or submit a URL through the Platform UI or API. This
  is the primary path for the requirement *"end user should be able to add documents to the
  wiki."*
- **Connectors** (managed by admins, [05](05-admin-backend-and-maintenance.md) §7): scheduled
  crawlers/integrations (e.g., Git repositories, Confluence, Notion, websites, OpenAPI specs,
  `llms.txt` files) that discover new or changed content and submit it the same way, with
  `submitted_by = connector:<connector_id>`.

Either way, a `raw_source` record is created in the **target-undetermined** state — the gateway
accepts the upload without requiring the caller to know which workspace it belongs to; that's the
Classifier's job (§3).

## 3. Classification & Routing

Classification runs in two parts: a deterministic pre-step that needs no model, then the LLM
Classifier proper. Splitting them keeps the model's job to what actually requires judgement and
yields a second, independent routing signal for free.

**Pre-step (deterministic, no LLM):**

1. Tag `content_shape` (`narrative` | `structured_data`) from MIME type and structural parse —
   JSON/YAML/CSV/spreadsheet payloads parse as data or they do not. This is mechanical and needs no
   model. It is independent of `document_type`/workspace routing, and determines how the Curator
   Agent ingests the source (§6 below; full treatment in
   [07](07-additional-features-and-roadmap.md) §1).
2. For `structured_data` sources, derive identity/version metadata: `artifact_identity` (a stable
   identifier — from its path, name, or declared schema/resource identity) and
   `source_version`/`source_modified_at` (from version headers, file metadata, or naming
   conventions, where extractable). Used by Duplicate Detection (§4) to recognize a new version of
   an existing artifact.
3. Compute a **lexical taxonomy match**: score the source's filename, title, and text against the
   central taxonomy's labels and keywords ([01](01-architecture-and-data-model.md) §3) — the same
   static-table lookup the query path uses in [04](04-search-and-retrieval.md) §4, applied at
   ingest. Cheap, provider-neutral, and may return no match.

**Classifier (LLM-based, async worker):**

4. Produces a short summary — consumed by Duplicate Detection's near-match query (§4).
5. Assigns a `document_type` label from the **central taxonomy** with a self-reported confidence
   score.
6. Resolves `document_type → workspace_id` via the taxonomy's routing table.

**Routing gate**, requiring *both* signals rather than confidence alone:

7. Proceed to `classified` only if confidence ≥ the workspace's configured threshold
   (`SCHEMA.md`) **and** the lexical match from step 3 either agrees with the LLM's label or is
   absent. A disagreement between the two — or a confident lexical match the LLM contradicts —
   creates a `kind=classification` review item with both candidates and moves to `pending_review`,
   whatever the self-reported confidence says.

The cross-check exists because self-reported LLM confidence is poorly calibrated
([09](09-implementation-notes.md) §9), and §9's correction is an offline loop over admin
resolutions that only pays off after enough of them accumulate. The lexical signal is independent
of the model, available at decision time rather than weeks later, and costs a table lookup — so it
is the one calibration input that works from the first document, and it matters more on a
cheaper model, not less.

The asymmetry with [04](04-search-and-retrieval.md) §4 is deliberate. On the query path a wrong
lexical match is benign — it narrows a search that falls back to full fan-out. At ingest a wrong
label routes the source to the wrong workspace and builds pages there, so here the lexical signal
never routes on its own: it can only confirm the LLM or force a human decision.

```mermaid
flowchart LR
    SRC[New raw source] --> PRE["Pre-step (no LLM):\ncontent_shape, artifact_identity,\nlexical taxonomy match"]
    PRE --> CLS[Classifier]
    CLS --> GATE{"confidence >= threshold\nAND lexical match agrees\nor is absent?"}
    GATE -->|yes| TYPE[document_type assigned]
    GATE -->|no| RI1[Review item:\nkind=classification\nboth candidates + confidence]
    TYPE --> ROUTE[Taxonomy lookup]
    ROUTE --> WS[workspace_id resolved]
    RI1 -.admin picks type.-> ROUTE
```

**Admin actions on a `classification` review item**: confirm the top suggestion, pick a different
type from the taxonomy, or — if nothing fits — create a new document type (which itself is a
repository-management action, [05](05-admin-backend-and-maintenance.md) §7) and assign it.

## 4. Duplicate Detection

Runs once `workspace_id` is known, against that workspace's existing content only.

```mermaid
flowchart TD
    A[New source, workspace known] --> Z{content_shape=structured_data AND\nartifact_identity matches an existing\nraw_source with an older\nsource_version/source_modified_at?}
    Z -- yes --> H[Review item: kind=duplicate, severity=low\nproposed: supersede existing source --\nnew version of same artifact]
    Z -- no --> B{Exact content-hash match\nin workspace?}
    B -- yes --> C[Review item: kind=duplicate, severity=high\nproposed: reject as exact duplicate of source X]
    B -- no --> D{Near-duplicate via Full-Text Index\n'more like this' similarity\n>= workspace threshold?}
    D -- yes --> E[Review item: kind=duplicate, severity=medium\nproposed: merge into page Y / supersede page Y / keep both]
    D -- no --> F[No duplicate concerns —\nproceed per workspace ingestion policy]
    H --> G[pending_review]
    C --> G
    E --> G
```

- **Same artifact, newer version** (`structured_data` only): if the new source's
  `artifact_identity` (§3) matches an existing `raw_source` in the same workspace whose
  `source_version`/`source_modified_at` is older, raise a `duplicate` review item with
  `severity=low` and `proposed_action=supersede` pre-filled — the expected case when re-ingesting
  an updated schema/config. Always blocks (`pending_review`), but the low severity and pre-filled
  action keep resolution to a single confirmation; approving applies the `supersede` resolution
  below (prior source marked `superseded`, existing `source`/`entity` pages updated in place via
  new `page_version`s — [01](01-architecture-and-data-model.md) §5).
- **Exact match**: `content_hash` of the new source equals an existing `raw_source.content_hash`
  in the same workspace. Always blocks (`pending_review`), regardless of workspace policy.
- **Near match**: run the new source's summary as a similarity ("more like this") query against
  the workspace's Full-Text Index ([02](02-storage-and-indexing.md) §4), comparing against existing
  wiki page content — no embeddings involved. Threshold is set per workspace in `SCHEMA.md` (dedup
  sensitivity). Scores above threshold always block (`pending_review`) — this is the requirement
  *"identify potential duplicates and mark review item for the admin user."* Lexical similarity
  surfaces *candidates*; the Curator Agent makes the final near-duplicate judgment when the review
  item is resolved (merge/supersede/keep both, below) by reading both documents — LLM use stays
  confined to ingest, never the search/retrieval path.
- **No duplicate**: continues to ingestion per the workspace's ingestion policy (§7).

**Admin actions on a `duplicate` review item**: `reject` (new source marked `rejected`), `merge`
(Curator folds the new source's content into the existing page(s) as an update — produces a new
`page_version` with `trigger=ingest` and a change summary noting the merge), `supersede` (existing
source/page marked superseded, new one becomes canonical), or `keep both` (proceeds to normal
ingestion as a distinct source — use when the similarity is coincidental).

## 5. Submission Review Item (always created)

Independent of the duplicate/classification checks, **every** new submission creates a
`kind=submission` review item the moment the `raw_source` record exists (state `submitted`).
This satisfies *"a review item should be added for the admin staff so that they know [about the]
addition of new document"* — it is informational by default (`status=open` until resolved with
`resolved_action=acknowledge`, a one-click no-op resolution), but admins can open it to see the
placeholder `source` page, reassign workspace, or halt processing if something looks wrong before
`ingesting` completes.

| Review item kind | Created when | Blocks pipeline? | Default `resolved_action` |
|---|---|---|---|
| `submission` | Every new `raw_source` | No | `acknowledge` (informational) |
| `classification` | Classifier confidence below threshold | Yes (`pending_review`) | None — admin must choose a type |
| `duplicate` | Exact/near/version-supersede match found (§4) | Yes (`pending_review`) | None — admin must choose an action (`supersede` pre-filled for version-supersede) |

## 6. Ingestion ("Ingest" Operation)

Once a source reaches `ingesting` (auto or admin-approved), the Curator Agent performs the
ingest operation from Karpathy's pattern, scoped to the target workspace:

1. Read the raw source (full text/structured content from the object store).
2. Finalize the `source` page (placeholder created in §1), shaped by `content_shape` (§3): a
   `narrative` source gets a full summary, key extracted points, and citations back to the raw
   source; a `structured_data` source gets a metadata/intent page — schema, fields, purpose — per
   [07](07-additional-features-and-roadmap.md) §1.
3. Create or update **concept** and **entity** pages as warranted — typically 5–15 pages touched
   per source, consistent with Karpathy's observation.
4. Update `overview.md`: source count, page count, key findings, recent updates.
5. Append an entry to the `ingestion_log` (materialized into `log.md`).
6. Mark every touched page's `index_status` row `pending` for the Full-Text Index
   ([02](02-storage-and-indexing.md) §7) — this enqueues a reindex job.
7. Set the pipeline state to `ingested` (`raw_source.status` remains `active`, per §1).

On failure at any step, state moves to `error` and a review item is raised with the failure
context (state `pending_review`) for admin triage/retry.

## 7. Workspace Ingestion Policy

Set per workspace in `SCHEMA.md`:

| Policy | Behavior after `duplicate_check` finds nothing |
|---|---|
| `auto` (default) | Proceeds directly to `ingesting`. Lower-friction; suited to high-volume, lower-risk document types (e.g. meeting notes, engineering docs from a trusted connector). |
| `gated` | Always moves to `pending_review`, requiring explicit admin approval before `ingesting`. Suited to higher-stakes document types (e.g. policies, legal/compliance content). |

Duplicate and low-confidence-classification review items are **always** raised regardless of this
policy — only the "no concerns found" path is affected.

## 8. End-to-End Sequence

```mermaid
sequenceDiagram
    actor User as End User
    participant GW as Common Gateway
    participant ING as Ingestion Service
    participant CLS as Classifier (LLM)
    participant DUP as Dedup Check
    participant REV as Review Service
    actor Admin
    participant CUR as Curator Agent
    participant WIKI as Wiki Service

    User->>GW: Upload document
    GW->>ING: Create raw_source (submitted)
    ING->>WIKI: Create placeholder source page
    ING->>REV: Create review item (kind=submission)
    REV-->>Admin: Notification (new document added)

    ING->>CLS: Classify
    CLS-->>ING: document_type, confidence, workspace_id

    alt low confidence
        ING->>REV: Create review item (kind=classification)
        REV-->>Admin: Needs type confirmation
        Admin->>REV: Confirm/assign type
    end

    ING->>DUP: Check duplicates in workspace
    alt duplicate found
        ING->>REV: Create review item (kind=duplicate)
        REV-->>Admin: Needs action (merge/reject/keep both/supersede)
        Admin->>REV: Resolve
    else no duplicate, policy=gated
        ING->>REV: Create review item (pending_review)
        Admin->>REV: Approve
    end

    ING->>CUR: Run ingest operation
    CUR->>WIKI: Update source/concept/entity pages, overview.md, log.md
    CUR->>ING: Mark index_status = pending (reindex queued)
    ING->>ING: state = ingested
```

---
Previous: [02-storage-and-indexing.md](02-storage-and-indexing.md) · Next: [04-search-and-retrieval.md](04-search-and-retrieval.md)
