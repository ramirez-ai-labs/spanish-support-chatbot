"""IdentityProvider for any OIDC-style JWT signed with RS256 and published as a JWKS.

Firebase uses it in M1; Entra External ID and Cognito publish the same shape, so M7 and M8 only
change the URL, issuer and audience.
"""

import time
from typing import Any, Protocol

import jwt

from core.identity import InvalidToken, VerifiedToken

REQUIRED_CLAIMS = ["exp", "iat", "sub", "auth_time"]


class SigningKeys(Protocol):
    """Finds the public key for a token's `kid`. `jwt.PyJWKClient` in production."""

    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class JwtIdentityProvider:
    def __init__(
        self,
        jwks_url: str,
        issuer: str,
        audience: str,
        keys: SigningKeys | None = None,
        leeway_seconds: int = 30,
    ) -> None:
        # Keys are cached for an hour; the provider rotates them with days of overlap.
        self._keys = keys or jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600, timeout=5)
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway_seconds

    def verify(self, token: str) -> VerifiedToken:
        try:
            key = self._keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],  # pinned: never HS256 or "none" from the token header
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": REQUIRED_CLAIMS},
            )
        except jwt.ExpiredSignatureError as exc:
            raise InvalidToken("expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise InvalidToken("wrong_audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise InvalidToken("wrong_issuer") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise InvalidToken("missing_claim") from exc
        except jwt.PyJWKClientError as exc:
            raise InvalidToken("unknown_key") from exc
        except jwt.PyJWTError as exc:
            raise InvalidToken("invalid") from exc

        sub, auth_time = claims["sub"], claims["auth_time"]
        if not isinstance(sub, str) or not sub:
            raise InvalidToken("missing_claim")
        if not isinstance(auth_time, int) or auth_time > time.time() + self._leeway:
            raise InvalidToken("invalid_auth_time")
        return VerifiedToken(sub=sub, auth_time=auth_time)
