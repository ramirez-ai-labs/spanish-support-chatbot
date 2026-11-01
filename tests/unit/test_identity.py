"""The tier rule and how the API uses the token check. The JWT checks themselves are in the
contract suite (tests/contract/test_identity_provider.py), which every identity adapter passes.
"""

import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from adapters.shared.jwt_identity import JwtIdentityProvider
from adapters.shared.mock_llm import MockLLM
from apps.api.main import build_identity, create_app
from core.identity import STEP_UP_WINDOW_SECONDS, Tier, VerifiedToken, caller_for
from tests.jwt_keys import OTHER_KEY, FakeKeys, claims, sign

ISSUER, AUDIENCE = "https://issuer.example", "demo-audience"


def provider() -> JwtIdentityProvider:
    return JwtIdentityProvider("unused", issuer=ISSUER, audience=AUDIENCE, keys=FakeKeys())


def token(key: Any = None, **overrides: Any) -> str:
    values = claims(ISSUER, AUDIENCE, **overrides)
    return sign(values) if key is None else sign(values, key=key)


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

    async def stream(
        self, system: str, user_message: str, usage: object = None
    ) -> AsyncIterator[str]:
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
        f"Bearer {token(aud='another-audience')}",  # wrong audience
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
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
    assert isinstance(build_identity(), JwtIdentityProvider)
    monkeypatch.setenv("IDENTITY_PROVIDER", "nope")
    with pytest.raises(ValueError, match="Unknown IDENTITY_PROVIDER"):
        build_identity()
