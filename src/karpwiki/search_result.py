"""04 §7's search-result shape, shared by every retrieval backend (phase2-tasklist.md
step 26) — `search.py` (Postgres, the shared index) and `dedicated_index.py` (OpenSearch,
per-workspace dedicated instances) both return this type, and `search.merge_federated`
combines them. Split out from `search.py` to avoid a circular import: `search.py` calls
into `dedicated_index.py` (to route an indexing write there for a dedicated workspace),
and `dedicated_index.py` needs this type back — neither module can own it.
"""

import re
import uuid
from dataclasses import dataclass

# Shared by search.py's and dedicated_index.py's own `search()` — both had an identical,
# uncapped `limit: int = 20` default with no way to cap an arbitrarily large caller-supplied
# value, unlike every list endpoint's own `pagination.py` (DEFAULT_LIST_LIMIT/MAX_LIST_LIMIT).
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100

# 01 §6: citations are markdown footnote *definitions* — "[^1]: filename.pdf, p. 4" — at
# the start of a line. Captures the marker and the definition text separately so callers
# get the definition text without re-parsing the bracket syntax themselves.
_FOOTNOTE_DEFINITION = re.compile(r"^\[\^([^\]]+)\]:\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class SearchResult:
    """04 §7's result-provenance shape — richer than `search.Hit`, which near-duplicate
    scoring (03 §4) still returns unchanged since it needs none of this."""

    page_id: uuid.UUID
    workspace_id: str
    path: str
    page_type: str
    title: str
    score: float
    excerpt: str
    citations: tuple[str, ...]


def extract_citations(content: str) -> tuple[str, ...]:
    return tuple(
        f"[^{marker}]: {definition.strip()}"
        for marker, definition in _FOOTNOTE_DEFINITION.findall(content)
    )
