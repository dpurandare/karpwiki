"""Phase 2 step 47 — real OIDC bearer-JWT auth (`OidcAuthenticator`, 06 §3, 08 §2).

Uses a real RSA keypair and real `joserfc` encoding to mint tokens exactly as a real IdP
would, and a real `httpx.MockTransport` so the JWKS fetch goes through real HTTP
request/response handling (real JSON parsing, real `raise_for_status`) without a real
network socket — the closest a committed test gets to "real" for something needing a live
IdP to fully exercise (see spec/09-implementation-notes.md for the real live check, not
committed, that serves a JWKS over a real local HTTP server).
"""

import time

import httpx
import pytest
from joserfc.jwk import KeySet, RSAKey
from joserfc.jwt import encode as jwt_encode

from karpwiki.auth import OidcAuthenticator

ISSUER = "https://idp.example.com"
AUDIENCE = "karpwiki"
JWKS_URI = "https://idp.example.com/jwks"


def _keypair(kid="key-1"):
    key = RSAKey.generate_key(2048, parameters={"kid": kid}, private=True)
    return key, KeySet([key])


def _token(key, *, kid="key-1", claims_override=None, **claims):
    header = {"alg": "RS256", "kid": kid}
    base_claims = {
        "sub": "alice",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 3600,
    }
    base_claims.update(claims)
    if claims_override is not None:
        base_claims = claims_override
    return jwt_encode(header, base_claims, key)


def _jwks_transport(keyset: KeySet, *, fail: bool = False):
    async def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(500, json={"error": "idp unavailable"})
        assert str(request.url) == JWKS_URI
        return httpx.Response(200, json=keyset.as_dict(private=False))

    return httpx.MockTransport(handler)


def _authenticator(keyset: KeySet, *, fail: bool = False, **kwargs) -> OidcAuthenticator:
    client = httpx.AsyncClient(transport=_jwks_transport(keyset, fail=fail))
    return OidcAuthenticator(
        issuer=ISSUER, audience=AUDIENCE, jwks_uri=JWKS_URI, http_client=client, **kwargs
    )


async def test_accepts_a_valid_token():
    key, keyset = _keypair()
    token = _token(key, groups=["eng", "ops"])
    authenticator = _authenticator(keyset)

    principal = await authenticator.authenticate({"Authorization": f"Bearer {token}"})
    assert principal is not None
    assert principal.id == "alice"
    assert principal.groups == ("eng", "ops")


async def test_rejects_missing_authorization_header():
    key, keyset = _keypair()
    authenticator = _authenticator(keyset)
    assert await authenticator.authenticate({}) is None


async def test_rejects_non_bearer_scheme():
    key, keyset = _keypair()
    authenticator = _authenticator(keyset)
    assert await authenticator.authenticate({"Authorization": "Basic abc123"}) is None


async def test_header_lookup_is_case_insensitive():
    key, keyset = _keypair()
    token = _token(key)
    authenticator = _authenticator(keyset)
    principal = await authenticator.authenticate({"authorization": f"Bearer {token}"})
    assert principal is not None
    assert principal.id == "alice"


async def test_rejects_expired_token():
    key, keyset = _keypair()
    token = _token(key, exp=int(time.time()) - 10)
    authenticator = _authenticator(keyset)
    assert await authenticator.authenticate({"Authorization": f"Bearer {token}"}) is None


async def test_rejects_wrong_audience():
    key, keyset = _keypair()
    token = _token(key, aud="someone-else")
    authenticator = _authenticator(keyset)
    assert await authenticator.authenticate({"Authorization": f"Bearer {token}"}) is None


async def test_rejects_wrong_issuer():
    key, keyset = _keypair()
    token = _token(key, iss="https://not-the-idp.example.com")
    authenticator = _authenticator(keyset)
    assert await authenticator.authenticate({"Authorization": f"Bearer {token}"}) is None


async def test_rejects_malformed_token():
    key, keyset = _keypair()
    authenticator = _authenticator(keyset)
    assert await authenticator.authenticate({"Authorization": "Bearer not-a-jwt"}) is None


