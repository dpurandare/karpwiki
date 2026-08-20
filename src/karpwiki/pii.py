"""PII detection (07 §2, phase3-tasklist.md step 71) — a dedicated, deterministic scanner,
not an LLM judgment. `07` §2 offers "Classifier (or a dedicated scanner)" as interchangeable
options; a scanner matches this project's existing preference for a mechanical signal over
spending a model call where regex/known-format patterns already answer the question
reliably (same reasoning `curate.score_content_quality`, step 69, already used) — and means
PII/credential content is never sent to a third-party model in the first place, since this
runs before the Classifier's own LLM call (`ingestion.classify_source`).

Scoped to four categories, confirmed with the user rather than assumed: `ssn`, `credit_card`
(Luhn-validated, to cut the false-positive rate a bare digit-count regex alone would have),
`credential` (a `password`/`passwd`/`pwd`-labeled assignment), and `secret_key` (a
well-known provider key format, a PEM private-key header, or an
`api_key`/`secret_key`/`access_token`-labeled assignment). Email addresses and phone
numbers are a deliberate, documented exclusion — both are ubiquitous in ordinary business
writing (signatures, on-call contact info) and would false-positive block routine documents
far too often from a bare regex to be a useful hard-block signal; a real NER-based scanner
would be needed to do better, a materially larger addition than this step takes on.
"""

import re

_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# 13-19 digits, optionally space/dash-separated (matches every major card network's length
# range) — a Luhn check on the digits below is what actually decides `credit_card`, since
# the bare shape alone matches far too much incidental content (phone numbers, tracking
# numbers, order ids) to mean anything on its own.
_CREDIT_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

_CREDENTIAL = re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*\S+")

_PRIVATE_KEY_HEADER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
# Well-known provider key formats — high-precision by construction (a fixed prefix +
# length), unlike a generic "looks random" entropy check, which this scanner doesn't do.
_AWS_ACCESS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GITHUB_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b")
_SLACK_TOKEN = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")
_LABELED_SECRET = re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*\S{8,}")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _contains_credit_card(text: str) -> bool:
    for match in _CREDIT_CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"[ -]", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return True
    return False


def detect_pii(text: str) -> list[str]:
    """Category names found, sorted, empty if none — `ssn`, `credit_card`, `credential`,
    `secret_key`."""
    categories = set()
    if _SSN.search(text):
        categories.add("ssn")
    if _contains_credit_card(text):
        categories.add("credit_card")
    if _CREDENTIAL.search(text):
        categories.add("credential")
    if (
        _PRIVATE_KEY_HEADER.search(text)
        or _AWS_ACCESS_KEY.search(text)
        or _GITHUB_TOKEN.search(text)
        or _SLACK_TOKEN.search(text)
        or _LABELED_SECRET.search(text)
    ):
        categories.add("secret_key")
    return sorted(categories)
