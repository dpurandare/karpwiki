"""Gateway authentication and authorization (06 §3, 09 §15).

Authorization is real from Phase 1: `access_policy` plus the three roles, checked on every
endpoint. Authentication is a provider interface with one method, so a real IdP-backed
provider can land without touching a single handler — Phase 1 ships the trusted-header
provider (still the default when no OIDC settings are configured), and phase2-tasklist.md
step 47 adds `OidcAuthenticator`, a real bearer-JWT validator, as the second implementation
`09` §15 always intended.

`Authenticator.authenticate` is async (changed in step 47, from the sync-only shape Phase 1
shipped): `TrustedHeaderAuthenticator` does no I/O so this is a no-op change for it, but
`OidcAuthenticator` fetches/caches a JWKS over the network, and a real gateway meant to
serve more than one request concurrently shouldn't block its event loop doing that inline.

**SAML is not supported.** `08` §2 names "Authlib (OIDC/SAML)" as the auth stack, but
Authlib has no SAML module at all (checked directly: `authlib.oauth1`/`oauth2`/`oidc` only)
— SAML SP support (XML signature validation, IdP metadata, an assertion-consumer endpoint)
is a materially different, much larger feature needing a different library entirely, and
nothing in `08` §4's dependency list or any tasklist step names one. Flagged here as a real
spec/implementation-stack mismatch rather than silently unbuilt.
"""

import asyncio
from dataclasses import dataclass
from typing import Protocol

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config
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

    async def authenticate(self, headers: dict[str, str]) -> Principal | None: ...


class TrustedHeaderAuthenticator:
    """Phase 1 provider: believes `X-Karpwiki-User` and `X-Karpwiki-Groups`.

    Only sound where the gateway is unreachable except through a proxy that authenticates
    and strips these headers. It exists so Phase 1 need not wait on an IdP (09 §15), and
    stays the default until real OIDC settings are configured (`default_authenticator`
    below) — `OidcAuthenticator` is a second implementation, not a replacement.
    """

    async def authenticate(self, headers: dict[str, str]) -> Principal | None:
        lowered = {k.lower(): v for k, v in headers.items()}
        user = lowered.get("x-karpwiki-user", "").strip()
        if not user:
            return None
        raw_groups = lowered.get("x-karpwiki-groups", "")
        groups = tuple(g.strip() for g in raw_groups.split(",") if g.strip())
        return Principal(id=user, groups=groups)


class OidcAuthenticator:
    """Real bearer-JWT OIDC auth (06 §3, 08 §2, phase2-tasklist.md step 47) — validates an
    `Authorization: Bearer <token>` header against the configured IdP's JWKS.

    Uses `joserfc` for the actual JWS/claims verification — Authlib's own `authlib.jose` is
    deprecated in favor of it ("please use joserfc instead," as of Authlib 1.7), and
    `joserfc` is already an Authlib dependency, not a separate library choice.

    JWKS is fetched once (via OIDC discovery, `{issuer}/.well-known/openid-configuration`,
    unless `jwks_uri` is given directly) and cached indefinitely, refetched exactly once,
    inline, on a `kid` this cache doesn't recognize — the standard client pattern for
    surviving IdP key rotation without polling. A per-instance `asyncio.Lock` serializes
    concurrent refreshes so a burst of requests during a cache miss triggers one fetch, not
    one per in-flight request.

    `http_client` is injectable for tests; built fresh per instance otherwise, never at
    module scope — a module-level async client bound to whichever event loop is running on
    first use broke across test functions the same way once already in this codebase
    (`09` §29's OpenSearch-client lesson), so each `OidcAuthenticator` gets its own.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_uri: str | None = None,
        principal_claim: str = "sub",
        groups_claim: str = "groups",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks_uri = jwks_uri
        self._principal_claim = principal_claim
        self._groups_claim = groups_claim
        self._http = http_client or httpx.AsyncClient(timeout=config.OIDC_JWKS_TIMEOUT_SECONDS)
        self._keyset: KeySet | None = None
        self._lock = asyncio.Lock()

    async def _resolve_jwks_uri(self) -> str:
        if self._jwks_uri:
            return self._jwks_uri
        resp = await self._http.get(f"{self._issuer.rstrip('/')}/.well-known/openid-configuration")
        resp.raise_for_status()
        self._jwks_uri = resp.json()["jwks_uri"]
        return self._jwks_uri

    async def _fetch_keyset(self) -> KeySet:
        uri = await self._resolve_jwks_uri()
        resp = await self._http.get(uri)
        resp.raise_for_status()
        return KeySet.import_key_set(resp.json())

    async def _decode(self, token: str) -> jwt.Token | None:
        async with self._lock:
            if self._keyset is None:
                self._keyset = await self._fetch_keyset()
            keyset = self._keyset
        try:
            return jwt.decode(token, keyset)
        except JoseError:
            # Possibly a rotated key this cache hasn't seen yet — refetch once, inline.
            async with self._lock:
                self._keyset = await self._fetch_keyset()
                keyset = self._keyset
            try:
                return jwt.decode(token, keyset)
            except JoseError:
                return None

    async def authenticate(self, headers: dict[str, str]) -> Principal | None:
        lowered = {k.lower(): v for k, v in headers.items()}
        auth_header = lowered.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return None
        token = auth_header[len("bearer ") :].strip()
        if not token:
            return None

        try:
            decoded = await self._decode(token)
        except httpx.HTTPError:
            # A real network failure fetching/refreshing the JWKS — not the caller's
            # fault, but still "not authenticated" from this method's own contract
            # (headers in, Principal or None out — it never raises for a bad caller).
            return None
        if decoded is None:
            return None

        registry = JWTClaimsRegistry(
            iss={"essential": True, "value": self._issuer},
            aud={"essential": True, "value": self._audience},
            exp={"essential": True},
        )
        try:
            registry.validate(decoded.claims)
        except JoseError:
            return None

        principal_id = decoded.claims.get(self._principal_claim)
        if not principal_id:
            return None
        raw_groups = decoded.claims.get(self._groups_claim) or []
        groups = tuple(raw_groups) if isinstance(raw_groups, list) else ()
        return Principal(id=str(principal_id), groups=groups)


def default_authenticator() -> Authenticator:
    """`create_app`/`create_mcp_server`'s shared default — an unconfigured deployment keeps
    `TrustedHeaderAuthenticator`; setting both `KARPWIKI_OIDC_ISSUER` and
    `KARPWIKI_OIDC_AUDIENCE` swaps in real OIDC with no handler changes, exactly what `09`
    §15 said this second provider would do."""
    if config.OIDC_ISSUER and config.OIDC_AUDIENCE:
        return OidcAuthenticator(
            issuer=config.OIDC_ISSUER,
            audience=config.OIDC_AUDIENCE,
            jwks_uri=config.OIDC_JWKS_URI or None,
            principal_claim=config.OIDC_PRINCIPAL_CLAIM,
            groups_claim=config.OIDC_GROUPS_CLAIM,
        )
    return TrustedHeaderAuthenticator()


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
