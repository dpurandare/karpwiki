"""Connector polling — fetch, diff, create a `raw_source` (02 §3, 09 §4, phase2-tasklist.md
step 52). The generic run orchestration only: fetch/diff logic is entirely
connector-type-specific (09 §4) and lives in a separate module per type — `connectors_git.py`
(step 54) is the first, registering itself into `ADAPTERS` below when imported.

09 §4's three steps, per scheduled run: fetch the connector's current state listing, diff
against `Connector.last_sync_cursor`, and for each new/changed item create a `raw_source`
exactly as if a user had uploaded it ("From step 3 onward the item is indistinguishable
from any other submission"). This module owns steps 1-3's orchestration and the
create-raw_source call; an `Adapter` owns steps 1-2's connector-type-specific mechanics —
diffing against the cursor is the adapter's job, not this module's, since only the adapter
understands its own cursor shape (a git SHA vs. a page-id/timestamp set are nothing alike).

Credential handling (phase2-tasklist.md step 53): `connector.credential_ref` is resolved
into the real secret via `secrets_manager.SecretResolver` at the start of each run (09 §13)
— the adapter never sees the ref, only the resolved value, held in memory for this one call
only and never persisted or logged.

Auth failure (phase2-tasklist.md step 55): flips the connector to `disabled_auth` and calls
`notifications.NotificationSink.notify_connector_auth_failure` — 09 §13's "surfaces via...
the Notification Service," the pluggable hook `notifications.py` builds.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from . import ingestion, notifications, secrets_manager
from .models import Connector, ConnectorState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredItem:
    """One new/changed item an adapter's `poll` found, already diffed against the cursor
    the adapter itself was given — ready to become a `raw_source` unchanged."""

    filename: str
    content: bytes


class ConnectorAuthError(Exception):
    """An adapter raises this specifically for an authentication failure — distinct from
    any other fetch error, since 09 §13 gives auth failure its own outcome (`disabled_auth`,
    no retry on the normal schedule) rather than treating it like a transient error."""


class ConnectorAdapter(Protocol):
    """One implementation per `Connector.type` (e.g. `"git"`, step 54). Registered into
    `ADAPTERS` below by whichever step adds it — mirrors `auth.py`'s pluggable
    `Authenticator` pattern, per 09 §13's own note that credential resolution follows that
    same shape (`secrets_manager.py`, step 53)."""

    async def poll(
        self, connector: Connector, credential: str | None
    ) -> tuple[list[DiscoveredItem], dict]:
        """Fetch the current state listing, diff against `connector.last_sync_cursor`
        (`connector.last_sync_cursor`, not a separate argument — the adapter reads it off
        the row it's given), and return `(new_or_changed_items, new_cursor)`. `credential`
        is the real, already-resolved secret (`secrets_manager.SecretResolver`, step 53) —
        never the opaque `credential_ref` pointer, and never held onto past this call.
        Raise `ConnectorAuthError` for an authentication failure; any other exception is
        treated as a transient fetch error that simply retries on the connector's next
        scheduled run."""
        ...


# Populated by each concrete adapter module importing itself for the side effect
# (`connectors_git.py`'s bottom line, step 54) — nothing here imports them eagerly, so a
# process that never imports `connectors_git` (e.g. a test not exercising it) sees no "git"
# entry. A connector whose `type` has no registered adapter is not an error state — `type`
# is deliberately open-ended (step 51) and nothing stops an admin from configuring one ahead
# of its adapter landing.
ADAPTERS: dict[str, ConnectorAdapter] = {}


async def poll_connector(
    session: AsyncSession,
    *,
    connector: Connector,
    secret_resolver: secrets_manager.SecretResolver = secrets_manager.default_secret_resolver(),
    notification_sink: notifications.NotificationSink | None = None,
) -> list[uuid.UUID]:
    """One connector's scheduled run (09 §4). Always updates `last_run_at`/
    `last_run_detail`, whatever the outcome — a poll that finds nothing new is still a
    completed run, not a no-op the operator has no record of. Returns the `raw_source` ids
    created this run (empty unless the outcome is `"ok"`), for the caller to dispatch
    classification against after commit.

    `secret_resolver` is a real, callable default (mirrors `ingestion.py`'s `call:
    ClassifierCall = call_model` pattern) — `EnvSecretResolver` holds no connection or
    client, so constructing it once at import time is safe. `notification_sink` is NOT a
    bare default for the same reason anymore, since phase3-tasklist.md step 67: a real
    `WebhookNotificationSink` holds an `httpx.AsyncClient` bound to whichever event loop is
    running when it's built, and this function's own callers each run inside their own
    fresh-per-call `asyncio.run()` loop (09 §34's cross-event-loop bug) — resolved fresh
    inside the function body instead."""
    sink = notification_sink or notifications.default_notification_sink()
    adapter = ADAPTERS.get(connector.type)
    if adapter is None:
        connector.last_run_at = datetime.now(UTC)
        connector.last_run_detail = {
            "outcome": "unsupported_type",
            "items_discovered": 0,
            "message": f"no adapter registered for type {connector.type!r}",
        }
        await session.flush()
        return []

    try:
        credential = None
        if connector.credential_ref is not None:
            try:
                credential = await secret_resolver.resolve(connector.credential_ref)
            except secrets_manager.SecretNotFoundError as exc:
                # Can't possibly authenticate without ever obtaining the credential —
                # the same outcome as the adapter itself rejecting a bad one (09 §13).
                raise ConnectorAuthError(str(exc)) from exc
        items, new_cursor = await adapter.poll(connector, credential)
    except ConnectorAuthError as exc:
        # 09 §13: an auth failure disables rather than retries — a connector repeatedly
        # hitting an expired credential on its own poll interval is the usual way an
        # integration account gets locked out at the source system.
        connector.state = ConnectorState.disabled_auth
        connector.last_run_at = datetime.now(UTC)
        connector.last_run_detail = {"outcome": "auth_failed", "items_discovered": 0, "message": str(exc)}
        await session.flush()
        # 09 §13/step 55: surfaces via the Notification Service, alongside the Admin
        # Console operational-health signal `last_run_detail` above already is.
        await sink.notify_connector_auth_failure(connector, str(exc))
        return []
    except Exception as exc:
        logger.warning("connector %s poll failed: %s", connector.connector_id, exc)
        connector.last_run_at = datetime.now(UTC)
        connector.last_run_detail = {"outcome": "error", "items_discovered": 0, "message": str(exc)}
        await session.flush()
        return []

    # `ingestion.store` now rejects content `doc_extract` can't read at all (found live
    # during Phase 3 step 62 prep) — `connectors_git.py`'s own adapter already filters
    # these out before they ever become a `DiscoveredItem`, but a future adapter type
    # might not, so this run skips a rejected item rather than losing every other item
    # discovered in the same poll to one bad file.
    sources = []
    skipped = 0
    for item in items:
        try:
            sources.append(
                await ingestion.store(
                    session,
                    item.content,
                    item.filename,
                    submitted_by=f"connector:{connector.connector_id}",
                )
            )
        except ingestion.UnsupportedContentError:
            logger.warning(
                "connector %s: skipping %r, unsupported content",
                connector.connector_id,
                item.filename,
            )
            skipped += 1

    connector.last_sync_cursor = new_cursor
    connector.last_run_at = datetime.now(UTC)
    connector.last_run_detail = {
        "outcome": "ok",
        "items_discovered": len(items),
        "items_skipped": skipped,
        "message": None,
    }
    await session.flush()
    return [source.source_id for source in sources]
