"""Contract for IdentityProvider: every adapter must pass every test here.

To add an adapter (Entra in M7, Cognito in M8), add one entry to ADAPTERS that builds it with the
test keys and says which issuer and audience its tokens carry.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from adapters.gcp.firebase_auth import firebase_identity_provider
from adapters.shared.jwt_identity import JwtIdentityProvider
from core.identity import InvalidToken
from core.ports import IdentityProvider
from tests.jwt_keys import OTHER_KEY, FakeKeys, claims, forge_hs256_with_public_key, sign


@dataclass(frozen=True)
class Adapter:
    provider: IdentityProvider
    issuer: str
    audience: str

    def token(self, **overrides: Any) -> str:
        return sign(claims(self.issuer, self.audience, **overrides))


def generic_jwt() -> Adapter:
    issuer, audience = "https://issuer.example", "demo-audience"
    return Adapter(
        JwtIdentityProvider("unused", issuer, audience, keys=FakeKeys()), issuer, audience
    )


def firebase() -> Adapter:
    project = "demo-project"
    provider = firebase_identity_provider(project, keys=FakeKeys())
    return Adapter(provider, f"https://securetoken.google.com/{project}", project)


ADAPTERS: list[Any] = [
    pytest.param(generic_jwt, id="jwt"),
    pytest.param(firebase, id="firebase"),
]


@pytest.fixture(params=ADAPTERS)
def adapter(request: pytest.FixtureRequest) -> Adapter:
    build: Callable[[], Adapter] = request.param
    return build()


def test_valid_token_gives_the_subject_and_sign_in_time(adapter: Adapter) -> None:
    signed_in_at = int(time.time()) - 120

    verified = adapter.provider.verify(adapter.token(auth_time=signed_in_at))

    assert verified.sub == "user-123"
    assert verified.auth_time == signed_in_at


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"exp": int(time.time()) - 120}, "expired"),
        ({"aud": "another-audience"}, "wrong_audience"),
        ({"iss": "https://another-issuer.example"}, "wrong_issuer"),
        ({"sub": None}, "missing_claim"),
        ({"sub": ""}, "missing_claim"),
        ({"exp": None}, "missing_claim"),
        ({"auth_time": None}, "missing_claim"),
        ({"auth_time": int(time.time()) + 3600}, "invalid_auth_time"),
    ],
)
def test_bad_claims_are_rejected_with_the_same_reason_everywhere(
    adapter: Adapter, overrides: dict[str, Any], reason: str
) -> None:
    with pytest.raises(InvalidToken) as rejected:
        adapter.provider.verify(adapter.token(**overrides))
    assert rejected.value.reason == reason


def test_signature_from_another_key_is_rejected(adapter: Adapter) -> None:
    forged = sign(claims(adapter.issuer, adapter.audience), key=OTHER_KEY)
    with pytest.raises(InvalidToken):
        adapter.provider.verify(forged)


def test_hs256_signed_with_the_public_key_is_rejected(adapter: Adapter) -> None:
    forged = forge_hs256_with_public_key(claims(adapter.issuer, adapter.audience))
    with pytest.raises(InvalidToken):
        adapter.provider.verify(forged)


def test_unsigned_token_is_rejected(adapter: Adapter) -> None:
    unsigned = sign(claims(adapter.issuer, adapter.audience), key=None, algorithm="none")
    with pytest.raises(InvalidToken):
        adapter.provider.verify(unsigned)


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b.c"])
def test_garbage_is_rejected(adapter: Adapter, garbage: str) -> None:
    with pytest.raises(InvalidToken):
        adapter.provider.verify(garbage)
