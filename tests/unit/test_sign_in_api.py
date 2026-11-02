"""POST /v1/sign-in-link: the bot check comes first, and the address never leaves the request."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from adapters.shared.mock_llm import MockLLM
from apps.api.main import build_sign_in, create_app
from core.bot_check import BotCheckFailed
from core.sign_in import SignInLinkFailed, looks_like_email

EMAIL = "ana.prueba@example.com"  # synthetic


class FakeBotCheck:
    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason

    def verify(self, token: str | None) -> None:
        if self.reason is not None:
            raise BotCheckFailed(self.reason)
        if not token:
            raise BotCheckFailed("turnstile_required")


class FakeSender:
    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        self.sent: list[str] = []

    def send(self, email: str) -> None:
        if self.reason is not None:
            raise SignInLinkFailed(self.reason)
        self.sent.append(email)


def post(sender: Any, bot: Any = None, email: str = EMAIL, token: str | None = "solved") -> Any:
    app = create_app(llm=MockLLM(), bot_check=bot or FakeBotCheck(), sign_in=sender)
    headers = {"CF-Turnstile-Response": token} if token else {}
    return TestClient(app).post("/v1/sign-in-link", json={"email": email}, headers=headers)


def test_sends_the_link_after_the_bot_check() -> None:
    sender = FakeSender()
    response = post(sender)
    assert response.status_code == 202
    assert response.json() == {"status": "sent"}
    assert sender.sent == [EMAIL]


@pytest.mark.parametrize(
    ("bot", "token", "status", "reason"),
    [
        (None, None, 403, "turnstile_required"),
        (FakeBotCheck("turnstile_failed"), "stale", 403, "turnstile_failed"),
        (FakeBotCheck("turnstile_unavailable"), "solved", 503, "turnstile_unavailable"),
    ],
)
def test_bot_check_refusals_send_nothing(
    bot: Any, token: str | None, status: int, reason: str
) -> None:
    sender = FakeSender()
    response = post(sender, bot=bot, token=token)
    assert (response.status_code, response.json()) == (status, {"error": reason})
    assert sender.sent == []


def test_malformed_address_is_refused_before_the_provider() -> None:
    sender = FakeSender()
    response = post(sender, email="no-arroba.example.com")
    assert (response.status_code, response.json()) == (400, {"error": "invalid_email"})
    assert sender.sent == []


@pytest.mark.parametrize(
    ("reason", "status"),
    [("invalid_email", 400), ("too_many_attempts", 429), ("sign_in_unavailable", 503)],
)
def test_sender_failures_map_to_status_codes(reason: str, status: int) -> None:
    response = post(FakeSender(reason))
    assert (response.status_code, response.json()) == (status, {"error": reason})


def test_without_a_sender_sign_in_is_unavailable() -> None:
    response = post(None)
    assert (response.status_code, response.json()) == (503, {"error": "sign_in_unavailable"})


def test_the_address_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="apps.api.main"):
        post(FakeSender())
        post(FakeSender("too_many_attempts"))
    assert "sign_in_link_sent" in caplog.text
    assert "request_rejected reason=too_many_attempts" in caplog.text
    assert EMAIL not in caplog.text


def test_sign_in_sender_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIGN_IN_SENDER", raising=False)
    assert build_sign_in() is None
    monkeypatch.setenv("SIGN_IN_SENDER", "nope")
    with pytest.raises(ValueError, match="Unknown SIGN_IN_SENDER"):
        build_sign_in()


@pytest.mark.parametrize(
    "value",
    ["ana.prueba@example.com", "a+b@sub.example.mx", "x@y.co"],
)
def test_email_shapes_that_pass(value: str) -> None:
    assert looks_like_email(value)


@pytest.mark.parametrize(
    "value",
    [
        "no-arroba.example.com",
        "@example.com",
        "ana@",
        "ana@example",
        "ana@.com",
        "ana@example.",
        "ana@@example.com",
        "ana@ex ample.com",
        "a" * 250 + "@x.co",  # over 254
        "!@" + "!" * 5000,  # CodeQL's slow case for the old regex: refused at once
    ],
)
def test_email_shapes_that_fail(value: str) -> None:
    assert not looks_like_email(value)
