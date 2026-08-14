"""Gateway authentication and authorization (06 §3, 09 §15).

Authorization is real from Phase 1: `access_policy` plus the three roles, checked on every
endpoint. Authentication is a provider interface with one method, so the OIDC/SAML provider
can land in Phase 2 without touching a single handler — Phase 1 ships the trusted-header
provider, which is why the deployment must terminate it behind something that actually
authenticates.
"""

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AccessPolicy, Role

# Ascending authority: a role satisfies any requirement at or below its own rank.
_RANK: dict[Role, int] = {Role.reader: 0, Role.contributor: 1, Role.admin: 2}


@dataclass(frozen=True)
class Principal:
    """Who is making the request, in `access_policy.principal` form (02 §3)."""

    id: str
    groups: tuple[str, ...] = ()

    @property
    def policy_keys(self) -> tuple[str, ...]:
        """Every principal string a grant could be recorded against for this caller."""
        return (self.id, *self.groups)


class Authenticator(Protocol):
    """Resolve a request's headers to a principal, or None if unauthenticated."""

    def authenticate(self, headers: dict[str, str]) -> Principal | None: ...


class TrustedHeaderAuthenticator:
    """Phase 1 provider: believes `X-Karpwiki-User` and `X-Karpwiki-Groups`.

    Only sound where the gateway is unreachable except through a proxy that authenticates
    and strips these headers. It exists so Phase 1 need not wait on an IdP (09 §15), and is
    replaced — not extended — by the OIDC/SAML provider in Phase 2.
    """

    def authenticate(self, headers: dict[str, str]) -> Principal | None:
        lowered = {k.lower(): v for k, v in headers.items()}
        user = lowered.get("x-karpwiki-user", "").strip()
        if not user:
            return None
        raw_groups = lowered.get("x-karpwiki-groups", "")
        groups = tuple(g.strip() for g in raw_groups.split(",") if g.strip())
        return Principal(id=user, groups=groups)


async def effective_role(
    session: AsyncSession, *, principal: Principal, workspace_id: str
) -> Role | None:
    """The strongest role this principal holds in a workspace, directly or via a group."""
    result = await session.execute(
        select(AccessPolicy.role).where(
            AccessPolicy.workspace_id == workspace_id,
            AccessPolicy.principal.in_(principal.policy_keys),
        )
    )
    roles = list(result.scalars())
    return max(roles, key=lambda r: _RANK[r]) if roles else None


async def has_role(
    session: AsyncSession, *, principal: Principal, workspace_id: str, required: Role
) -> bool:
    held = await effective_role(session, principal=principal, workspace_id=workspace_id)
    return held is not None and _RANK[held] >= _RANK[required]


async def any_workspace_with_role(
    session: AsyncSession, *, principal: Principal, required: Role
) -> list[str]:
    """Workspaces where this principal meets `required`.

    Submission needs this because the caller does not name a workspace — 03 §2 accepts a
    source in the target-undetermined state and lets the Classifier route it — so the
    gateway authorizes against what the caller can contribute to *anywhere*.
    """
    result = await session.execute(
        select(AccessPolicy.workspace_id, AccessPolicy.role).where(
            AccessPolicy.principal.in_(principal.policy_keys)
        )
    )
    return sorted({ws for ws, role in result.all() if _RANK[role] >= _RANK[required]})
