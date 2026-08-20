"""Curator decisions (03 §6, 07 §1) — no I/O, no database, no transitions.

One LLM call proposes the source page's content and the concept/entity pages a source
warrants — `CuratedContent` for a `narrative` source, `StructuredCuratedContent` for
`structured_data` (07 §1.1's two treatments; `ingestion.curate_source` branches on
`RawSource.content_shape` and calls one or the other, phase3-tasklist.md step 61). A
second, separate call folds a duplicate source into an existing page when an admin resolves
a `duplicate` review item as `merge` (03 §4, `MergedPage`) — its own call, not a variant of
curation, since it targets one page an admin already picked rather than proposing new ones.
Everything else here is deterministic rendering: matching a proposed page against the
workspace's existing pages, and regenerating `overview.md`/`log.md`/`index.md` bodies from
queryable ground truth rather than parsing prior markdown.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

OVERVIEW_RECENT_LIMIT = 10
LOG_RECENT_LIMIT = 20


class CuratedPage(BaseModel):
    """One concept or entity page the source warrants (03 §6 step 3)."""

    page_type: Literal["concept", "entity"]
    title: str = Field(min_length=1)
    tags: list[str] = Field(min_length=2, description="At least two tags (01 §6).")
    body: str = Field(min_length=1, description="Markdown body only — no frontmatter block.")


class CuratedContent(BaseModel):
    """The Curator's structured output for one source (03 §6 steps 2-3)."""

    source_title: str = Field(min_length=1)
    source_description: str = Field(
        min_length=1, description="One sentence — becomes the source page's frontmatter description."
    )
    source_summary: str = Field(min_length=1, description="2-5 sentences for the source page body.")
    source_key_points: list[str] = Field(default_factory=list)
    pages: list[CuratedPage] = Field(
        default_factory=list,
        description=(
            "Concept/entity pages this source warrants. If a page already covers the same "
            "concept or entity, reuse its EXACT existing title so it is updated rather than "
            "duplicated; otherwise pick a new, distinct title."
        ),
    )


class StructuredField(BaseModel):
    """One row of the structure table (07 §1.3 step 1) — a field/column/parameter/endpoint
    the artifact declares."""

    name: str = Field(min_length=1)
    type: str | None = None
    description: str | None = None


class StructuredCuratedContent(BaseModel):
    """The Curator's structured output for a `structured_data` source (07 §1.3) —
    metadata-first: a structure table and an intent statement, not a prose summary. Reuses
    `CuratedPage` for `pages` (entity pages for defined tables/resources/config
    sections, §1.3 step 4) — the same create-or-update-by-exact-title matching
    `_write_curated_page` already does for narrative content applies unchanged.
    """

    source_title: str = Field(min_length=1)
    intent_statement: str = Field(
        min_length=1,
        description=(
            "One sentence: what this artifact is for, what system/process it supports, who "
            "owns/produces/consumes it — inferred from context (surrounding files, naming, "
            "comments), not invented. Becomes the source page's frontmatter description AND "
            "its index.md catalog entry (07 §1.1: 'phrased the way a user would search... not "
            "the filename') — the same one-sentence contract 01 §6 already requires of every "
            "page's description, so no separate field is needed for the catalog entry."
        ),
    )
    fields: list[StructuredField] = Field(
        default_factory=list, description="Structure table rows — name, type, description."
    )
    pages: list[CuratedPage] = Field(
        default_factory=list,
        description=(
            "An entity page for each major table, resource, or config section this artifact "
            "defines, when significant enough to be referenced from elsewhere (07 §1.3 step "
            "4). Reuse an existing page's EXACT title to update it rather than duplicate it."
        ),
    )


def render_structured_source_body(
    content: StructuredCuratedContent,
    *,
    filename: str,
    artifact_identity: str | None,
    source_version: str | None,
) -> str:
    """07 §1.3: structure table + intent statement + provenance — the `structured_data`
    counterpart to `render_source_body`'s narrative summary+citations treatment (07 §1.1's
    own distinguishing framing: "metadata-first, not a prose summary")."""
    if content.fields:
        rows = "\n".join(
            f"| {f.name} | {f.type or ''} | {f.description or ''} |" for f in content.fields
        )
        table = f"| Field | Type | Description |\n|---|---|---|\n{rows}"
    else:
        table = "(no fields extracted)"

    provenance = [f"- Source file: {filename} [^1]"]
    if artifact_identity:
        provenance.append(f"- Artifact identity: `{artifact_identity}`")
    if source_version:
        provenance.append(f"- Version: `{source_version}`")

    return (
        f"{content.intent_statement}\n\n"
        f"## Structure\n\n{table}\n\n"
        "## Provenance\n\n" + "\n".join(provenance) + "\n\n"
        f"[^1]: {filename}"
    )


