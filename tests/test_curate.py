"""Curator decisions (03 §6) — pure, no database or network."""

from datetime import datetime, timezone

import pytest

from karpwiki import curate
from karpwiki.curate import CuratedContent, CuratedPage, ExistingPage


def test_slugify_produces_a_url_safe_path_fragment():
    assert curate.slugify("Retry & Backoff Policy") == "retry-backoff-policy"
    assert curate.slugify("  spaced  out  ") == "spaced-out"


def test_slugify_never_returns_empty():
    assert curate.slugify("!!!") == "page"


def test_match_existing_is_case_and_whitespace_insensitive():
    existing = [ExistingPage(page_id="p1", title="Retry Backoff", path="concepts/retry-backoff.md")]
    match = curate.match_existing("  retry backoff  ", existing)
    assert match is not None
    assert match.page_id == "p1"


def test_match_existing_returns_none_for_a_genuinely_new_title():
    existing = [ExistingPage(page_id="p1", title="Retry Backoff", path="x")]
    assert curate.match_existing("Payments Ledger", existing) is None


def test_source_body_includes_the_citation_footnote():
    content = CuratedContent(
        source_title="Restarting Payments",
        source_description="How to restart the payments worker.",
        source_summary="The worker drains its queue before restart.",
        source_key_points=["Drain the queue.", "Verify lag returns to zero."],
    )
    body = curate.render_source_body(content, filename="runbook.md")

    assert "[^1]" in body
    assert "[^1]: runbook.md" in body
    assert "Drain the queue." in body


def test_overview_body_lists_recent_sources_and_counts():
    body = curate.render_overview_body(
        source_count=3,
        page_count=12,
        recent=[("Restarting Payments", "How to restart.", "sources/abc.md")],
    )
    assert "Sources ingested: 3" in body
    assert "Pages: 12" in body
    assert "Restarting Payments" in body
    assert "sources/abc.md" in body


def test_overview_body_handles_no_sources_yet():
    body = curate.render_overview_body(source_count=0, page_count=0, recent=[])
    assert "(none yet)" in body


def test_overview_body_caps_at_the_recent_limit():
    recent = [(f"Page {i}", "d", f"p{i}.md") for i in range(curate.OVERVIEW_RECENT_LIMIT + 5)]
    body = curate.render_overview_body(source_count=1, page_count=1, recent=recent)
    assert body.count("- **Page") == curate.OVERVIEW_RECENT_LIMIT


def test_index_body_lists_each_category_with_a_linked_entry():
    body = curate.render_index_body(
        concepts=[("Retry and Backoff", "How services retry.", "concepts/retry-backoff.md")],
        entities=[("Payments Worker", "The worker service.", "entities/payments-worker.md")],
        sources=[("Runbook", "A runbook.", "sources/abc.md")],
        comparisons=[],
    )
    assert "## Concepts" in body
    assert "[Retry and Backoff](concepts/retry-backoff.md) — How services retry." in body
    assert "## Entities" in body
    assert "[Payments Worker](entities/payments-worker.md) — The worker service." in body
    assert "## Sources" in body
    assert "[Runbook](sources/abc.md) — A runbook." in body
    assert "## Comparisons" in body
    assert "(none yet)" in body


def test_index_body_handles_every_category_empty():
    body = curate.render_index_body(concepts=[], entities=[], sources=[], comparisons=[])
    assert body.count("(none yet)") == 4


def test_log_body_renders_newest_first_as_given():
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    body = curate.render_log_body([(now, "Ingested `runbook.md` → 4 page(s) touched")])
    assert "runbook.md" in body
    assert "4 page(s)" in body


def test_log_body_handles_no_entries_yet():
    assert "(none yet)" in curate.render_log_body([])


def test_curated_page_requires_at_least_two_tags():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CuratedPage(page_type="concept", title="X", tags=["one"], body="body")


