"""Typed exceptions for the Power BI adapter.

Every exception carries a `context` dict with the evidence a caller (or the
LLM self-correction loop) needs: HTTP status, response body excerpt, the
query preview. The orchestrator serializes these into tool results.
"""
from __future__ import annotations

from typing import Any


class PowerBIError(Exception):
    """Base class for all Power BI adapter errors."""

    def __init__(self, message: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.context: dict[str, Any] = context or {}


class PowerBIConfigError(PowerBIError):
    """Missing or invalid configuration (env vars, empty inputs)."""


class PowerBIAuthError(PowerBIError):
    """Azure AD rejected the credentials, or Power BI rejected the token."""


class PowerBIAPIError(PowerBIError):
    """The Power BI REST API returned an error (invalid DAX, 404, 429, 5xx…)."""