class MergedPage(BaseModel):
    """The Curator's output for folding a duplicate source into an existing page (03 §4's
    `merge` duplicate resolution)."""

    body: str = Field(min_length=1, description="The page's full updated markdown body.")
    change_summary: str = Field(min_length=1, description="One sentence noting the merge.")


@dataclass(frozen=True)
class ExistingPage:
    page_id: object  # uuid.UUID; kept untyped here to avoid importing models into this pure module
    title: str
    path: str


# Directory names per 01 §4's diagram — not naive pluralization ("entity" + "s" is wrong).
PAGE_DIRECTORY = {"concept": "concepts", "entity": "entities"}


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "page"


def match_existing(title: str, existing: list[ExistingPage]) -> ExistingPage | None:
    """Case-insensitive exact match — the contract the prompt asks the model to honour."""
    lowered = title.strip().lower()
    for page in existing:
        if page.title.strip().lower() == lowered:
            return page
    return None


def render_source_body(content: CuratedContent, *, filename: str) -> str:
    """01 §6: citations as markdown footnotes referencing the raw source by filename."""
    points = "\n".join(f"- {p}" for p in content.source_key_points)
    return (
        f"{content.source_summary} [^1]\n\n"
        f"## Key Points\n\n{points}\n\n"
        f"## Source\n\n[^1]: {filename}"
    )


def render_overview_body(
    *, source_count: int, page_count: int, recent: list[tuple[str, str, str]]
) -> str:
    """`recent` is (title, description, path) for the most recently ingested sources.

    Regenerated fresh each ingest from queryable data, not appended to — see the module
    docstring for why round-tripping markdown text was rejected.
    """
    lines = "\n".join(f"- **{t}** — {d} (`{p}`)" for t, d, p in recent[:OVERVIEW_RECENT_LIMIT])
    return (
        f"- Sources ingested: {source_count}\n"
        f"- Pages: {page_count}\n\n"
        f"## Recent Updates\n\n{lines or '- (none yet)'}"
    )


def render_index_body(
    *,
    concepts: list[tuple[str, str, str]],
    entities: list[tuple[str, str, str]],
    sources: list[tuple[str, str, str]],
    comparisons: list[tuple[str, str, str]],
) -> str:
    """Catalog of all pages, one-line summaries organized by category (01 §4,
    phase3-tasklist.md step 60) — the literal file-based form of Karpathy's "LLM reads
    index.md first" pattern (00 §2 Principle 8, 04 §3). Each category is a list of
    (title, description, path) tuples, alphabetical by title (queried that way — see
    `ingestion._refresh_index`); `overview`/`index`/`log` are structural pages, not
    catalog members, matching `curate.PAGE_DIRECTORY`'s own concept/entity-only scope and
    `advisor.ORPHAN_CANDIDATE_PAGE_TYPES`'s identical structural/content distinction.
    """

    def _section(name: str, items: list[tuple[str, str, str]]) -> str:
        if not items:
            return f"## {name}\n\n(none yet)"
        lines = "\n".join(f"- [{title}]({path}) — {description}" for title, description, path in items)
        return f"## {name}\n\n{lines}"

    return "\n\n".join(
        [
            _section("Concepts", concepts),
            _section("Entities", entities),
            _section("Sources", sources),
            _section("Comparisons", comparisons),
        ]
    )


def render_log_body(entries: list[tuple[datetime, str]]) -> str:
    """`entries` is (timestamp, description), newest first, already merged from every
    source 02 §5 names for `log.md` — `ingestion_log` and `admin_action_log` (09 §23);
    `lint_log` doesn't exist in Phase 1, no lint pass is built. The merge and per-source
    description formatting happen in `ingestion.refresh_log`, not here — this function
    stays a pure renderer over whatever timeline it's given.
    """
    lines = "\n".join(
        f"- {ts.isoformat()}: {desc}" for ts, desc in entries[:LOG_RECENT_LIMIT]
    )
    return lines or "- (none yet)"
