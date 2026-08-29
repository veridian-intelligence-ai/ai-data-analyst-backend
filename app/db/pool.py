"""
Process-wide PostgreSQL connection pool (psycopg2 ThreadedConnectionPool).

Production lessons encoded here:

- ONE pool per process, lazily created. FastAPI sync endpoints run in a
  threadpool, so plain per-request `psycopg2.connect()` works fine in unit
  tests and then falls over under real concurrency — connection setup
  latency stacks up and the managed-Postgres connection limit gets eaten.
  Unit tests can't see pool exhaustion; only a pool with a hard MAX makes
  that failure mode explicit and testable.

- WAIT, then 503. `ThreadedConnectionPool.getconn()` raises immediately when
  the pool is exhausted. Failing a user request because of a 200ms burst is
  wrong; hanging forever is worse. So get_connection() retries with a short
  backoff up to DB_POOL_TIMEOUT and then raises PoolTimeout, which the app
  maps to HTTP 503 — an honest "busy, try again" instead of a mystery 500.

- SIZE FOR THE SHARED LIMIT. Every service pointing at the same managed
  Postgres instance shares ONE server-side connection limit. DB_POOL_MAX
  here must be budgeted against the sum across all services (API replicas,
  workers, migration jobs), not chosen in isolation.

- release_connection() ALWAYS rolls back. A connection returned mid-
  transaction poisons the next borrower with "current transaction is
  aborted" errors. Rollback on a clean connection is a no-op; on a dirty
  one it is the fix. It never closes — the pool owns the sockets.
"""
from __future__ import annotations

import atexit
import os
import threading
import time

import psycopg2
import psycopg2.pool

_RETRY_BACKOFF_SECONDS = 0.05

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


class PoolTimeout(Exception):
    """Raised when no connection became free within DB_POOL_TIMEOUT."""


def _pool_timeout_seconds() -> float:
    return float(os.getenv("DB_POOL_TIMEOUT", "10"))


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Lazily create the process-wide pool (thread-safe, double-checked)."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                dsn = os.getenv("DATABASE_URL", "").strip()
                if not dsn:
                    raise RuntimeError("DATABASE_URL is not set")
                min_conn = int(os.getenv("DB_POOL_MIN", "1"))
                max_conn = int(os.getenv("DB_POOL_MAX", "10"))
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=min_conn, maxconn=max_conn, dsn=dsn
                )
                print(f"[db.pool] pool created (min={min_conn}, max={max_conn})")
    return _pool


def get_connection():
    """
    Borrow a connection, waiting up to DB_POOL_TIMEOUT for one to free up.

    Raises PoolTimeout when the pool stays exhausted — the caller maps it to
    HTTP 503. Never returns a half-open connection.
    """
    pool = _get_pool()
    deadline = time.monotonic() + _pool_timeout_seconds()
    while True:
        try:
            return pool.getconn()
        except psycopg2.pool.PoolError:
            if time.monotonic() >= deadline:
                raise PoolTimeout(
                    "No database connection became free within "
                    f"{_pool_timeout_seconds():.0f}s (pool exhausted)"
                ) from None
            time.sleep(_RETRY_BACKOFF_SECONDS)


def release_connection(conn) -> None:
    """
    Return a connection to the pool.

    Rolls back any open transaction first (no-op when clean) and NEVER
    closes: closing would shrink the pool one socket at a time until it
    starves. Best-effort — a broken connection is handed back to the pool,
    which discards it.
    """
    global _pool
    if conn is None or _pool is None:
        return
    try:
        conn.rollback()
    except Exception as exc:
        print(f"[db.pool] rollback on release failed: {exc}")
    try:
        _pool.putconn(conn)
    except Exception as exc:
        print(f"[db.pool] putconn failed: {exc}")


def close_pool() -> None:
    """Close every connection. Call once, at application shutdown."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.closeall()
                print("[db.pool] pool closed")
            except Exception as exc:
                print(f"[db.pool] closeall failed: {exc}")
            _pool = None


# Safety net for interpreter paths where the FastAPI lifespan never runs
# (one-off scripts importing the store, a crashed startup): close_pool is
# idempotent, so lifespan + atexit double-closing is harmless.
atexit.register(close_pool)
