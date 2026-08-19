"""Notification Service hook (09 §13, 01 §1, phase2-tasklist.md step 55) — a pluggable
provider interface, same "protocol + factory + one real default" shape already used twice
(`auth.py`'s `Authenticator`, `secrets_manager.py`'s `SecretResolver`).

The Notification Service's *full delivery mechanics* (email/chat-platform webhook,
07 §6's own wording) are explicitly out of scope for Phase 2 — phase2-tasklist.md's own
header lists them under "Explicitly excluded... Phase 3+." What this module builds instead
is the real call-out point 09 §13 asks for: when a connector's credential fails to
authenticate, something concrete happens (`LogNotificationSink`'s structured log line) at
exactly the moment `01` §1's architecture diagram says a Notification Service — its own,
separate Core Service — would be told. A deployment with a real notification backend
implements `NotificationSink` and swaps it in via `default_notification_sink()`, with no
change to `connector_polling.py`.

Scoped narrowly to this one trigger (`notify_connector_auth_failure`), not a speculative
general `notify(event_type, **kwargs)` API — 07 §6 names other triggers (aging review
items, SLA breaches, submitter outcomes) that no tasklist step asks this module to handle
yet, and building for those now would be guessing ahead of the step that actually needs
them.
"""

import logging
from typing import Protocol

from .models import Connector

logger = logging.getLogger(__name__)


class NotificationSink(Protocol):
    """One method, matching this step's one real trigger — 09 §13's connector auth-failure
    surfacing, not a general notification API."""

    async def notify_connector_auth_failure(self, connector: Connector, message: str) -> None: ...


class LogNotificationSink:
    """The one concrete provider this step builds: a structured log line an operator's log
    aggregation would see. Not a stand-in the way `TrustedHeaderAuthenticator` is — there is
    no real Phase 2 notification backend to be a stand-in *for*; this is genuinely what
    "surfaces via... the Notification Service" means until Phase 3 builds real delivery."""

    async def notify_connector_auth_failure(self, connector: Connector, message: str) -> None:
        logger.warning(
            "connector auth failure: connector_id=%s workspace_id=%s type=%s message=%s",
            connector.connector_id,
            connector.workspace_id,
            connector.type,
            message,
        )


def default_notification_sink() -> NotificationSink:
    """`connector_polling.poll_connector`'s default — always `LogNotificationSink` today,
    the same one-provider shape `default_secret_resolver()` started with (step 53)."""
    return LogNotificationSink()
