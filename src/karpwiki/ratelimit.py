"""Rate limiting (`01` §1-2, `07` §3, `09` §14) — a Redis-backed fixed-window counter
behind the `RateLimit-*`/`Retry-After` header contract `09` §14 already specified but
nothing emitted until phase2-tasklist.md step 48.

**Scoped to the REST gateway only.** `09` §14's header contract is inherently an HTTP
mechanism, and MCP's own protocol has no equivalent — `stdio` transport has no headers at
all, and neither `06` §2's tool table nor this tasklist step names rate limiting for MCP.

**Two scopes, both from `07` §3's own "per-principal and per-workspace" framing**:
per-principal is always checked, using a coarse, *unverified* identity key — the raw
`Authorization`/`X-Karpwiki-User` header value, hashed before it ever becomes a Redis key
name (never a raw credential, matching this project's existing never-print-a-secret
discipline applied to a new surface). Re-running a real `Authenticator` — possibly a real
network JWKS fetch, `09` §50 — just to bucket a counter would be wasteful, and wrong: an
unauthenticated or invalid-token caller still needs throttling, which a coarse key
provides without needing to *validate* anything. Per-workspace is opportunistic, not
comprehensive — `api.py`'s middleware only checks it when `workspace_id` is already a
plain query/path parameter on the request, since genuinely resolving "which workspace"
for every endpoint shape (taxonomy pre-filter, not-yet-classified submissions) would mean
duplicating real business logic in middleware. Confirmed via AskUserQuestion before
building rather than assumed.

**Three mutually exclusive categories** (`07` §3's own three: "submissions, search calls,
and API requests") — a request is classified into exactly one, by path/method, not
layered; "API requests" reads as the general catch-all, not an additional layer on top of
the other two.
"""

import hashlib
from dataclasses import dataclass

import redis.asyncio as redis


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int


async def check(client: redis.Redis, *, key: str, limit: int, window_seconds: int) -> RateLimitResult:
    """Fixed-window counter: `INCR` a Redis key scoped to the caller/window, `EXPIRE` it
    only on the first hit in that window. Not perfectly atomic between the two calls (a
    crash between them would leave a key with no expiry, pinning that bucket until Redis
    itself evicts it) — acceptable for abuse/load protection, not a security boundary;
    `09` §14 specifies the header contract, not a specific algorithm."""
    count = await client.incr(key)
    if count == 1:
        await client.expire(key, window_seconds)
    ttl = await client.ttl(key)
    reset_seconds = ttl if ttl and ttl > 0 else window_seconds
    return RateLimitResult(
        allowed=count <= limit,
        limit=limit,
        remaining=max(0, limit - count),
        reset_seconds=reset_seconds,
    )


def principal_key(headers: dict[str, str]) -> str:
    """A coarse, unverified bucketing key — never used for AuthN/AuthZ, only to group a
    caller's requests together for throttling."""
    lowered = {k.lower(): v for k, v in headers.items()}
    raw = lowered.get("authorization") or lowered.get("x-karpwiki-user") or ""
    if not raw:
        return "anon"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
