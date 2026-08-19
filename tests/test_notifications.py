"""Notification Service hook (09 §13, phase2-tasklist.md step 55)."""

import logging

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
