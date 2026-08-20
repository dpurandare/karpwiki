"""Classification decisions (03 §3) — no I/O, no database, no transitions.

03 §3 runs a deterministic pre-step, then the LLM, then a routing gate requiring both to
agree. Everything here is the decision half; `ingestion.py` performs it.
"""

import json
import re
from dataclasses import dataclass

import yaml
from pydantic import BaseModel, Field

from . import doc_extract
from .models import ContentShape

STRUCTURED_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".csv", ".toml", ".xml"})

# A label matches when this fraction of its tokens appear in the text. A starting value,
# tunable per workspace like the other thresholds in 09 §6.
LEXICAL_MIN_SCORE = 0.5

# Tokens that carry no routing signal on their own.
_STOPWORD_TOKENS = frozenset({"doc", "docs", "document", "general", "misc", "other"})


class ClassificationResult(BaseModel):
    """The Classifier's structured output (08 §2's Pydantic AI result model)."""

    summary: str = Field(description="Two or three sentences; used for duplicate detection.")
    document_type: str = Field(description="Exactly one label from the supplied taxonomy.")
    confidence: float = Field(ge=0.0, le=1.0, description="Self-reported, 0 to 1.")


@dataclass(frozen=True)
class LexicalMatch:
    label: str
    score: float


@dataclass(frozen=True)
class Routing:
    """Outcome of 03 §3's gate."""

    accepted: bool
    document_type: str | None
    reason: str
    candidates: tuple[str, ...] = ()


def detect_content_shape(filename: str, payload: bytes) -> ContentShape:
    """`narrative` or `structured_data` from extension and structural parse (03 §3 step 1).

    Mechanical on purpose: data either parses or it does not, so this needs no model.
    Uses `doc_extract.extract_text` rather than a bare UTF-8 decode so a PDF/DOCX gets its
    real extracted text run through the same structural check — in practice this almost
    always lands `narrative` (a data-format document rendered as PDF/DOCX prose is rare),
    but it's the same real text every other stage of the pipeline now sees, not a special
    case. `ingestion.store` already rejects content this can't extract at all before a
    `raw_source` ever exists, so the `None` fallback below is defensive, not the real gate.
    """
    suffix = filename[filename.rfind(".") :].lower() if "." in filename else ""
    if suffix in STRUCTURED_SUFFIXES:
        return ContentShape.structured_data

    text = doc_extract.extract_text(filename, payload)
    if text is None:
        return ContentShape.narrative

    if _parses_as_data(text):
        return ContentShape.structured_data
    return ContentShape.narrative


def _parses_as_data(text: str) -> bool:
    """Content sniffing for a file whose extension did not already answer the question.

    Only JSON counts here. YAML and CSV are too eager on prose to sniff safely: `csv.Sniffer`
    finds a delimiter in ordinary sentences, and any English line containing a colon
    ("Note: see below") parses as a YAML mapping. Both would classify narrative documents as
    data and send them down 07 §1.3's structured-data curation path. A `.yaml` or `.csv`
    file is caught by its extension; prose is the case that must not be misread.
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return False
    try:
        loaded = json.loads(stripped)
    except ValueError:
        return False
    return isinstance(loaded, (dict, list)) and bool(loaded)


def derive_artifact_identity(
    filename: str, payload: bytes, shape: ContentShape
) -> tuple[str | None, str | None]:
    """Stable identity and version for a structured source (03 §3 step 2).

    Spec'd as derivable "from its path, name, or declared schema/resource identity", so a
    declared field wins and the filename stem is the fallback.
    """
    if shape is not ContentShape.structured_data:
        return None, None

    stem = filename.rsplit("/", 1)[-1]
    stem = stem[: stem.rfind(".")] if "." in stem else stem
    identity, version = stem or None, None

    try:
        loaded = yaml.safe_load(payload.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return identity, version

    if isinstance(loaded, dict):
        for key in ("$id", "id", "name", "title"):
            value = loaded.get(key)
            if isinstance(value, str) and value.strip():
                identity = value.strip()
                break
        for key in ("version", "apiVersion", "schemaVersion"):
            value = loaded.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                version = str(value).strip()
                break
    return identity, version


def lexical_match(text: str, document_types: list[str]) -> LexicalMatch | None:
    """Score the source against the taxonomy's labels (03 §3 step 3).

    The same static-table lookup 04 §4 uses on the query path. Returns the single best
    match, or None when nothing scores high enough or two labels tie — an ambiguous signal
    is no signal, and must not be reported as agreement.
    """
    lowered = text.lower()
    scored: list[LexicalMatch] = []
    for label in document_types:
        tokens = [t for t in re.split(r"[.\-_/\s]+", label.lower()) if t and t not in _STOPWORD_TOKENS]
        if not tokens:
            continue
        hits = sum(1 for t in tokens if re.search(rf"\b{re.escape(t)}\w*", lowered))
        score = hits / len(tokens)
        if score >= LEXICAL_MIN_SCORE:
            scored.append(LexicalMatch(label=label, score=score))

    if not scored:
        return None
    scored.sort(key=lambda m: m.score, reverse=True)
    if len(scored) > 1 and scored[0].score == scored[1].score:
        return None
    return scored[0]


def route(
    result: ClassificationResult,
    lexical: LexicalMatch | None,
    *,
    min_confidence: float,
    document_types: list[str],
) -> Routing:
    """03 §3's gate: confidence AND the lexical cross-check, not confidence alone."""
    if result.document_type not in document_types:
        return Routing(
            False,
            None,
            "model returned a label outside the taxonomy",
            candidates=(result.document_type,),
        )

    if result.confidence < min_confidence:
        candidates = (result.document_type,) + ((lexical.label,) if lexical else ())
        return Routing(False, None, "confidence below threshold", candidates=tuple(dict.fromkeys(candidates)))

    if lexical is not None and lexical.label != result.document_type:
        return Routing(
            False,
            None,
            "lexical cross-check disagreed",
            candidates=(result.document_type, lexical.label),
        )

    return Routing(True, result.document_type, "confidence met and cross-check agreed")
