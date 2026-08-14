"""Classification decisions (03 §3) — pure, no database or network."""

import pytest

from karpwiki import classify
from karpwiki.classify import ClassificationResult, LexicalMatch
from karpwiki.models import ContentShape

TAXONOMY = ["eng.design-doc", "eng.runbook", "policy.hr"]


@pytest.mark.parametrize(
    "filename,payload,expected",
    [
        ("notes.md", b"# Retry policy\n\nUse exponential backoff.", ContentShape.narrative),
        ("schema.json", b'{"name": "payments", "version": "2.1"}', ContentShape.structured_data),
        ("config.yaml", b"name: payments\nversion: 2.1\n", ContentShape.structured_data),
        # No extension, but the body is unambiguously data.
        ("export", b'{"a": 1, "b": [2, 3]}', ContentShape.structured_data),
        # A PDF's bytes do not decode; that is narrative, not a parse failure.
        ("scan.pdf", b"%PDF-1.7\x00\x80\xff binary", ContentShape.narrative),
        ("empty.txt", b"", ContentShape.narrative),
        # Prose whose lines contain colons parses as a YAML mapping, and csv.Sniffer
        # finds delimiters in sentences. Neither may be read as data.
        ("minutes.md", b"Note: see below\nOwner: platform team", ContentShape.narrative),
        ("readme.txt", b"Retry, backoff, and jitter are three things.", ContentShape.narrative),
    ],
)
def test_content_shape_is_determined_mechanically(filename, payload, expected):
    assert classify.detect_content_shape(filename, payload) == expected


def test_declared_identity_beats_the_filename():
    identity, version = classify.derive_artifact_identity(
        "dump-2026.json", b'{"name": "payments-api", "version": "2.1"}', ContentShape.structured_data
    )
    assert identity == "payments-api"
    assert version == "2.1"


def test_filename_is_the_fallback_identity():
    identity, version = classify.derive_artifact_identity(
        "payments-api.yaml", b"retries: 3\nbackoff: 2\n", ContentShape.structured_data
    )
    assert identity == "payments-api"
    assert version is None


def test_narrative_sources_have_no_artifact_identity():
    assert classify.derive_artifact_identity("a.md", b"# hi", ContentShape.narrative) == (None, None)


def test_lexical_match_finds_the_label_in_the_text():
    match = classify.lexical_match("Runbook: restarting the eng payments worker", TAXONOMY)
    assert match == LexicalMatch(label="eng.runbook", score=1.0)


def test_lexical_match_is_absent_when_nothing_scores():
    assert classify.lexical_match("Minutes of the quarterly offsite", TAXONOMY) is None


def test_a_tie_is_reported_as_no_signal():
    """An ambiguous signal must not be passed off as agreement."""
    assert classify.lexical_match("eng eng", ["eng.alpha", "eng.beta"]) is None


def _result(label="eng.runbook", confidence=0.9):
    return ClassificationResult(summary="s", document_type=label, confidence=confidence)


def test_gate_accepts_when_confidence_and_cross_check_agree():
    routing = classify.route(
        _result(), LexicalMatch("eng.runbook", 1.0), min_confidence=0.75, document_types=TAXONOMY
    )
    assert routing.accepted
    assert routing.document_type == "eng.runbook"


def test_gate_accepts_when_the_lexical_signal_is_absent():
    """Absent is not disagreement — most documents will not name their own type."""
    routing = classify.route(_result(), None, min_confidence=0.75, document_types=TAXONOMY)
    assert routing.accepted


def test_gate_refuses_below_the_threshold():
    routing = classify.route(
        _result(confidence=0.4), None, min_confidence=0.75, document_types=TAXONOMY
    )
    assert not routing.accepted
    assert routing.reason == "confidence below threshold"


def test_gate_refuses_a_disagreement_however_confident_the_model_is():
    """The point of the cross-check: self-reported confidence cannot overrule it."""
    routing = classify.route(
        _result(confidence=0.99),
        LexicalMatch("policy.hr", 1.0),
        min_confidence=0.75,
        document_types=TAXONOMY,
    )
    assert not routing.accepted
    assert routing.reason == "lexical cross-check disagreed"
    assert set(routing.candidates) == {"eng.runbook", "policy.hr"}


def test_gate_refuses_a_label_outside_the_taxonomy():
    routing = classify.route(
        _result(label="invented.type"), None, min_confidence=0.75, document_types=TAXONOMY
    )
    assert not routing.accepted
    assert "outside the taxonomy" in routing.reason
