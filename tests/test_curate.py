"""Curator decisions (03 §6) — pure, no database or network."""

from datetime import datetime, timezone

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


def test_log_body_renders_newest_first_as_given():
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    body = curate.render_log_body([(now, "runbook.md", 4)])
    assert "runbook.md" in body
    assert "4 page(s)" in body


def test_log_body_handles_no_entries_yet():
    assert "(none yet)" in curate.render_log_body([])


def test_curated_page_requires_at_least_two_tags():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CuratedPage(page_type="concept", title="X", tags=["one"], body="body")
