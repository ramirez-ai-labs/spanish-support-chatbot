import time
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from adapters.gcp.deploy_probe import PROBE_AUDIENCE, GoogleDeployProbe
from adapters.shared.mock_llm import MockLLM
from adapters.shared.turnstile import SITEVERIFY_URL, TurnstileBotCheck
from apps.api.main import create_app
from core.bot_check import BotCheckFailed
from core.identity import InvalidToken
from tests.jwt_keys import OTHER_KEY, FakeKeys, sign

PAGE = "https://spanish-support-chatbot.pages.dev"
PROBE_EMAIL = "tf-apply@demo-project.iam.gserviceaccount.com"


# --- Turnstile ------------------------------------------------------------------------------------


def turnstile(reply: Any, seen: list[dict[str, list[str]]] | None = None) -> TurnstileBotCheck:
    """A TurnstileBotCheck whose HTTP calls go to a fake Cloudflare that returns `reply`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == SITEVERIFY_URL
        if seen is not None:
            seen.append(parse_qs(request.content.decode()))
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return httpx.Response(reply)
        return httpx.Response(200, json=reply)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TurnstileBotCheck("server-secret", {"spanish-support-chatbot.pages.dev"}, client)


def test_valid_token_passes_and_sends_the_secret() -> None:
    seen: list[dict[str, list[str]]] = []
    check = turnstile({"success": True, "hostname": "spanish-support-chatbot.pages.dev"}, seen)

    check.verify("token-from-the-page")

    assert seen == [{"secret": ["server-secret"], "response": ["token-from-the-page"]}]


@pytest.mark.parametrize(
    ("token", "reply", "reason"),
    [
        (None, {"success": True}, "turnstile_required"),
        ("", {"success": True}, "turnstile_required"),
        ("x" * 2049, {"success": True}, "turnstile_failed"),
        ("t", {"success": False, "error-codes": ["timeout-or-duplicate"]}, "turnstile_failed"),
        ("t", {"success": True, "hostname": "another-site.example"}, "turnstile_wrong_host"),
        ("t", 500, "turnstile_unavailable"),
        ("t", httpx.ConnectError("down"), "turnstile_unavailable"),
    ],
)
def test_turnstile_refusals(token: str | None, reply: Any, reason: str) -> None:
    with pytest.raises(BotCheckFailed) as failed:
        turnstile(reply).verify(token)
    assert failed.value.reason == reason


# --- the deploy probe -----------------------------------------------------------------------------


def probe_token(key: Any = None, **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": PROBE_AUDIENCE,
        "sub": "1234567890",
        "email": PROBE_EMAIL,
        "email_verified": True,
        "iat": now - 10,
        "exp": now + 3600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return sign(claims) if key is None else sign(claims, key=key)


def probe() -> GoogleDeployProbe:
    return GoogleDeployProbe(PROBE_EMAIL, keys=FakeKeys())


def test_the_deploy_accounts_token_is_accepted() -> None:
    probe().verify(probe_token())  # Google ID tokens have no auth_time; none is needed


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"email": "someone-else@demo-project.iam.gserviceaccount.com"}, "probe_wrong_identity"),
        ({"email_verified": None}, "probe_wrong_identity"),
        ({"aud": "another-audience"}, "probe_invalid"),
        ({"iss": "https://securetoken.google.com/demo-project"}, "probe_invalid"),
        ({"exp": int(time.time()) - 120}, "probe_invalid"),
    ],
)
def test_other_tokens_are_not_the_probe(overrides: dict[str, Any], reason: str) -> None:
    with pytest.raises(InvalidToken) as rejected:
        probe().verify(probe_token(**overrides))
    assert rejected.value.reason == reason


def test_a_forged_probe_is_rejected() -> None:
    with pytest.raises(InvalidToken):
        probe().verify(probe_token(key=OTHER_KEY))


# --- the API --------------------------------------------------------------------------------------


class FakeBotCheck:
    def __init__(self, fail_with: str | None = None) -> None:
        self.fail_with = fail_with
        self.calls: list[str | None] = []

    def verify(self, token: str | None) -> None:
        self.calls.append(token)
        if self.fail_with:
            raise BotCheckFailed(self.fail_with)


def post(bot_check: FakeBotCheck, headers: dict[str, str]) -> httpx.Response:
    app = create_app(llm=MockLLM(), bot_check=bot_check, deploy_probe=probe(), origins=[PAGE])
    return TestClient(app).post("/v1/chat", json={"message": "hola"}, headers=headers)


def test_a_message_with_a_passing_bot_check_streams() -> None:
    check = FakeBotCheck()
    response = post(check, {"CF-Turnstile-Response": "page-token"})
    assert response.status_code == 200 and "event: done" in response.text
    assert check.calls == ["page-token"]


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("turnstile_required", 403),
        ("turnstile_failed", 403),
        ("turnstile_wrong_host", 403),
        ("turnstile_unavailable", 503),
    ],
)
def test_a_failed_bot_check_is_refused_before_anything_streams(reason: str, status: int) -> None:
    response = post(FakeBotCheck(fail_with=reason), {})
    assert response.status_code == status
    assert response.json() == {"error": reason}


def test_the_deploy_probe_skips_the_bot_check_and_gets_an_answer() -> None:
    check = FakeBotCheck(fail_with="turnstile_required")
    response = post(check, {"X-Deploy-Probe": probe_token()})
    assert response.status_code == 200 and "event: done" in response.text
    assert check.calls == []


def test_a_bad_deploy_probe_is_401_not_a_bypass() -> None:
    check = FakeBotCheck()
    response = post(check, {"X-Deploy-Probe": probe_token(key=OTHER_KEY)})
    assert response.status_code == 401
    assert check.calls == []


def test_a_probe_header_is_refused_when_no_probe_is_configured() -> None:
    app = create_app(llm=MockLLM(), bot_check=FakeBotCheck())
    response = TestClient(app).post(
        "/v1/chat", json={"message": "hola"}, headers={"X-Deploy-Probe": probe_token()}
    )
    assert response.status_code == 401


def test_the_chat_page_origin_may_call_the_api_from_a_browser() -> None:
    app = create_app(llm=MockLLM(), origins=[PAGE])
    preflight = TestClient(app).options(
        "/v1/chat",
        headers={
            "Origin": PAGE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, traceparent, cf-turnstile-response",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == PAGE
    allowed = preflight.headers["access-control-allow-headers"].lower()
    assert "traceparent" in allowed and "cf-turnstile-response" in allowed


def test_other_origins_are_not_allowed() -> None:
    app = create_app(llm=MockLLM(), origins=[PAGE])
    preflight = TestClient(app).options(
        "/v1/chat",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers
