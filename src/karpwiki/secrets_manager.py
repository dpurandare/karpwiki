"""Connector credential resolution (09 §13, phase2-tasklist.md step 53) — a provider
interface mirroring `auth.py`'s pluggable `Authenticator` shape (09 §13's own words: "the
same shape" as credential resolution).

09 §13 deliberately frames the backend as "a role, not a product" (Vault, AWS Secrets
Manager, GCP Secret Manager, and Kubernetes secrets are named as equally valid options),
unlike OIDC where `08` §2 named a specific library — so there is no one "real" provider
this step commits the Platform's code to. `EnvSecretResolver` below is the one concrete
implementation: `credential_ref` names an environment variable, and resolving it reads
that variable. This is not a toy stand-in the way `TrustedHeaderAuthenticator` is
(explicitly "sound only behind a proxy") — it's a genuinely production-viable pattern, and
the most common way a Kubernetes Secret actually reaches a running process (injected as an
env var on the pod spec). A deployment backed by Vault/AWS/GCP secrets instead implements
`SecretResolver` and swaps it in via `default_secret_resolver()`, with no change to
`connector_polling.py`.

Resolution happens once per connector run, in `connector_polling.poll_connector`, and the
resolved value is held only for that call's lifetime — never persisted, never logged (09
§13: "never stored... any log stream"). Only `connector.credential_ref` — the pointer name
— is ever written to the Metadata DB (phase2-tasklist.md step 51); this module never
receives or returns anything else.
"""

import os
from typing import Protocol


class SecretNotFoundError(Exception):
    """`credential_ref` doesn't resolve to anything — treated as an auth-adjacent failure
    by `connector_polling.poll_connector` (the connector can't possibly authenticate
    without ever obtaining the credential), not a generic fetch error."""


class SecretResolver(Protocol):
    """Resolve a connector's `credential_ref` (an opaque pointer, phase2-tasklist.md step
    51) into the real secret value. Raise `SecretNotFoundError` if the ref doesn't
    resolve — never return `None` or an empty string for "not found," so callers can't
    mistake a resolution failure for a real empty credential."""

    async def resolve(self, credential_ref: str) -> str: ...


class EnvSecretResolver:
    """`credential_ref` is the name of an environment variable holding the real secret —
    e.g. a connector configured with `credential_ref: "KARPWIKI_CONNECTOR_GIT_MAIN_TOKEN"`
    resolves against `os.environ["KARPWIKI_CONNECTOR_GIT_MAIN_TOKEN"]`. No I/O, no held
    connection, no client to worry about across event loops (09 §29's OpenSearch-client
    lesson doesn't apply here — there's nothing stateful to hold)."""

    async def resolve(self, credential_ref: str) -> str:
        value = os.environ.get(credential_ref)
        if value is None:
            raise SecretNotFoundError(f"no environment variable named {credential_ref!r}")
        return value


def default_secret_resolver() -> SecretResolver:
    """`connector_polling.poll_connector`'s default — always `EnvSecretResolver` today,
    the one concrete provider this step builds (confirmed via AskUserQuestion: 09 §13's
    own product-agnostic framing argues against committing to one commercial backend the
    way `08` §2 named Authlib for OIDC). Kept as a factory, not a bare singleton, so a
    deployment backed by a real secrets manager can swap providers here later with no
    change to any call site — the same shape `default_authenticator()` already proved out
    for OIDC."""
    return EnvSecretResolver()
