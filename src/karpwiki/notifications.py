"""Notification Service (09 §13, 01 §1, phase2-tasklist.md step 55; phase3-tasklist.md step
67) — a pluggable provider interface, same "protocol + factory + one real default" shape
already used twice (`auth.py`'s `Authenticator`, `secrets_manager.py`'s `SecretResolver`).

Step 55 (Phase 2) built the one real hook that existed then: connector auth failure. Step 67
adds the two triggers `07` §3 names beyond it — admin alerts on aging review items and SLA
breaches (`05` §8: "threshold breaches... feed the Notification Service"), and submitter
outcome notifications (ingested, rejected, merged as a duplicate) — plus a second, real
`NotificationSink` implementation.

**Delivery is webhook-only, not also email.** `07` §3 says "email and/or chat-platform
webhook"; a plain HTTP POST to a configured URL (Slack-style incoming webhook, or any
generic JSON receiver) is genuinely deliverable today. Real email delivery is not: no
`Principal` anywhere in this schema has a stored email address — `access_policy.principal`
is an opaque string (`user:<id>`, `group:<id>`, `connector:<connector_id>`) with no directory
behind it. Building one would be a new, unplanned contact-info concept, not a swap-in sink —
flagged here as a real prerequisite gap rather than faked with an unaddressable send.

**A submitter-outcome notification fires only for the direct, single-source paths** (a fresh
submission reaching `ingested` via `tasks._curate`, a `duplicate` item's `reject`/`merge`
resolution) — not for the Stuck-Pipeline Sweep Detector's batched `abort` (phase3-tasklist.md
step 64), which rejects a *set* of sources for an operational reason, not a content judgment
about any one submitter's document. Named as a deliberate scope boundary, not a miss.

**The SLA-breach sweep re-alerts every run while a breach is still open, rather than
suppressing repeats until it resolves** — confirmed with the user: this matches how
`monitoring.py`'s own dashboards already work (a live snapshot, re-read each time, not a
one-shot event) and how most real alerting systems behave (a condition that's still true
keeps firing). Suppression would need a new "already notified" tracking table this step
doesn't build.
"""

import logging
from typing import Protocol

import httpx

from . import config
from .models import Connector

logger = logging.getLogger(__name__)


class NotificationSink(Protocol):
    """One method per real trigger this codebase actually has a caller for — not a
    speculative general `notify(event_type, **kwargs)` API (09 §13's own reasoning,
    unchanged since step 55)."""

    async def notify_connector_auth_failure(self, connector: Connector, message: str) -> None: ...

    async def notify_review_sla_breach(
        self, *, workspace_id: str | None, kind: str, count: int, oldest_age_hours: float
    ) -> None: ...

    async def notify_search_latency_sla_breach(
        self, *, workspace_id: str | None, p95_ms: float, sla_ms: float
    ) -> None: ...

    async def notify_source_ingested(
        self, *, submitted_by: str, filename: str, source_id: str
    ) -> None: ...

    async def notify_source_rejected(
        self, *, submitted_by: str, filename: str, source_id: str, reason: str
    ) -> None: ...

    async def notify_source_merged(
        self, *, submitted_by: str, filename: str, source_id: str, target_page_path: str
    ) -> None: ...


class LogNotificationSink:
    """The default when no webhook is configured: a structured log line an operator's log
    aggregation would see. Not a stand-in the way `TrustedHeaderAuthenticator` is — until
    step 55 there was no real Phase 2 backend to stand in *for*; this is genuinely what
    "surfaces via... the Notification Service" meant before a webhook URL existed."""

    async def notify_connector_auth_failure(self, connector: Connector, message: str) -> None:
        logger.warning(
            "connector auth failure: connector_id=%s workspace_id=%s type=%s message=%s",
            connector.connector_id,
            connector.workspace_id,
            connector.type,
            message,
        )

    async def notify_review_sla_breach(
        self, *, workspace_id: str | None, kind: str, count: int, oldest_age_hours: float
    ) -> None:
        logger.warning(
            "review SLA breach: workspace_id=%s kind=%s open_past_sla=%d oldest_age_hours=%.2f",
            workspace_id,
            kind,
            count,
            oldest_age_hours,
        )

    async def notify_search_latency_sla_breach(
        self, *, workspace_id: str | None, p95_ms: float, sla_ms: float
    ) -> None:
        logger.warning(
            "search latency SLA breach: workspace_id=%s p95_ms=%.1f sla_ms=%.1f",
            workspace_id,
            p95_ms,
            sla_ms,
        )

    async def notify_source_ingested(
        self, *, submitted_by: str, filename: str, source_id: str
    ) -> None:
        logger.info(
            "source ingested: source_id=%s submitted_by=%s filename=%s",
            source_id,
            submitted_by,
            filename,
        )

    async def notify_source_rejected(
        self, *, submitted_by: str, filename: str, source_id: str, reason: str
    ) -> None:
        logger.info(
            "source rejected: source_id=%s submitted_by=%s filename=%s reason=%s",
            source_id,
            submitted_by,
            filename,
            reason,
        )

    async def notify_source_merged(
        self, *, submitted_by: str, filename: str, source_id: str, target_page_path: str
    ) -> None:
        logger.info(
            "source merged: source_id=%s submitted_by=%s filename=%s target_page_path=%s",
            source_id,
            submitted_by,
            filename,
            target_page_path,
        )


