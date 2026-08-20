"""Notification Service (09 §13, phase2-tasklist.md step 55; phase3-tasklist.md step 67)."""

import json
import logging

import httpx

from karpwiki import notifications
from karpwiki.models import Connector


async def test_log_notification_sink_logs_a_structured_warning(caplog):
    connector = Connector(type="git", workspace_id="ws-1")
    sink = notifications.LogNotificationSink()

    with caplog.at_level(logging.WARNING):
        await sink.notify_connector_auth_failure(connector, "expired token")

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert "expired token" in record.getMessage()
    assert str(connector.connector_id) in record.getMessage()
    assert "ws-1" in record.getMessage()
    assert "git" in record.getMessage()


def test_default_notification_sink_is_log_based():
    assert isinstance(notifications.default_notification_sink(), notifications.LogNotificationSink)


def test_default_notification_sink_swaps_to_webhook_when_configured(monkeypatch):
    """Same swap-on-config-presence property `default_authenticator()` already proved out
    for OIDC (09 §15, step 47) — no caller changes needed either way."""
    monkeypatch.setattr(notifications.config, "NOTIFICATION_WEBHOOK_URL", "https://ops.example.com/hook")
    sink = notifications.default_notification_sink()
    assert isinstance(sink, notifications.WebhookNotificationSink)


# --- LogNotificationSink — the new step-67 trigger points ---------------------------------


async def test_log_sink_review_sla_breach(caplog):
    with caplog.at_level(logging.WARNING):
        await notifications.LogNotificationSink().notify_review_sla_breach(
            workspace_id="ws-1", kind="classification", count=3, oldest_age_hours=12.5
        )
    [record] = caplog.records
    assert "ws-1" in record.getMessage()
    assert "classification" in record.getMessage()


async def test_log_sink_search_latency_sla_breach(caplog):
    with caplog.at_level(logging.WARNING):
        await notifications.LogNotificationSink().notify_search_latency_sla_breach(
            workspace_id=None, p95_ms=1500.0, sla_ms=1000.0
        )
    [record] = caplog.records
    assert "1500" in record.getMessage()


async def test_log_sink_source_ingested(caplog):
    with caplog.at_level(logging.INFO):
        await notifications.LogNotificationSink().notify_source_ingested(
            submitted_by="user:deepak", filename="runbook.md", source_id="src-1"
        )
    [record] = caplog.records
    assert "user:deepak" in record.getMessage()
    assert "runbook.md" in record.getMessage()


async def test_log_sink_source_rejected(caplog):
    with caplog.at_level(logging.INFO):
        await notifications.LogNotificationSink().notify_source_rejected(
            submitted_by="user:deepak", filename="runbook.md", source_id="src-1", reason="duplicate"
        )
    [record] = caplog.records
    assert "duplicate" in record.getMessage()


async def test_log_sink_source_merged(caplog):
    with caplog.at_level(logging.INFO):
        await notifications.LogNotificationSink().notify_source_merged(
            submitted_by="user:deepak",
            filename="runbook.md",
            source_id="src-1",
            target_page_path="concepts/runbook.md",
        )
    [record] = caplog.records
    assert "concepts/runbook.md" in record.getMessage()


# --- WebhookNotificationSink — real HTTP delivery, via httpx.MockTransport ----------------


def _webhook(handler) -> notifications.WebhookNotificationSink:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return notifications.WebhookNotificationSink(url="https://ops.example.com/hook", http_client=client)


async def test_webhook_sink_posts_connector_auth_failure():
    posted = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        posted.update(json.loads(request.content))
        return httpx.Response(200)

    connector = Connector(type="git", workspace_id="ws-1")
    await _webhook(handler).notify_connector_auth_failure(connector, "expired token")
    assert posted["event"] == "connector_auth_failure"
    assert posted["workspace_id"] == "ws-1"
    assert posted["message"] == "expired token"


async def test_webhook_sink_posts_review_sla_breach():
    posted = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        posted.update(json.loads(request.content))
        return httpx.Response(200)

    await _webhook(handler).notify_review_sla_breach(
        workspace_id="ws-1", kind="duplicate", count=2, oldest_age_hours=9.0
    )
    assert posted == {
        "event": "review_sla_breach",
        "workspace_id": "ws-1",
        "kind": "duplicate",
        "open_past_sla": 2,
        "oldest_age_hours": 9.0,
    }


async def test_webhook_sink_posts_source_ingested():
    posted = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        posted.update(json.loads(request.content))
        return httpx.Response(200)

    await _webhook(handler).notify_source_ingested(
        submitted_by="user:deepak", filename="runbook.md", source_id="src-1"
    )
    assert posted == {
        "event": "source_ingested",
        "source_id": "src-1",
        "submitted_by": "user:deepak",
        "filename": "runbook.md",
    }


async def test_webhook_sink_posts_source_merged():
    posted = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        posted.update(json.loads(request.content))
        return httpx.Response(200)

    await _webhook(handler).notify_source_merged(
        submitted_by="user:deepak",
        filename="runbook.md",
        source_id="src-1",
        target_page_path="concepts/runbook.md",
    )
    assert posted["event"] == "source_merged"
    assert posted["target_page_path"] == "concepts/runbook.md"


async def test_webhook_sink_swallows_a_delivery_failure(caplog):
    """A webhook that's down/erroring must not raise into the caller — this is a
    best-effort side channel, not something that should fail the real operation it's
    reporting on."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with caplog.at_level(logging.ERROR):
        await _webhook(handler).notify_source_ingested(
            submitted_by="user:deepak", filename="runbook.md", source_id="src-1"
        )
    assert any("delivery failed" in r.getMessage() for r in caplog.records)
