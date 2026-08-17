"""Cursor pagination (09 §14) — shared by every list endpoint's `(created_at, id)` cursor.

Opaque to callers by construction (base64), not by any security property — the cursor is
just the sort key plus a tiebreak id, exactly as 09 §14 describes it.
"""

import base64
import uuid
from datetime import datetime

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


def encode_cursor(created_at: datetime, tiebreak_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{tiebreak_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    created_at, tiebreak_id = raw.rsplit("|", 1)
    return datetime.fromisoformat(created_at), uuid.UUID(tiebreak_id)
