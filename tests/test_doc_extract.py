"""Binary document text extraction (PDF, DOCX) — found live during Phase 3 step 62 prep."""

import io

import docx
import pytest

from karpwiki import doc_extract

# A minimal, hand-built, valid single-page PDF with real text content in its content
# stream — real bytes a real PDF reader parses, not a mock.
_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 5 0 R >> >> /MediaBox [0 0 200 200] /Contents 4 0 R >>
endobj
4 0 obj
<< /Length 58 >>
stream
BT /F1 18 Tf 10 100 Td (Hello PDF World) Tj ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f
trailer
<< /Size 6 /Root 1 0 R >>
startxref
0
%%EOF
"""


def _real_docx_bytes(*paragraphs: str) -> bytes:
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def test_detect_binary_format_recognizes_pdf_magic_bytes():
    assert doc_extract.detect_binary_format("report.pdf", _MINIMAL_PDF) == "pdf"


def test_detect_binary_format_recognizes_docx():
    assert doc_extract.detect_binary_format("report.docx", _real_docx_bytes("hi")) == "docx"


def test_detect_binary_format_requires_the_docx_extension_not_just_zip_magic():
    """The ZIP local-file-header magic bytes are shared by every Office Open XML format
    (docx/xlsx/pptx) and plain .zip — the extension is what disambiguates."""
    zip_like = _real_docx_bytes("hi")
    assert doc_extract.detect_binary_format("archive.zip", zip_like) is None
    assert doc_extract.detect_binary_format("workbook.xlsx", zip_like) is None


def test_detect_binary_format_returns_none_for_plain_text():
    assert doc_extract.detect_binary_format("notes.txt", b"just some prose") is None


def test_extract_text_reads_a_real_pdf():
    text = doc_extract.extract_text("report.pdf", _MINIMAL_PDF)
    assert text is not None
    assert "Hello PDF World" in text


def test_extract_text_reads_a_real_docx():
    text = doc_extract.extract_text(
        "report.docx", _real_docx_bytes("First paragraph.", "Second paragraph.")
    )
    assert text is not None
    assert "First paragraph." in text
    assert "Second paragraph." in text


def test_extract_text_decodes_plain_utf8():
    assert doc_extract.extract_text("notes.txt", "café".encode()) == "café"


def test_extract_text_returns_none_for_genuinely_unsupported_binary():
    # Neither PDF nor DOCX magic bytes, and not valid UTF-8 either.
    assert doc_extract.extract_text("photo.png", b"\x89PNG\r\n\x1a\n\x00\x01\xff\xfe") is None


def test_extract_text_returns_none_for_a_corrupt_pdf():
    """Magic bytes matched but the container itself is truncated/corrupt — still
    "couldn't extract," not a crash."""
    assert doc_extract.extract_text("broken.pdf", b"%PDF-1.4\ngarbage, not a real pdf") is None


def test_extract_text_returns_none_for_a_corrupt_docx():
    assert doc_extract.extract_text("broken.docx", b"PK\x03\x04garbage, not a real docx") is None


@pytest.mark.parametrize("filename", ["report.doc", "report.DOC"])
def test_legacy_doc_is_explicitly_out_of_scope(filename):
    """Legacy .doc (pre-2007 OLE2/CFB binary) is not PDF/DOCX-shaped and not valid UTF-8 —
    a real, documented boundary (module docstring), not silently mishandled."""
    legacy_doc_like_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32
    assert doc_extract.detect_binary_format(filename, legacy_doc_like_bytes) is None
    assert doc_extract.extract_text(filename, legacy_doc_like_bytes) is None
