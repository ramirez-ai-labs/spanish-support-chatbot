"""DeployProbe for GCP: a Google-signed ID token for the deploy account (tf-apply).

The approved deploy job mints it through Workload Identity Federation with a fixed audience, so it
can only exist inside that job. The API lets it skip the bot check, so the post-deploy smoke test
can still ask the model a real question. It never raises the caller above the anonymous tier.
"""

from typing import Any

import jwt

from adapters.shared.jwt_identity import SigningKeys
from core.identity import InvalidToken

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
# Must match `id_token_audience` in .github/workflows/terraform.yml.
PROBE_AUDIENCE = "spanish-support-chatbot-deploy-probe"


class GoogleDeployProbe:
    def __init__(self, email: str, keys: SigningKeys | None = None, leeway_seconds: int = 30):
        if not email:
            raise ValueError("DEPLOY_PROBE_EMAIL is empty")
        self._email = email
        self._keys = keys or jwt.PyJWKClient(
            GOOGLE_JWKS_URL, cache_keys=True, lifespan=3600, timeout=5
        )
        self._leeway = leeway_seconds

    def verify(self, token: str) -> None:
        try:
            key = self._keys.get_signing_key_from_jwt(token).key
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=PROBE_AUDIENCE,
                issuer=GOOGLE_ISSUERS,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "email"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken("probe_invalid") from exc
        if claims.get("email") != self._email or claims.get("email_verified") is not True:
            raise InvalidToken("probe_wrong_identity")
