"""Curator decisions (03 §6) — no I/O, no database, no transitions.

One LLM call proposes the source page's content and the concept/entity pages a source
warrants (`CuratedContent`). A second, separate call folds a duplicate source into an
existing page when an admin resolves a `duplicate` review item as `merge` (03 §4,
`MergedPage`) — its own call, not a variant of curation, since it targets one page an
admin already picked rather than proposing new ones. Everything else here is deterministic
rendering: matching a proposed page against the workspace's existing pages, and
regenerating `overview.md`/`log.md` bodies from queryable ground truth rather than parsing
prior markdown.

Phase 1 curates every source as `narrative` — 07 §1.3's structured-data metadata/intent
page is out of scope (phase1-tasklist §0, accepted simplification).
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
