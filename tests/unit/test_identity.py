import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from adapters.gcp.firebase_auth import firebase_identity_provider
from adapters.shared.jwt_identity import JwtIdentityProvider
from adapters.shared.mock_llm import MockLLM
from apps.api.main import build_identity, create_app
from core.identity import (
    STEP_UP_WINDOW_SECONDS,
    InvalidToken,
    Tier,
    VerifiedToken,
    caller_for,
)

PROJECT = "demo-project"
ISSUER = f"https://securetoken.google.com/{PROJECT}"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass
class FakeSigningKey:
    key: Any


class FakeKeys:
    """Stands in for the JWKS download: always returns the public half of KEY."""

    def get_signing_key_from_jwt(self, token: str) -> FakeSigningKey:
        return FakeSigningKey(KEY.public_key())


def provider() -> JwtIdentityProvider:
    return JwtIdentityProvider("unused", issuer=ISSUER, audience=PROJECT, keys=FakeKeys())


def token(key: Any = KEY, algorithm: str = "RS256", **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": PROJECT,
        "sub": "user-123",
        "iat": now - 60,
        "exp": now + 3600,
        "auth_time": now - 3600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": "k1"})


# --- the JWT checks -------------------------------------------------------------------------------


def test_valid_token_verifies() -> None:
    verified = provider().verify(token())
    assert verified.sub == "user-123"


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"exp": int(time.time()) - 120}, "expired"),
        ({"aud": "another-project"}, "wrong_audience"),
        ({"iss": "https://securetoken.google.com/another-project"}, "wrong_issuer"),
        ({"sub": None}, "missing_claim"),
        ({"sub": ""}, "missing_claim"),
        ({"auth_time": None}, "missing_claim"),
        ({"auth_time": int(time.time()) + 3600}, "invalid_auth_time"),
    ],
)
def test_bad_claims_are_rejected(claims: dict[str, Any], reason: str) -> None:
    with pytest.raises(InvalidToken) as rejected:
        provider().verify(token(**claims))
    assert rejected.value.reason == reason


def test_signature_from_another_key_is_rejected() -> None:
    with pytest.raises(InvalidToken, match="invalid"):
        provider().verify(token(key=OTHER_KEY))


def test_hs256_signed_with_the_public_key_is_rejected() -> None:
    # Algorithm confusion: an attacker signs with HS256, using the public key as the secret.
    # PyJWT refuses to build such a token, so it is assembled by hand, as an attacker would.
    public_pem = KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    claims = jwt.decode(token(), options={"verify_signature": False})

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    body = b64(json.dumps(claims).encode())
    signature = b64(hmac.new(public_pem, f"{header}.{body}".encode(), hashlib.sha256).digest())

    with pytest.raises(InvalidToken):
        provider().verify(f"{header}.{body}.{signature}")


def test_unsigned_token_is_rejected() -> None:
    with pytest.raises(InvalidToken):
        provider().verify(token(key=None, algorithm="none"))


def test_garbage_is_rejected() -> None:
    with pytest.raises(InvalidToken):
        provider().verify("not-a-jwt")


def test_firebase_provider_uses_the_project_as_audience_and_issuer() -> None:
    firebase = firebase_identity_provider(PROJECT)
    assert firebase._audience == PROJECT
    assert firebase._issuer == ISSUER


# --- the tier rule --------------------------------------------------------------------------------


def test_recent_sign_in_is_step_up_and_older_is_signed_in() -> None:
    now = 1_000_000.0
    recent = VerifiedToken("u", auth_time=int(now) - STEP_UP_WINDOW_SECONDS)
    older = VerifiedToken("u", auth_time=int(now) - STEP_UP_WINDOW_SECONDS - 1)
    assert caller_for(recent, now).tier == Tier.STEP_UP
    assert caller_for(older, now).tier == Tier.SIGNED_IN
    assert caller_for(older, now).sub == "u"


# --- the API --------------------------------------------------------------------------------------


class RecordingLLM:
    model = "fake"

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def stream(self, system: str, user_message: str) -> AsyncIterator[str]:
        self.seen += [system, user_message]
        yield "ok"


def post(headers: dict[str, str], llm: Any = None) -> Any:
    app = create_app(llm=llm or MockLLM(), identity=provider())
    return TestClient(app).post("/v1/chat", json={"message": "hola"}, headers=headers)


def test_missing_token_is_the_anonymous_tier() -> None:
    response = post({})
    assert response.status_code == 200
    assert "event: done" in response.text


def test_valid_token_streams_and_never_reaches_the_model() -> None:
    llm = RecordingLLM()
    signed = token()

    response = post({"Authorization": f"Bearer {signed}"}, llm=llm)

    assert response.status_code == 200
    assert not any(signed in text or "Bearer" in text for text in llm.seen)


@pytest.mark.parametrize(
    "authorization",
    [
        f"Bearer {token(exp=int(time.time()) - 120)}",  # expired
        f"Bearer {token(aud='another-project')}",  # wrong audience
        f"Bearer {token(key=OTHER_KEY)}",  # forged
        "Bearer ",  # empty
        "Basic dXNlcjpwYXNz",  # wrong scheme
        "garbage",
    ],
)
def test_invalid_token_is_401_before_anything_streams(authorization: str) -> None:
    llm = RecordingLLM()

    response = post({"Authorization": authorization}, llm=llm)

    assert response.status_code == 401
    assert response.json() == {"error": "invalid_token"}
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    assert llm.seen == []


def test_token_is_rejected_when_no_identity_provider_is_configured() -> None:
    app = create_app(llm=MockLLM())
    response = TestClient(app).post(
        "/v1/chat", json={"message": "hola"}, headers={"Authorization": f"Bearer {token()}"}
    )
    assert response.status_code == 401


def test_rejections_log_the_reason_never_the_token(caplog: pytest.LogCaptureFixture) -> None:
    expired = token(exp=int(time.time()) - 120)
    with caplog.at_level("INFO", logger="apps.api.main"):
        post({"Authorization": f"Bearer {expired}"})
    assert "auth_rejected reason=expired" in caplog.text
    assert expired not in caplog.text


def test_identity_provider_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IDENTITY_PROVIDER", raising=False)
    assert build_identity() is None
    monkeypatch.setenv("IDENTITY_PROVIDER", "firebase")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    assert isinstance(build_identity(), JwtIdentityProvider)
    monkeypatch.setenv("IDENTITY_PROVIDER", "nope")
    with pytest.raises(ValueError, match="Unknown IDENTITY_PROVIDER"):
        build_identity()
