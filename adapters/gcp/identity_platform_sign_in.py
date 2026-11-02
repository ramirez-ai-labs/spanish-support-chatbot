"""SignInLinkSender for GCP: Identity Platform's sendOobCode, called as the runtime account.

No API key: the call carries the account's OAuth token, and the account holds only the bootstrap
stack's `signInEmailSender` role (firebaseauth.users.sendEmail). It can send sign-in emails but
can't read, change or delete users.
"""

import threading
from collections.abc import Callable

import httpx

from core.sign_in import SignInLinkFailed

SEND_URL = "https://identitytoolkit.googleapis.com/v1/projects/{project}/accounts:sendOobCode"
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# Identity Platform's error codes, which start its error message (e.g. "INVALID_EMAIL : ...").
REASONS = {
    "INVALID_EMAIL": "invalid_email",
    "MISSING_EMAIL": "invalid_email",
    "TOO_MANY_ATTEMPTS_TRY_LATER": "too_many_attempts",
}


def default_token() -> Callable[[], str]:
    """The runtime account's access token (Application Default Credentials), refreshed as needed."""
    import google.auth
    import google.auth.transport.requests

    credentials, _ = google.auth.default(scopes=SCOPES)
    lock = threading.Lock()

    def token() -> str:
        with lock:  # requests run in worker threads; refresh once, not per thread
            if not credentials.valid:
                credentials.refresh(google.auth.transport.requests.Request())  # type: ignore[no-untyped-call]
            return str(credentials.token)

    return token


class IdentityPlatformSignInLinkSender:
    def __init__(
        self,
        project_id: str,
        continue_url: str,
        client: httpx.Client | None = None,
        token: Callable[[], str] | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        if not project_id or not continue_url.startswith("https://"):
            raise ValueError("Set GOOGLE_CLOUD_PROJECT and an https SIGN_IN_CONTINUE_URL")
        self._url = SEND_URL.format(project=project_id)
        self._continue_url = continue_url
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._token = token or default_token()

    def send(self, email: str) -> None:
        try:
            response = self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._token()}"},
                json={
                    "requestType": "EMAIL_SIGNIN",
                    "email": email,
                    "continueUrl": self._continue_url,
                    "canHandleCodeInApp": True,
                },
            )
        except Exception as exc:  # network or credential errors: fail closed, by type only
            raise SignInLinkFailed("sign_in_unavailable") from exc
        if response.is_success:
            return
        try:
            code = str(response.json()["error"]["message"]).split(" ")[0]
        except (ValueError, KeyError, TypeError):
            code = ""
        if response.status_code == 429:
            code = "TOO_MANY_ATTEMPTS_TRY_LATER"
        raise SignInLinkFailed(REASONS.get(code, "sign_in_unavailable"))
