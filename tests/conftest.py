"""
Shared fixtures for the starter backend test suite.

Testing rules (mirroring the production lessons the app encodes):
- NO live externals, ever: Microsoft Entra, Power BI, Anthropic, WorkOS and
  Postgres are all replaced with fakes. A unit suite that needs credentials
  is a suite nobody runs.
- Fakes over MagicMock where the shape matters: a fake HTTP response class
  makes each test state exactly what the wire would have carried.
- Safe placeholder env vars are installed at import time so `app.main` (which
  builds CORS middleware at import) can always be imported; individual tests
  override or delete variables through `monkeypatch`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Make the backend root importable so `from app...` works when pytest is run
# from anywhere (the app itself needs no path hacks; tests live outside it).
_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

# Placeholder configuration: obviously fake, never routable. `app.main`
# resolves ALLOWED_ORIGINS at import time, and the pool would try DATABASE_URL
# only if a test forgets to fake the DB — db.invalid fails loudly, not silently.
os.environ.setdefault("ALLOWED_ORIGINS", "https://analyst.acme.example")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@db.invalid:5432/fake")  # sanitization-allow: placeholder DSN — fake credentials on the reserved, unroutable db.invalid host
os.environ.setdefault("POWERBI_DATASET_ID", "fake-dataset-id-33333333-3333-3333-3333-333333333333")


class FakeHTTPResponse:
    """
    Minimal stand-in for httpx.Response: just enough surface for the adapter
    (status_code, .json(), .text, .headers). `json_data=None` with
    `raise_json=True` simulates a non-JSON body.
    """

    def __init__(
        self,
        status_code: int,
        json_data: Any | None = None,
        text: str = "",
        headers: dict[str, str] | None = None,
        raise_json: bool = False,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or ("" if json_data is None else str(json_data))
        self.headers = headers or {}
        self._raise_json = raise_json

    def json(self) -> Any:
        if self._raise_json:
            raise ValueError("response body is not valid JSON")
        return self._json_data


@pytest.fixture
def fake_workspace_id() -> str:
    return "fake-workspace-id-22222222-2222-2222-2222-222222222222"


@pytest.fixture
def powerbi_env(monkeypatch: pytest.MonkeyPatch, fake_workspace_id: str) -> dict[str, str]:
    """Install the full set of fake POWERBI_* variables."""
    values = {
        "POWERBI_TENANT_ID": "fake-tenant-id-00000000-0000-0000-0000-000000000000",
        "POWERBI_CLIENT_ID": "fake-client-id-11111111-1111-1111-1111-111111111111",
        "POWERBI_CLIENT_SECRET": "fake-client-secret-not-a-real-secret",
        "POWERBI_WORKSPACE_ID": fake_workspace_id,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


@pytest.fixture
def token_payload() -> dict[str, Any]:
    """Microsoft Entra-shaped token response body."""
    return {
        "access_token": "fake-access-token-abc123",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
