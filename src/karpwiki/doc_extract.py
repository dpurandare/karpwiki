"""Binary document text extraction (03 §3's content_shape pre-step) — found live during
Phase 3 step 62 prep, not part of either completeness audit pass, in response to a direct
question about common enterprise document formats (DOCX, PDF, CSV, TXT).

**The gap this closes**: no spec section anywhere explicitly scopes PDF/DOCX support in or
out — CSV and TXT already worked (plain text, decode cleanly), but every text-producing
call in the ingest path (`classify.detect_content_shape`, `ingestion.classify_source`/
`curate_source`) used a bare `payload.decode("utf-8", errors="replace")`. For a binary PDF
or DOCX, that never raises — it silently substitutes most of the file with U+FFFD
replacement characters and feeds the garbled result to the Classifier/Curator LLM calls,
rather than extracting real text or failing loudly. `connectors_git.py` already has the
right instinct for this exact class of problem (skips a file that fails UTF-8 decode
rather than submitting it, `09` its own module docstring) — this module gives every
submission path (not just the git connector) a real alternative to "decode or skip":
extract real text from the two most common binary formats, and only skip/reject what's
genuinely neither text nor a recognized binary format.

**Scope, stated explicitly**: modern DOCX (Office Open XML, a ZIP/XML container) and PDF
only. Legacy `.doc` (the pre-2007 OLE2/CFB binary format) is a materially different, harder
parsing problem needing a different library entirely (no maintained pure-Python reader);
nothing in spec/ names it, and it is not "the most common" format today the way `.docx`
is — explicitly out of scope here, not silently unhandled. A `.doc` file is simply neither
UTF-8-decodable nor a recognized format, so `extract_text` returns `None` for it, same as
any other genuinely unsupported binary.
"""

import io

# ZIP local file header — shared by every Office Open XML format (docx/xlsx/pptx) and
# plain .zip, so the filename extension is what actually disambiguates "this is a DOCX we
# know how to read" from "this is some other ZIP-based file."
_DOCX_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF-"


def detect_binary_format(filename: str, payload: bytes) -> str | None:
    """`"pdf"` | `"docx"` | `None` (not a binary format this module extracts)."""
    if payload.startswith(_PDF_MAGIC):
        return "pdf"
    if payload.startswith(_DOCX_MAGIC) and filename.lower().endswith(".docx"):
        return "docx"
    return None


def extract_text(filename: str, payload: bytes) -> str | None:
    """The canonical raw-bytes-to-text step for the whole ingest path — real extraction
    for a recognized binary format, a plain UTF-8 decode otherwise. Returns `None` when
    neither works: genuinely unsupported binary content (a legacy `.doc`, an image, an
    executable, ...), which callers (`ingestion.store`) reject at submission time rather
    than silently proceeding with garbage.
    """
    fmt = detect_binary_format(filename, payload)
    if fmt is not None:
        try:
            return _extract_pdf(payload) if fmt == "pdf" else _extract_docx(payload)
        except Exception:
            # Magic bytes matched but the container itself is corrupt/truncated — still
            # "couldn't extract," the same outcome as never recognizing the format at all.
            return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _extract_pdf(payload: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(payload))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_docx(payload: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(payload))
    return "\n\n".join(p.text for p in document.paragraphs if p.text.strip())