class WebhookNotificationSink:
    """Real delivery (phase3-tasklist.md step 67): one JSON POST per event to
    `KARPWIKI_NOTIFICATION_WEBHOOK_URL` — `{"event": "<name>", ...fields}`, the shape any
    generic webhook receiver (a Slack incoming webhook proxy, an internal ops endpoint) can
    consume. One shared channel, not per-recipient delivery — there is nowhere to address a
    message to a specific principal (see module docstring), so every event names the
    relevant principal/workspace/source *in the payload* for whoever's watching the channel
    to read, rather than pretending to route it to them directly.

    `http_client` is injectable for tests; built fresh per instance otherwise, never at
    module scope (`auth.OidcAuthenticator`'s own established reasoning: an event-loop-bound
    client can't safely be a module-level singleton, 09 §29)."""

    def __init__(self, *, url: str, http_client: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._http = http_client or httpx.AsyncClient(timeout=config.NOTIFICATION_WEBHOOK_TIMEOUT_SECONDS)

    async def _post(self, event: str, **fields) -> None:
        try:
            resp = await self._http.post(self._url, json={"event": event, **fields})
            resp.raise_for_status()
        except httpx.HTTPError:
            # A delivery failure is not the caller's own operation failing — logged and
            # swallowed, same as any other best-effort notification; the log line this
            # falls back to is exactly `LogNotificationSink`'s own line for the event.
            logger.exception("notification webhook delivery failed for event=%s", event)

    async def notify_connector_auth_failure(self, connector: Connector, message: str) -> None:
        await self._post(
            "connector_auth_failure",
            connector_id=str(connector.connector_id),
            workspace_id=connector.workspace_id,
            type=connector.type,
            message=message,
        )

    async def notify_review_sla_breach(
        self, *, workspace_id: str | None, kind: str, count: int, oldest_age_hours: float
    ) -> None:
        await self._post(
            "review_sla_breach",
            workspace_id=workspace_id,
            kind=kind,
            open_past_sla=count,
            oldest_age_hours=oldest_age_hours,
        )

    async def notify_search_latency_sla_breach(
        self, *, workspace_id: str | None, p95_ms: float, sla_ms: float
    ) -> None:
        await self._post(
            "search_latency_sla_breach", workspace_id=workspace_id, p95_ms=p95_ms, sla_ms=sla_ms
        )

    async def notify_source_ingested(
        self, *, submitted_by: str, filename: str, source_id: str
    ) -> None:
        await self._post(
            "source_ingested", source_id=source_id, submitted_by=submitted_by, filename=filename
        )

    async def notify_source_rejected(
        self, *, submitted_by: str, filename: str, source_id: str, reason: str
    ) -> None:
        await self._post(
            "source_rejected",
            source_id=source_id,
            submitted_by=submitted_by,
            filename=filename,
            reason=reason,
        )

    async def notify_source_merged(
        self, *, submitted_by: str, filename: str, source_id: str, target_page_path: str
    ) -> None:
        await self._post(
            "source_merged",
            source_id=source_id,
            submitted_by=submitted_by,
            filename=filename,
            target_page_path=target_page_path,
        )


def default_notification_sink() -> NotificationSink:
    """Every caller's default. Swaps to `WebhookNotificationSink` the moment
    `KARPWIKI_NOTIFICATION_WEBHOOK_URL` is set, with no change to any caller — the same
    swap-on-config-presence property `default_authenticator()` already proved out for OIDC."""
    if config.NOTIFICATION_WEBHOOK_URL:
        return WebhookNotificationSink(url=config.NOTIFICATION_WEBHOOK_URL)
    return LogNotificationSink()
