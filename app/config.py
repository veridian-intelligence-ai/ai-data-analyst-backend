"""
Tiny configuration helpers — fail fast, fail loud.

Production lesson: a service that boots half-configured produces confusing
runtime errors hours later (a 401 from Power BI, a hung DB connect) that are
much harder to diagnose than a crash at startup naming the missing variable.
Every required setting goes through require_env() so misconfiguration is a
one-line fix, not an investigation.
"""
from __future__ import annotations

import os


def require_env(name: str) -> str:
    """Return the env var's value, or raise RuntimeError naming it."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable {name} is not set. "
            f"See .env.example for the full list."
        )
    return value


def get_allowed_origins() -> list[str]:
    """
    Parse ALLOWED_ORIGINS (comma-separated) into an explicit origin list.

    There is deliberately NO wildcard fallback: this API uses credentialed
    requests, and `allow_origins=["*"]` with credentials is both forbidden by
    the CORS spec and a real security hole when a framework "helpfully" makes
    it work. An empty/missing value is a configuration error, not a default.
    """
    raw = os.getenv("ALLOWED_ORIGINS", "").strip()
    origins = [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]
    if not origins:
        raise RuntimeError(
            "ALLOWED_ORIGINS is not set (comma-separated list of explicit "
            "origins, e.g. https://analyst.acme.example). Refusing to boot "
            "with no CORS policy — never use a wildcard with credentials."
        )
    return origins
