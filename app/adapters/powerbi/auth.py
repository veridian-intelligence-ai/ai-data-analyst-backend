"""
OAuth2 client-credentials authenticator for Power BI (Microsoft Entra ID).

- Reads credentials from the environment; fails fast at construction when any
  are missing (the service must never boot half-configured).
- Caches the token in memory until expiry, with a 60-second safety buffer so
  a request never goes out with a token about to die mid-flight.
- Never logs secrets: identifiers are masked to their first characters.
"""
from __future__ import annotations

import os
import time

import httpx

from app.adapters.powerbi.exceptions import PowerBIAuthError, PowerBIConfigError

_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
# The .default scope grants whatever API permissions the app registration has.
_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_EXPIRATION_SAFETY_BUFFER_SECONDS = 60


def _mask(value: str, keep: int = 8) -> str:
    """Return only the first `keep` characters, for safe logging."""
    if not value:
        return "<empty>"
    return f"{value[:keep]}..."


class PowerBIAuthenticator:
    """OAuth2 client-credentials flow with in-memory token cache."""

    def __init__(self) -> None:
        tenant_id = os.getenv("POWERBI_TENANT_ID", "").strip()
        client_id = os.getenv("POWERBI_CLIENT_ID", "").strip()
        client_secret = os.getenv("POWERBI_CLIENT_SECRET", "").strip()

        missing = [
            name
            for name, value in (
                ("POWERBI_TENANT_ID", tenant_id),
                ("POWERBI_CLIENT_ID", client_id),
                ("POWERBI_CLIENT_SECRET", client_secret),
            )
            if not value
        ]
        if missing:
            raise PowerBIConfigError(
                f"Missing environment variables for the Power BI adapter: {', '.join(missing)}",
                context={"missing": missing},
            )

        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._cached_token: str | None = None
        self._token_expires_at: float = 0.0

        print(f"[powerbi.auth] authenticator initialized for tenant {_mask(tenant_id)}")

    def _is_token_valid(self) -> bool:
        if not self._cached_token:
            return False
        return time.time() < (self._token_expires_at - _EXPIRATION_SAFETY_BUFFER_SECONDS)

    def _request_new_token(self) -> None:
        url = _TOKEN_URL_TEMPLATE.format(tenant_id=self._tenant_id)
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": _SCOPE,
        }

        try:
            response = httpx.post(url, data=data, timeout=30.0)
        except httpx.HTTPError as exc:
            raise PowerBIAuthError(
                "Network error contacting Microsoft Entra for a token.",
                context={"error": str(exc)},
            ) from exc

        if response.status_code != 200:
            raise PowerBIAuthError(
                f"Microsoft Entra returned status {response.status_code} for the token request.",
                context={"status_code": response.status_code, "body": response.text[:500]},
            )

        try:
            payload = response.json()
            access_token = payload["access_token"]
            expires_in = int(payload.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError) as exc:
            raise PowerBIAuthError(
                "Microsoft Entra token response has an unexpected shape.",
                context={"error": str(exc)},
            ) from exc

        self._cached_token = access_token
        self._token_expires_at = time.time() + expires_in

    def get_token(self) -> str:
        """Return a valid token, refreshing it when needed."""
        if self._is_token_valid():
            assert self._cached_token is not None
            return self._cached_token

        is_refresh = self._cached_token is not None
        self._request_new_token()

        assert self._cached_token is not None
        remaining = int(self._token_expires_at - time.time())
        if is_refresh:
            print("[powerbi.auth] token refreshed")
        else:
            print(f"[powerbi.auth] token acquired, expires in {remaining}s")
        return self._cached_token