def test_structured_source_body_renders_the_structure_table():
    from karpwiki.curate import StructuredCuratedContent, StructuredField

    content = StructuredCuratedContent(
        source_title="Payments Config",
        intent_statement="Defines retry/backoff parameters for the Payments connector.",
        fields=[
            StructuredField(name="max_retries", type="int", description="Retry ceiling."),
            StructuredField(name="backoff_ms", type="int", description=None),
        ],
    )
    body = curate.render_structured_source_body(
        content, filename="payments.yaml", artifact_identity="payments-config", source_version="2.1"
    )
    assert "Defines retry/backoff parameters" in body
    assert "## Structure" in body
    assert "| max_retries | int | Retry ceiling. |" in body
    assert "| backoff_ms | int |  |" in body
    assert "## Provenance" in body
    assert "payments.yaml" in body
    assert "`payments-config`" in body
    assert "`2.1`" in body
    assert "[^1]: payments.yaml" in body


def test_structured_source_body_handles_no_fields_extracted():
    from karpwiki.curate import StructuredCuratedContent

    content = StructuredCuratedContent(source_title="Empty Config", intent_statement="Does very little.")
    body = curate.render_structured_source_body(
        content, filename="empty.json", artifact_identity=None, source_version=None
    )
    assert "(no fields extracted)" in body
    assert "Artifact identity" not in body
    assert "Version:" not in body


def test_structured_field_requires_a_name():
    import pytest
    from pydantic import ValidationError

    from karpwiki.curate import StructuredField

    with pytest.raises(ValidationError):
        StructuredField(name="")


def test_structured_curated_content_requires_an_intent_statement():
    import pytest
    from pydantic import ValidationError

    from karpwiki.curate import StructuredCuratedContent

    with pytest.raises(ValidationError):
        StructuredCuratedContent(source_title="X", intent_statement="")


# --- Content quality scoring (07 §4, phase3-tasklist.md step 69) -------------------------


def test_score_content_quality_zero_citations_and_links_is_zero():
    body = "Plain prose with no citations and no internal links at all here."
    score = curate.score_content_quality(body)
    assert score.citation_density == 0.0
    assert score.cross_reference_completeness == 0.0
    assert score.combined == 0.0


def test_score_content_quality_caps_citation_density_at_one():
    # 100-word body with 5 citation footnotes, target is 3 per 1000 words — way over.
    body = " ".join(["word"] * 100) + "\n" + "\n".join(f"[^{i}]: def {i}" for i in range(5))
    score = curate.score_content_quality(body, citation_target_per_1000_words=3.0)
    assert score.citation_density == 1.0


def test_score_content_quality_caps_cross_reference_completeness_at_one():
    body = "See " + " ".join(f"[link{i}](concepts/other{i}.md)" for i in range(10)) + "."
    score = curate.score_content_quality(body, cross_reference_target=3)
    assert score.cross_reference_completeness == 1.0


def test_score_content_quality_combined_is_the_mean():
    # One citation footnote (~1000 body words) and one of two target links.
    body = "word " * 1000 + "\n[^1]: a definition\n\nSee [x](concepts/x.md)."
    score = curate.score_content_quality(
        body, citation_target_per_1000_words=2.0, cross_reference_target=2
    )
    assert score.citation_density == pytest.approx(0.5, abs=0.01)
    assert score.cross_reference_completeness == 0.5
    assert score.combined == round((score.citation_density + score.cross_reference_completeness) / 2, 4)


def test_freshness_score_is_one_at_zero_age():
    assert curate.freshness_score(0) == 1.0


def test_freshness_score_is_half_at_the_half_life():
    assert curate.freshness_score(180, half_life_days=180) == 0.5


def test_freshness_score_decays_toward_zero_as_age_grows():
    near = curate.freshness_score(30, half_life_days=180)
    far = curate.freshness_score(360, half_life_days=180)
    assert 0.0 < far < near < 1.0