async def test_rejects_token_missing_principal_claim():
    key, keyset = _keypair()
    token = _token(key, claims_override={"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 3600})
    authenticator = _authenticator(keyset)
    assert await authenticator.authenticate({"Authorization": f"Bearer {token}"}) is None


async def test_custom_principal_and_groups_claims():
    key, keyset = _keypair()
    token = _token(
        key,
        claims_override={
            "email": "alice@example.com",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": int(time.time()) + 3600,
            "roles": ["eng"],
        },
    )
    authenticator = _authenticator(keyset, principal_claim="email", groups_claim="roles")
    principal = await authenticator.authenticate({"Authorization": f"Bearer {token}"})
    assert principal is not None
    assert principal.id == "alice@example.com"
    assert principal.groups == ("eng",)


async def test_refetches_jwks_once_on_unknown_kid_then_succeeds():
    """Simulates key rotation: the authenticator's cached keyset is stale (holds the old
    key), but the IdP (mocked) now serves the new one — a real client must refetch and
    retry, not just fail."""
    old_key, old_keyset = _keypair(kid="old-key")
    new_key, new_keyset = _keypair(kid="new-key")
    token = _token(new_key, kid="new-key")

    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # First fetch (cold cache) serves the old keyset; every fetch after (the
        # rotation-triggered refetch) serves the new one.
        keyset = old_keyset if calls["n"] == 1 else new_keyset
        return httpx.Response(200, json=keyset.as_dict(private=False))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    authenticator = OidcAuthenticator(
        issuer=ISSUER, audience=AUDIENCE, jwks_uri=JWKS_URI, http_client=client
    )

    principal = await authenticator.authenticate({"Authorization": f"Bearer {token}"})
    assert principal is not None
    assert principal.id == "alice"
    assert calls["n"] == 2  # one cold-cache fetch, one rotation refetch


async def test_caches_jwks_across_calls():
    key, keyset = _keypair()
    token = _token(key)
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=keyset.as_dict(private=False))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    authenticator = OidcAuthenticator(
        issuer=ISSUER, audience=AUDIENCE, jwks_uri=JWKS_URI, http_client=client
    )

    await authenticator.authenticate({"Authorization": f"Bearer {token}"})
    await authenticator.authenticate({"Authorization": f"Bearer {token}"})
    assert calls["n"] == 1


async def test_returns_none_on_jwks_fetch_failure():
    key, keyset = _keypair()
    token = _token(key)
    authenticator = _authenticator(keyset, fail=True)
    assert await authenticator.authenticate({"Authorization": f"Bearer {token}"}) is None


async def test_discovery_resolves_jwks_uri_when_not_given_directly():
    key, keyset = _keypair()
    token = _token(key)

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(200, json={"jwks_uri": JWKS_URI})
        assert str(request.url) == JWKS_URI
        return httpx.Response(200, json=keyset.as_dict(private=False))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    authenticator = OidcAuthenticator(issuer=ISSUER, audience=AUDIENCE, http_client=client)

    principal = await authenticator.authenticate({"Authorization": f"Bearer {token}"})
    assert principal is not None
    assert principal.id == "alice"


# --- default_authenticator() selection (06 §3, 09 §15's "second provider, no handler changes") --


async def test_default_authenticator_is_trusted_header_when_oidc_unconfigured(monkeypatch):
    from karpwiki import auth, config

    monkeypatch.setattr(config, "OIDC_ISSUER", "")
    monkeypatch.setattr(config, "OIDC_AUDIENCE", "")
    assert isinstance(auth.default_authenticator(), auth.TrustedHeaderAuthenticator)


async def test_default_authenticator_is_oidc_when_both_issuer_and_audience_set(monkeypatch):
    from karpwiki import auth, config

    monkeypatch.setattr(config, "OIDC_ISSUER", ISSUER)
    monkeypatch.setattr(config, "OIDC_AUDIENCE", AUDIENCE)
    assert isinstance(auth.default_authenticator(), auth.OidcAuthenticator)


async def test_default_authenticator_stays_trusted_header_when_only_one_is_set(monkeypatch):
    from karpwiki import auth, config

    monkeypatch.setattr(config, "OIDC_ISSUER", ISSUER)
    monkeypatch.setattr(config, "OIDC_AUDIENCE", "")
    assert isinstance(auth.default_authenticator(), auth.TrustedHeaderAuthenticator)
