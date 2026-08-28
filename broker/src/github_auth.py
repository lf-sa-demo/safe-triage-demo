"""GitHub App authentication helper for the safe-triage broker.

Generates a JWT signed with the App's private key (RS256) and exchanges it
for a short-lived installation token via the GitHub API. The token is cached
and refreshed 5 minutes before expiry.

Uses the ``cryptography`` library for JWT signing (already a safe-agents
dependency). stdlib ``urllib`` for the token exchange -- no requests/httpx.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15

_API_BASE = "https://api.github.com"
_TIMEOUT_SECONDS = 15
_USER_AGENT = "safe-triage-broker"

# Refresh the token when it has fewer than this many seconds remaining.
_REFRESH_MARGIN_SECONDS = 300


def _b64url(data: bytes) -> str:
    """Base64url encode without padding (per RFC 7515)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class GitHubAppAuth:
    """Authenticate as a GitHub App and obtain installation tokens.

    Parameters
    ----------
    app_id:
        The numeric GitHub App ID (as a string).
    private_key_path:
        Filesystem path to the PEM-encoded private key file.
    installation_id:
        The numeric installation ID for the target org/repo.
    """

    def __init__(self, app_id: str, private_key_path: str, installation_id: str) -> None:
        self._app_id = app_id
        self._installation_id = installation_id

        pem_bytes = Path(private_key_path).read_bytes()
        self._private_key = serialization.load_pem_private_key(pem_bytes, password=None)

        # Cached installation token and its expiry (epoch seconds).
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _generate_jwt(self) -> str:
        """Create a JWT for the GitHub App, valid for 10 minutes.

        The JWT is signed with RS256. The ``iat`` claim is backdated 60 seconds
        to allow for clock drift.
        """
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        payload = {
            "iss": self._app_id,
            "iat": now - 60,
            "exp": now + 600,  # 10 minutes max
        }

        segments = _b64url(json.dumps(header).encode()) + "." + _b64url(json.dumps(payload).encode())
        signature = self._private_key.sign(
            segments.encode("ascii"),
            PKCS1v15(),
            hashes.SHA256(),
        )
        return segments + "." + _b64url(signature)

    def _exchange_jwt_for_token(self, jwt: str) -> tuple[str, float]:
        """POST the JWT to GitHub and return (token, expires_at_epoch)."""
        url = f"{_API_BASE}/app/installations/{self._installation_id}/access_tokens"
        request = urllib.request.Request(  # noqa: S310
            url,
            data=b"",
            method="POST",
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
                body: dict[str, Any] = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise ValueError(
                f"GitHub App token exchange failed: HTTP {exc.code} -- "
                f"{exc.read().decode()}"
            ) from exc

        token = body["token"]
        # ``expires_at`` is ISO-8601 with a Z suffix, e.g. "2026-08-27T12:00:00Z".
        # Parse just enough to get epoch seconds for cache management.
        from datetime import datetime, timezone  # noqa: PLC0415

        expires_at = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
        return token, expires_at.timestamp()

    def get_installation_token(self) -> str:
        """Return a valid installation token, refreshing if needed.

        The token is cached until 5 minutes before its expiry time. Thread
        safety is not required here -- the broker's Doer calls this
        synchronously.
        """
        now = time.time()
        if self._token is not None and now < (self._token_expires_at - _REFRESH_MARGIN_SECONDS):
            return self._token

        jwt = self._generate_jwt()
        self._token, self._token_expires_at = self._exchange_jwt_for_token(jwt)
        return self._token
