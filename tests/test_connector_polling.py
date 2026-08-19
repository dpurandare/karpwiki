"""Connector polling — fetch/diff/create-raw_source orchestration (09 §4, phase2-
tasklist.md step 52). `ADAPTERS` is empty in production until step 54; these tests
register a throwaway stub adapter directly rather than exercising a real connector type.
"""

import pytest

from karpwiki import connector_polling
from karpwiki.connector_polling import ConnectorAuthError, DiscoveredItem
from karpwiki.models import ConnectorState, RawSource


class _StubAdapter:
    def __init__(self, *, items=None, new_cursor=None, error=None):
        self._items = items or []
        self._new_cursor = new_cursor or {}
        self._error = error
        self.calls = []

    async def poll(self, connector, credential_ref):
        self.calls.append((connector.connector_id, credential_ref))
        if self._error is not None:
            raise self._error
        return self._items, self._new_cursor


@pytest.fixture
def registered_adapter(monkeypatch):
    def _register(adapter, type_="stub"):
        monkeypatch.setitem(connector_polling.ADAPTERS, type_, adapter)
        return adapter

    return _register


async def _connector(session, workspace, *, type="stub", **kwargs):
    from karpwiki import connectors as connectors_module

    return await connectors_module.create(session, workspace_id=workspace.workspace_id, type=type, **kwargs)


async def test_poll_creates_a_raw_source_per_discovered_item(session, workspace, registered_adapter):
    adapter = registered_adapter(
        _StubAdapter(
            items=[
                DiscoveredItem(filename="a.md", content=b"Item A"),
                DiscoveredItem(filename="b.md", content=b"Item B"),
            ],
            new_cursor={"sha": "abc123"},
        )
    )
    connector = await _connector(session, workspace)
    await session.commit()

    source_ids = await connector_polling.poll_connector(session, connector=connector)
    await session.commit()

    assert len(source_ids) == 2
    sources = [await session.get(RawSource, sid) for sid in source_ids]
    assert {s.filename for s in sources} == {"a.md", "b.md"}
    assert all(s.submitted_by == f"connector:{connector.connector_id}" for s in sources)
    assert adapter.calls == [(connector.connector_id, None)]


async def test_poll_updates_cursor_and_last_run_detail_on_success(session, workspace, registered_adapter):
    registered_adapter(
        _StubAdapter(items=[DiscoveredItem(filename="a.md", content=b"A")], new_cursor={"sha": "xyz"})
    )
    connector = await _connector(session, workspace)
    await session.commit()

    await connector_polling.poll_connector(session, connector=connector)
    await session.commit()
    await session.refresh(connector)

    assert connector.last_sync_cursor == {"sha": "xyz"}
    assert connector.last_run_at is not None
    assert connector.last_run_detail == {"outcome": "ok", "items_discovered": 1, "message": None}
    assert connector.state is ConnectorState.enabled


async def test_poll_passes_the_credential_ref_through_unresolved(session, workspace, registered_adapter):
    """Step 52 does not resolve credential_ref against a real secrets manager (step 53) —
    the adapter receives exactly the opaque pointer string stored on the connector."""
    adapter = registered_adapter(_StubAdapter())
    connector = await _connector(session, workspace, credential_ref="vault:kv/connectors/git-main")
    await session.commit()

    await connector_polling.poll_connector(session, connector=connector)

    assert adapter.calls == [(connector.connector_id, "vault:kv/connectors/git-main")]


async def test_poll_with_no_registered_adapter_is_not_an_error(session, workspace):
    connector = await _connector(session, workspace, type="not-yet-implemented")
    await session.commit()

    source_ids = await connector_polling.poll_connector(session, connector=connector)
    await session.commit()
    await session.refresh(connector)

    assert source_ids == []
    assert connector.state is ConnectorState.enabled  # not disabled — this isn't an auth failure
    assert connector.last_run_detail["outcome"] == "unsupported_type"
    assert connector.last_run_at is not None


async def test_poll_auth_failure_disables_the_connector(session, workspace, registered_adapter):
    registered_adapter(_StubAdapter(error=ConnectorAuthError("expired token")))
    connector = await _connector(session, workspace)
    await session.commit()

    source_ids = await connector_polling.poll_connector(session, connector=connector)
    await session.commit()
    await session.refresh(connector)

    assert source_ids == []
    assert connector.state is ConnectorState.disabled_auth
    assert connector.last_run_detail == {
        "outcome": "auth_failed",
        "items_discovered": 0,
        "message": "expired token",
    }


async def test_poll_generic_error_does_not_disable_the_connector(session, workspace, registered_adapter):
    """A transient fetch error (network blip, source-system 500) just retries on the
    connector's next scheduled run — only an auth failure disables (09 §13)."""
    registered_adapter(_StubAdapter(error=RuntimeError("source system timed out")))
    connector = await _connector(session, workspace)
    await session.commit()

    source_ids = await connector_polling.poll_connector(session, connector=connector)
    await session.commit()
    await session.refresh(connector)

    assert source_ids == []
    assert connector.state is ConnectorState.enabled
    assert connector.last_run_detail["outcome"] == "error"
    assert "source system timed out" in connector.last_run_detail["message"]


async def test_poll_with_zero_items_still_records_a_completed_run(session, workspace, registered_adapter):
    registered_adapter(_StubAdapter(items=[], new_cursor={"sha": "same-as-before"}))
    connector = await _connector(session, workspace)
    await session.commit()

    source_ids = await connector_polling.poll_connector(session, connector=connector)
    await session.commit()
    await session.refresh(connector)

    assert source_ids == []
    assert connector.last_run_detail == {"outcome": "ok", "items_discovered": 0, "message": None}
    assert connector.last_sync_cursor == {"sha": "same-as-before"}
