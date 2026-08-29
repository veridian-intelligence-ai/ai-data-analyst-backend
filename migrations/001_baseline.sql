-- 001_baseline.sql — full baseline schema for the AI Data Analyst starter.
--
-- Idempotent by construction (IF NOT EXISTS everywhere): safe to run on a
-- fresh database or re-run on an existing one. Apply with:
--   psql "$DATABASE_URL" -f migrations/001_baseline.sql
--
-- Design notes:
-- * Single-tenant instance: there is deliberately NO clients/tenants table.
--   One deployment serves one organization; multi-tenancy is a different
--   product shape and pretending otherwise breeds dead abstractions.
-- * ai.chat_messages.message_id is BIGSERIAL — message ORDER is a database
--   guarantee (the sequence), never MAX(id)+1 computed in application code.
-- * ai.api_usage_events is both the billing meter and the rate-limit ledger.

-- ── Schemas ─────────────────────────────────────────────────────────────

CREATE SCHEMA IF NOT EXISTS auth;
CREATE SCHEMA IF NOT EXISTS ai;

-- ── auth: users + opaque sessions ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS auth.users (
    id          SERIAL PRIMARY KEY,
    email       VARCHAR(320) NOT NULL,
    name        VARCHAR(200) NOT NULL,
    role        VARCHAR(20)  NOT NULL DEFAULT 'member'
                CHECK (role IN ('admin', 'member')),
    status      VARCHAR(20)  NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended')),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Case-insensitive uniqueness: login matches lower(email), so uniqueness
-- must be enforced at the same casing, or two rows differing only by case
-- would make login ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_uq
    ON auth.users (lower(email));

CREATE TABLE IF NOT EXISTS auth.sessions (
    token       VARCHAR(200) PRIMARY KEY,
    user_id     INTEGER      NOT NULL REFERENCES auth.users (id),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ  NOT NULL,
    revoked_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS sessions_user_id_idx
    ON auth.sessions (user_id);

-- Backs DELETE /admin/sessions/cleanup (expired-session purge): without it
-- the cleanup is a full-table scan on the one table that only ever grows.
CREATE INDEX IF NOT EXISTS sessions_expires_at_idx
    ON auth.sessions (expires_at);

-- ── ai: conversations ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ai.chat_sessions (
    session_id                 VARCHAR(100) PRIMARY KEY,
    title                      VARCHAR(200),
    is_active                  BOOLEAN      NOT NULL DEFAULT TRUE,
    -- NOT NULL on purpose: an ownerless session was the access-control hole
    -- of the source system (NULL owner matched every user). Every session
    -- has exactly one owner, from birth.
    user_id                    INTEGER      NOT NULL REFERENCES auth.users (id),
    created_at                 TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_user_message_at       TIMESTAMPTZ,
    last_assistant_message_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS chat_sessions_user_id_idx
    ON ai.chat_sessions (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS ai.chat_messages (
    -- BIGSERIAL: the sequence IS the message order.
    message_id  BIGSERIAL    PRIMARY KEY,
    session_id  VARCHAR(100) NOT NULL REFERENCES ai.chat_sessions (session_id),
    role        VARCHAR(20)  NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT         NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chat_messages_session_idx
    ON ai.chat_messages (session_id, message_id);

-- ── ai: usage metering + rate limiting ──────────────────────────────────

CREATE TABLE IF NOT EXISTS ai.api_usage_events (
    id                  BIGSERIAL    PRIMARY KEY,
    user_id             INTEGER      NOT NULL REFERENCES auth.users (id),
    endpoint            VARCHAR(100) NOT NULL,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    input_tokens        INTEGER      NOT NULL DEFAULT 0,
    output_tokens       INTEGER      NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER      NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER      NOT NULL DEFAULT 0,
    latency_ms          INTEGER      NOT NULL DEFAULT 0,
    extra               JSONB        NOT NULL DEFAULT '{}'::jsonb
);

-- Serves the rate-limit query (user + endpoint + recent window).
CREATE INDEX IF NOT EXISTS api_usage_events_rate_idx
    ON ai.api_usage_events (user_id, endpoint, created_at DESC);

-- ── Rollback (manual — run only if you must remove the baseline) ────────
-- DROP TABLE IF EXISTS ai.api_usage_events;
-- DROP TABLE IF EXISTS ai.chat_messages;
-- DROP TABLE IF EXISTS ai.chat_sessions;
-- DROP TABLE IF EXISTS auth.sessions;
-- DROP TABLE IF EXISTS auth.users;
-- DROP SCHEMA IF EXISTS ai;
-- DROP SCHEMA IF EXISTS auth;
