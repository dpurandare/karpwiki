"""PII detection (07 §2, phase3-tasklist.md step 71) — pure, no database or network."""

from karpwiki import pii


def test_detects_ssn():
    assert pii.detect_pii("SSN: 123-45-6789") == ["ssn"]


def test_no_categories_for_plain_text():
    assert pii.detect_pii("Drain the queue, then restart the deployment.") == []


def test_detects_a_luhn_valid_credit_card():
    # A real Visa test number (Luhn-valid).
    assert pii.detect_pii("Card: 4111 1111 1111 1111") == ["credit_card"]


def test_does_not_flag_a_luhn_invalid_digit_run():
    """A bare 13-19 digit shape alone must not be enough — phone numbers, tracking
    numbers, and order ids all incidentally match it; only a passing Luhn check counts."""
    assert pii.detect_pii("Tracking number: 1234 5678 9012 3456") == []


def test_detects_a_password_assignment():
    assert pii.detect_pii("password: hunter2") == ["credential"]
    assert pii.detect_pii("passwd=s3cr3t!!") == ["credential"]


def test_detects_an_aws_access_key():
    assert pii.detect_pii("Key: AKIAIOSFODNN7EXAMPLE") == ["secret_key"]


def test_detects_a_github_token():
    assert pii.detect_pii("token: ghp_" + "a" * 36) == ["secret_key"]


def test_detects_a_private_key_header():
    assert pii.detect_pii("-----BEGIN RSA PRIVATE KEY-----\nMIIB...") == ["secret_key"]


def test_detects_a_labeled_generic_secret():
    assert pii.detect_pii("api_key: sk_live_abcdefgh12345678") == ["secret_key"]


def test_email_and_phone_are_not_detected():
    """Deliberate scope exclusion (07 §2) — both are ubiquitous in ordinary business
    writing and would false-positive block routine documents from a bare regex."""
    assert pii.detect_pii("Contact ops@example.com or call 555-123-4567.") == []


def test_multiple_categories_are_all_reported_sorted():
    text = "SSN: 123-45-6789\npassword: hunter2"
    assert pii.detect_pii(text) == ["credential", "ssn"]
