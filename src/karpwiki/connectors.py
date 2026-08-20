"""Connector CRUD (02 §3, 05 §7, 09 §13) — phase2-tasklist.md step 51.

Storage and admin configuration only. The polling worker pool that actually runs a
connector (09 §4, step 52), credential resolution against a real secrets manager (step
53), and the first concrete connector type (step 54) are separate, later steps — nothing
here executes a connector or ever handles a raw secret.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AccessPolicy, Connector, ConnectorState, Role
from .pagination import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT


async def create(
    session: AsyncSession,
    *,
    workspace_id: str,
    type: str,
    config: dict | None = None,
    credential_ref: str | None = None,
    schedule: dict | None = None,
    ingestion_policy: str = "auto",
) -> Connector:
    """09 §13: a connector is an ordinary Platform principal — `connector:<connector_id>`
    is granted `contributor` on exactly this one workspace in the same transaction,
    since nothing else could ever establish that grant (there's no separate "invite a
    connector" flow the way there is for a user or group)."""
    connector = Connector(
        workspace_id=workspace_id,
        type=type,
        config=config or {},
        credential_ref=credential_ref,
        schedule=schedule or {},
        ingestion_policy=ingestion_policy,
    )
    session.add(connector)
    await session.flush()
    session.add(
        AccessPolicy(
            workspace_id=workspace_id,
            principal=f"connector:{connector.connector_id}",
            role=Role.contributor,
        )
    )
    await session.flush()
    return connector


async def list_for_workspace(
    session: AsyncSession, *, workspace_id: str, limit: int = DEFAULT_LIST_LIMIT
) -> list[Connector]:
    """Capped, not cursor-paginated (09 §14, phase3-tasklist.md step 66) — a workspace's
    connector count is deployment-config cardinality, not an append-heavy content table, so
    a plain cap is the honest contract rather than cursor machinery nothing will page
    through."""
    limit = min(limit, MAX_LIST_LIMIT)
    result = await session.execute(
        select(Connector)
        .where(Connector.workspace_id == workspace_id)
        .order_by(Connector.type, Connector.connector_id)
        .limit(limit)
    )
    return list(result.scalars())


async def list_for_workspaces(
    session: AsyncSession, *, workspace_ids: list[str], limit: int = DEFAULT_LIST_LIMIT
) -> list[Connector]:
    """Every connector across a set of workspaces — the admin listing endpoint's shape when
    no single `workspace_id` is given, same pattern as `document_types.list_for_workspaces`.
    Capped the same way `list_for_workspace` above is, for the same reason."""
    if not workspace_ids:
        return []
    limit = min(limit, MAX_LIST_LIMIT)
    result = await session.execute(
        select(Connector)
        .where(Connector.workspace_id.in_(workspace_ids))
        .order_by(Connector.workspace_id, Connector.type, Connector.connector_id)
        .limit(limit)
    )
    return list(result.scalars())


async def update(
    session: AsyncSession,
    *,
    connector: Connector,
    type: str | None = None,
    config: dict | None = None,
    credential_ref: str | None = None,
    schedule: dict | None = None,
    ingestion_policy: str | None = None,
    state: ConnectorState | None = None,
) -> Connector:
    """Reconfigure schedule/policy/credential/state (05 §7); `workspace_id` is
    deliberately not reassignable — see `models.Connector`'s docstring."""
    if type is not None:
        connector.type = type
    if config is not None:
        connector.config = config
    if credential_ref is not None:
        connector.credential_ref = credential_ref
    if schedule is not None:
        connector.schedule = schedule
    if ingestion_policy is not None:
        connector.ingestion_policy = ingestion_policy
    if state is not None:
        connector.state = state
    await session.flush()
    return connector
