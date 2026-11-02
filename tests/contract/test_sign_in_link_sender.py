"""Contract for SignInLinkSender: every adapter must pass every test here.

To add an adapter (Entra in M7, Cognito in M8), add one entry to ADAPTERS that builds it against a
fake of its provider. The fake answers with one of OUTCOMES and records what was sent.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from adapters.gcp.identity_platform_sign_in import SEND_URL, IdentityPlatformSignInLinkSender
from core.ports import SignInLinkSender
from core.sign_in import SignInLinkFailed

PAGE = "https://spanish-support-chatbot.pages.dev/"
EMAIL = "ana.prueba@example.com"  # synthetic


@dataclass
class Sent:
    email: str
    continue_url: str


@dataclass
class Fake:
    sender: SignInLinkSender
    sent: list[Sent] = field(default_factory=list)


# --- adapters -------------------------------------------------------------------------------------

IDENTITY_PLATFORM_REPLIES: dict[str, Any] = {
    "sent": (200, {"kind": "identitytoolkit#GetOobConfirmationCodeResponse", "email": EMAIL}),
    "invalid_email": (400, {"error": {"code": 400, "message": "INVALID_EMAIL"}}),
    "too_many_attempts": (400, {"error": {"code": 400, "message": "TOO_MANY_ATTEMPTS_TRY_LATER"}}),
    "rate_limited": (429, {"error": {"code": 429, "message": "RESOURCE_EXHAUSTED"}}),
    "server_error": (500, {"error": {"code": 500, "message": "INTERNAL_ERROR : " + EMAIL}}),
    "not_json": (502, None),
    "down": httpx.ConnectError("unreachable"),
}


def identity_platform(outcome: str) -> Fake:
    fake: Fake

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == SEND_URL.format(project="demo-project")
        assert request.headers["authorization"] == "Bearer runtime-account-token"
        body = httpx.Response(200, content=request.content).json()
        assert body["requestType"] == "EMAIL_SIGNIN" and body["canHandleCodeInApp"] is True
        fake.sent.append(Sent(body["email"], body["continueUrl"]))
        reply = IDENTITY_PLATFORM_REPLIES[outcome]
        if isinstance(reply, Exception):
            raise reply
        status, payload = reply
        return httpx.Response(status, json=payload) if payload else httpx.Response(status)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sender = IdentityPlatformSignInLinkSender(
        "demo-project", PAGE, client=client, token=lambda: "runtime-account-token"
    )
    fake = Fake(sender)
    return fake


ADAPTERS: dict[str, Callable[[str], Fake]] = {"identity_platform": identity_platform}

# --- the contract ---------------------------------------------------------------------------------


@pytest.fixture(params=sorted(ADAPTERS))
def make(request: pytest.FixtureRequest) -> Callable[[str], Fake]:
    return ADAPTERS[request.param]


def test_sends_one_link_that_returns_to_the_chat_page(make: Callable[[str], Fake]) -> None:
    fake = make("sent")
    fake.sender.send(EMAIL)
    assert fake.sent == [Sent(EMAIL, PAGE)]


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("invalid_email", "invalid_email"),
        ("too_many_attempts", "too_many_attempts"),
        ("rate_limited", "too_many_attempts"),
        ("server_error", "sign_in_unavailable"),
        ("not_json", "sign_in_unavailable"),
        ("down", "sign_in_unavailable"),  # fails closed
    ],
)
def test_failures_raise_a_reason_code_without_the_address(
    make: Callable[[str], Fake], outcome: str, reason: str
) -> None:
    with pytest.raises(SignInLinkFailed) as raised:
        make(outcome).sender.send(EMAIL)
    assert raised.value.reason == reason
    assert EMAIL not in str(raised.value)


def test_rejects_a_continue_url_that_is_not_https() -> None:
    with pytest.raises(ValueError):
        IdentityPlatformSignInLinkSender("demo-project", "http://example.com/", token=lambda: "t")
