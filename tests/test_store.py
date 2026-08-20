"""`ingestion.store` — the one entry point every submission source goes through (03 §2).
Rejection of genuinely unsupported binary content (`doc_extract.py`) found live during
Phase 3 step 62 prep, not part of either completeness audit."""

import pytest

from karpwiki import ingestion, objectstore


async def test_store_accepts_plain_text(session):
    source = await ingestion.store(session, b"hello world", "notes.txt", submitted_by="user:deepak")
    assert objectstore.read_bytes(source.object_key) == b"hello world"


async def test_store_rejects_genuinely_unsupported_binary_content(session):
    png_like = b"\x89PNG\r\n\x1a\n\x00\x01\xff\xfe"
    with pytest.raises(ingestion.UnsupportedContentError):
        await ingestion.store(session, png_like, "photo.png", submitted_by="user:deepak")


async def test_store_rejects_before_writing_anything(session):
    """A rejected submission leaves no trace — no object, no raw_source row — matching
    03 §2's own framing of `store` as creating exactly one record per accepted submission."""
    from sqlalchemy import func, select

    from karpwiki.models import RawSource

    before = (await session.execute(select(func.count()).select_from(RawSource))).scalar_one()
    png_like = b"\x89PNG\r\n\x1a\n\x00\x01\xff\xfe"
    with pytest.raises(ingestion.UnsupportedContentError):
        await ingestion.store(session, png_like, "photo.png", submitted_by="user:deepak")
    after = (await session.execute(select(func.count()).select_from(RawSource))).scalar_one()
    assert after == before
