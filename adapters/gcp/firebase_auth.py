"""Firebase Auth ID tokens: the GCP IdentityProvider.

Checks follow Firebase's rules for verifying ID tokens without the Admin SDK: RS256 signed by a
key from Google's securetoken JWKS, `aud` is the Firebase project ID, `iss` is
https://securetoken.google.com/<project ID>, and `sub` and `auth_time` are present.
"""

from adapters.shared.jwt_identity import JwtIdentityProvider, SigningKeys

FIREBASE_JWKS_URL = (
    "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"
)


def firebase_identity_provider(
    project_id: str, keys: SigningKeys | None = None
) -> JwtIdentityProvider:
    """`keys` replaces the JWKS download in the contract tests; production leaves it unset."""
    return JwtIdentityProvider(
        jwks_url=FIREBASE_JWKS_URL,
        issuer=f"https://securetoken.google.com/{project_id}",
        audience=project_id,
        keys=keys,
    )
