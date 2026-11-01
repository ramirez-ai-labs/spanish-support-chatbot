"""Caller identity and access tiers, shared by every cloud.

The tier comes from the token validated on this call, never from the conversation. Adapters only
verify tokens; the step-up rule lives here so it is the same on Firebase, Entra and Cognito.
"""

from dataclasses import dataclass
from enum import StrEnum

# Step-up: action tools need a sign-in this recent (email code or re-login).
STEP_UP_WINDOW_SECONDS = 5 * 60


class Tier(StrEnum):
    ANONYMOUS = "anonymous"  # policy answers only
    SIGNED_IN = "signed_in"  # plus read-only tools
    STEP_UP = "step_up"  # plus action tools


class InvalidToken(Exception):
    """The token failed verification. `reason` is a short code, safe to log; never the token."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedToken:
    sub: str  # the identity provider's stable user ID
    auth_time: int  # Unix seconds of the user's last actual sign-in


@dataclass(frozen=True)
class Caller:
    tier: Tier
    sub: str | None = None


ANONYMOUS = Caller(Tier.ANONYMOUS)


def caller_for(token: VerifiedToken, now: float) -> Caller:
    signed_in_for = now - token.auth_time
    tier = Tier.STEP_UP if signed_in_for <= STEP_UP_WINDOW_SECONDS else Tier.SIGNED_IN
    return Caller(tier, token.sub)
