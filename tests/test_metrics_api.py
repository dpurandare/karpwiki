"""Phase 2 step 44 — the `GET /metrics/*` admin dashboard endpoints (05 §8)."""

from karpwiki.models import AccessPolicy, Role

READER = {"X-Karpwiki-User": "casey"}
ADMIN = {"X-Karpwiki-User": "avery"}

ENDPOINTS = (
    "/metrics/index-health",
    "/metrics/ingestion-pipeline",
    "/metrics/search-performance",
    "/metrics/storage-utilization",
    "/metrics/review-queue-health",
)


async def test_metrics_endpoints_reject_non_admin(client, session, workspace):
    for path in ENDPOINTS:
        r = await client.get(path, headers=READER, params={"workspace_id": workspace.workspace_id})
        assert r.status_code == 403, path


async def test_metrics_endpoints_allow_admin_scoped_to_workspace(client, session, workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await session.commit()

    for path in ENDPOINTS:
        r = await client.get(path, headers=ADMIN, params={"workspace_id": workspace.workspace_id})
        assert r.status_code == 200, path


async def test_metrics_endpoints_reject_admin_in_a_different_workspace(
    client, session, workspace, other_workspace
):
    session.add(
        AccessPolicy(workspace_id=other_workspace.workspace_id, principal="avery", role=Role.admin)
    )
    await session.commit()

    for path in ENDPOINTS:
        r = await client.get(path, headers=ADMIN, params={"workspace_id": workspace.workspace_id})
        assert r.status_code == 403, path


async def test_metrics_endpoints_allow_admin_anywhere_when_workspace_omitted(
    client, session, other_workspace
):
    session.add(
        AccessPolicy(workspace_id=other_workspace.workspace_id, principal="avery", role=Role.admin)
    )
    await session.commit()

    for path in ENDPOINTS:
        r = await client.get(path, headers=ADMIN)
        assert r.status_code == 200, path


async def test_ingestion_pipeline_endpoint_includes_queue_depths(client, session, workspace):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await session.commit()

    r = await client.get(
        "/metrics/ingestion-pipeline", headers=ADMIN, params={"workspace_id": workspace.workspace_id}
    )
    assert r.status_code == 200
    body = r.json()
    assert "queue_depths" in body
    assert set(body["queue_depths"].keys()) == {
        "classification",
        "curation",
        "indexing",
        "maintenance",
        "connector_polling",
    }


async def test_search_performance_endpoint_reports_none_cache_hit_rate_with_no_activity(
    client, session, workspace
):
    """Step 76 built a real cache — `cache_hit_rate` is `None` only because nothing has
    looked the cache up yet (disabled by default, or simply no traffic), not because the
    mechanism doesn't exist (see cache.py's own docstring)."""
    import redis.asyncio as redis

    from karpwiki import cache, config

    redis_client = redis.from_url(config.CELERY_BROKER_URL)
    await redis_client.delete(cache._HITS_KEY, cache._MISSES_KEY)
    await redis_client.aclose()

    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await session.commit()

    r = await client.get(
        "/metrics/search-performance", headers=ADMIN, params={"workspace_id": workspace.workspace_id}
    )
    assert r.status_code == 200
    assert r.json()["cache_hit_rate"] is None


async def test_search_performance_endpoint_reports_a_real_cache_hit_rate(client, session, workspace):
    import redis.asyncio as redis

    from karpwiki import cache, config

    redis_client = redis.from_url(config.CELERY_BROKER_URL)
    await redis_client.delete(cache._HITS_KEY, cache._MISSES_KEY)
    await redis_client.set(cache._HITS_KEY, 1)
    await redis_client.set(cache._MISSES_KEY, 1)
    try:
        session.add(
            AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin)
        )
        await session.commit()

        r = await client.get(
            "/metrics/search-performance",
            headers=ADMIN,
            params={"workspace_id": workspace.workspace_id},
        )
        assert r.status_code == 200
        assert r.json()["cache_hit_rate"] == 0.5
    finally:
        await redis_client.delete(cache._HITS_KEY, cache._MISSES_KEY)
        await redis_client.aclose()


async def test_storage_utilization_endpoint_reports_empty_trend_before_any_snapshot(
    client, session, workspace
):
    """Step 72 replaced the `trend: None` gap with a real (if still-empty, until the first
    scheduled snapshot run) list — see monitoring.py's module docstring."""
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    await session.commit()

    r = await client.get(
        "/metrics/storage-utilization", headers=ADMIN, params={"workspace_id": workspace.workspace_id}
    )
    assert r.status_code == 200
    assert r.json()["trend"] == []
