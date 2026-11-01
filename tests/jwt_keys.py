"""Test-only signing keys and token builders for IdentityProvider tests."""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # the trusted key
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # an attacker's


@dataclass
class FakeSigningKey:
    key: Any


class FakeKeys:
    """Stands in for the JWKS download: always returns the public half of KEY."""

    def get_signing_key_from_jwt(self, token: str) -> FakeSigningKey:
        return FakeSigningKey(KEY.public_key())


def claims(issuer: str, audience: str, **overrides: Any) -> dict[str, Any]:
    """A valid claim set signed in an hour ago; an override of None removes that claim."""
    now = int(time.time())
    values: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": "user-123",
        "iat": now - 60,
        "exp": now + 3600,
        "auth_time": now - 3600,
    }
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def sign(values: dict[str, Any], key: Any = KEY, algorithm: str = "RS256") -> str:
    return jwt.encode(values, key, algorithm=algorithm, headers={"kid": "k1"})


def forge_hs256_with_public_key(values: dict[str, Any]) -> str:
    """Algorithm confusion: HS256 using the trusted public key as the HMAC secret.

    PyJWT refuses to build this, so it is assembled by hand, as an attacker would.
    """

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    secret = KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    body = b64(json.dumps(values).encode())
    signature = b64(hmac.new(secret, f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{signature}"
