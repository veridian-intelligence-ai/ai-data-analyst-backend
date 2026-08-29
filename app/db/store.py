"""
ConversationStore — all persistence for the chat API, over the schema in
migrations/001_baseline.sql (schemas `ai` + `auth`).

Design rules validated in production:

- MESSAGE ORDER IS THE PRIMARY KEY. `ai.chat_messages.message_id` is a
  BIGSERIAL and every read orders by it. The source system computed the next
  id with MAX(message_id)+1 in application code — two concurrent requests on
  the same session could mint the same id and interleave history. A sequence
  makes ordering a database guarantee, not an application hope.

- Assistant messages are stored as a JSON ENVELOPE
  {message_type, answer, response_mode, visual, next_step_suggestions} so a
  session can be replayed with its visuals intact. get_history() unwraps the
  envelope down to the plain answer text — the LLM only ever sees prose.

- Usage metering lives in `ai.api_usage_events` and doubles as the rate-limit
  ledger: one table, one source of truth for both billing and throttling.

- emit_usage_event() NEVER raises: metering must not take down the answer
  the user already paid the latency for.
"""
from __future__ import annotations

import json
from typing import Any

from app.db.pool import get_connection, release_connection

ASSISTANT_ENVELOPE_TYPE = "assistant_response"

_TITLE_FALLBACK_CHARS = 60


class ConversationStore:
    """CRUD + metering over ai.chat_sessions / ai.chat_messages / ai.api_usage_events."""

    # ── sessions ────────────────────────────────────────────────────────

    def ensure_session(self, session_id: str, user_id: int) -> None:
        """Create the session row if it doesn't exist (idempotent)."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ai.chat_sessions (session_id, user_id)
                    VALUES (%s, %s)
                    ON CONFLICT (session_id) DO NOTHING
                    """,
                    (session_id, user_id),
                )
            conn.commit()
        finally:
            release_connection(conn)

    def check_session_owner(self, session_id: str, user_id: int) -> bool:
        """
        True only when the session exists AND belongs to exactly this user.

        STRICT equality, no NULL fallback — deliberately. The source system
        treated a NULL owner as "accessible to anyone" so legacy rows kept
        working; that meant any authenticated user could read any legacy
        conversation by guessing its id. Here an ownerless row matches
        nobody: `user_id` is NOT NULL in the schema and this check would
        still reject NULL if one ever appeared.
        """
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM ai.chat_sessions WHERE session_id = %s",
                    (session_id,),
                )
                row = cur.fetchone()
            return row is not None and row[0] == user_id
        finally:
            release_connection(conn)

    def list_sessions(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """
        List the user's sessions, most recently active first.

        Title fallback: sessions are created before the user is asked to name
        them, so an unnamed session shows its first user message truncated to
        60 chars — enough for a sidebar, cheap to compute in SQL.

        Hygiene filters, both learned in production: sessions with no user
        message yet stay hidden (ensure_session runs before the first message
        lands, and a crash between the two would otherwise leave a permanent
        empty "New conversation" row), and smoke-test sessions
        (scripts/smoke_chat.py uses 'smoke-…' ids against the REAL deployment
        with a real user token) must never surface in that user's sidebar.
        The LIMIT keeps the sidebar payload bounded for long-lived accounts.
        """
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        s.session_id,
                        COALESCE(
                            s.title,
                            LEFT((SELECT m.content
                                  FROM ai.chat_messages m
                                  WHERE m.session_id = s.session_id
                                    AND m.role = 'user'
                                  ORDER BY m.message_id
                                  LIMIT 1), %s),
                            'New conversation'
                        ) AS title,
                        s.is_active,
                        s.created_at,
                        s.updated_at,
                        s.last_user_message_at,
                        s.last_assistant_message_at
                    FROM ai.chat_sessions s
                    WHERE s.user_id = %s
                      AND s.last_user_message_at IS NOT NULL
                      AND s.session_id NOT LIKE 'smoke-%%'
                      AND s.session_id NOT LIKE 'test-%%'
                    ORDER BY s.updated_at DESC
                    LIMIT %s
                    """,
                    (_TITLE_FALLBACK_CHARS, user_id, limit),
                )
                rows = cur.fetchall()
            return [
                {
                    "session_id": r[0],
                    "title": r[1],
                    "is_active": r[2],
                    "created_at": r[3].isoformat() if r[3] else None,
                    "updated_at": r[4].isoformat() if r[4] else None,
                    "last_user_message_at": r[5].isoformat() if r[5] else None,
                    "last_assistant_message_at": r[6].isoformat() if r[6] else None,
                }
                for r in rows
            ]
        finally:
            release_connection(conn)

    def rename_session(self, session_id: str, title: str) -> None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ai.chat_sessions
                    SET title = %s, updated_at = now()
                    WHERE session_id = %s
                    """,
                    (title.strip()[:200], session_id),
                )
            conn.commit()
        finally:
            release_connection(conn)

    def delete_session(self, session_id: str) -> None:
        """Delete a session and all its messages (messages first — FK)."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM ai.chat_messages WHERE session_id = %s", (session_id,)
                )
                cur.execute(
                    "DELETE FROM ai.chat_sessions WHERE session_id = %s", (session_id,)
                )
            conn.commit()
        finally:
            release_connection(conn)

    def clear_session(self, session_id: str) -> None:
        """Delete a session's messages but keep the session row (a 'reset').

        The activity timestamps are nulled with the messages: after a reset
        the session must look message-less — list_sessions hides rows with
        last_user_message_at IS NULL, and reporting activity times for
        deleted messages is a lie the API would otherwise keep telling.
        """
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM ai.chat_messages WHERE session_id = %s", (session_id,)
                )
                cur.execute(
                    """
                    UPDATE ai.chat_sessions
                    SET updated_at = now(),
                        last_user_message_at = NULL,
                        last_assistant_message_at = NULL
                    WHERE session_id = %s
                    """,
                    (session_id,),
                )
            conn.commit()
        finally:
            release_connection(conn)

    # ── messages ────────────────────────────────────────────────────────

    def append_message(self, session_id: str, role: str, content: str) -> None:
        """
        Append one message. message_id comes from the BIGSERIAL sequence —
        NEVER computed in application code (see module docstring).
        """
        if role not in ("user", "assistant"):
            raise ValueError(f"invalid role: {role}")
        timestamp_col = (
            "last_user_message_at" if role == "user" else "last_assistant_message_at"
        )
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ai.chat_messages (session_id, role, content)
                    VALUES (%s, %s, %s)
                    """,
                    (session_id, role, content),
                )
                cur.execute(
                    f"""
                    UPDATE ai.chat_sessions
                    SET updated_at = now(), {timestamp_col} = now()
                    WHERE session_id = %s
                    """,
                    (session_id,),
                )
            conn.commit()
        finally:
            release_connection(conn)

    def append_assistant_message(
        self,
        session_id: str,
        answer: str,
        response_mode: str,
        visual: dict[str, Any] | None,
        next_step_suggestions: list[str],
    ) -> None:
        """Append the assistant turn as its JSON envelope (see module docstring)."""
        envelope = {
            "message_type": ASSISTANT_ENVELOPE_TYPE,
            "answer": answer,
            "response_mode": response_mode,
            "visual": visual,
            "next_step_suggestions": next_step_suggestions,
        }
        self.append_message(
            session_id, "assistant", json.dumps(envelope, ensure_ascii=False)
        )

    def get_history(self, session_id: str, limit: int = 12) -> list[dict[str, str]]:
        """
        Return the last `limit` messages in chronological order, as plain
        {role, content} pairs ready for the orchestrator.

        Assistant envelopes are unwrapped to their `answer` text: the LLM
        needs the prose it said, not the JSON the frontend rendered.
        """
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT role, content
                    FROM ai.chat_messages
                    WHERE session_id = %s
                    ORDER BY message_id DESC
                    LIMIT %s
                    """,
                    (session_id, limit),
                )
                rows = cur.fetchall()
        finally:
            release_connection(conn)

        history: list[dict[str, str]] = []
        for role, content in reversed(rows):
            history.append({"role": role, "content": self._unwrap_content(role, content)})
        return history

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """
        Return ALL messages for the frontend, envelopes decoded to their
        structured form (visual + suggestions preserved for replay).
        """
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT message_id, role, content, created_at
                    FROM ai.chat_messages
                    WHERE session_id = %s
                    ORDER BY message_id
                    """,
                    (session_id,),
                )
                rows = cur.fetchall()
        finally:
            release_connection(conn)

        messages: list[dict[str, Any]] = []
        for message_id, role, content, created_at in rows:
            entry: dict[str, Any] = {
                "message_id": message_id,
                "role": role,
                "created_at": created_at.isoformat() if created_at else None,
            }
            envelope = self._parse_envelope(role, content)
            if envelope is not None:
                entry["content"] = envelope.get("answer", "")
                entry["response_mode"] = envelope.get("response_mode", "text")
                entry["visual"] = envelope.get("visual")
                entry["next_step_suggestions"] = envelope.get("next_step_suggestions", [])
            else:
                entry["content"] = content
            messages.append(entry)
        return messages

    @classmethod
    def _unwrap_content(cls, role: str, content: str) -> str:
        envelope = cls._parse_envelope(role, content)
        if envelope is not None:
            return str(envelope.get("answer", ""))
        return content

    @staticmethod
    def _parse_envelope(role: str, content: str) -> dict[str, Any] | None:
        if role != "assistant" or not content.startswith("{"):
            return None
        try:
            data = json.loads(content)
        except ValueError:
            return None
        if isinstance(data, dict) and data.get("message_type") == ASSISTANT_ENVELOPE_TYPE:
            return data
        return None

    # ── metering + rate limiting ────────────────────────────────────────

    def check_rate_limit(
        self,
        user_id: int,
        endpoint: str,
        per_min: int = 20,
        per_hour: int = 300,
    ) -> bool:
        """
        True when the user is under both limits for this endpoint.

        Reads the usage ledger itself — no extra infrastructure (Redis,
        middleware state), and the limit is enforced per PAID endpoint at
        the point where money is about to be spent. A single-tenant instance
        does not need more than this.
        """
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE created_at > now() - interval '1 minute'),
                        COUNT(*) FILTER (WHERE created_at > now() - interval '1 hour')
                    FROM ai.api_usage_events
                    WHERE user_id = %s AND endpoint = %s
                      AND created_at > now() - interval '1 hour'
                    """,
                    (user_id, endpoint),
                )
                minute_count, hour_count = cur.fetchone()
            if minute_count >= per_min or hour_count >= per_hour:
                print(
                    f"[store] rate limit hit user={user_id} endpoint={endpoint} "
                    f"minute={minute_count}/{per_min} hour={hour_count}/{per_hour}"
                )
                return False
            return True
        finally:
            release_connection(conn)

    def emit_usage_event(
        self,
        user_id: int,
        endpoint: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        latency_ms: int = 0,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """
        Record one metered call. BEST-EFFORT: catches everything and only
        logs — the user's answer must never fail because metering did.
        """
        try:
            conn = get_connection()
        except Exception as exc:
            print(f"[store] emit_usage_event skipped (no connection): {exc}")
            return
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ai.api_usage_events
                        (user_id, endpoint, input_tokens, output_tokens,
                         cache_read_tokens, cache_write_tokens, latency_ms, extra)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        endpoint,
                        input_tokens,
                        output_tokens,
                        cache_read_tokens,
                        cache_write_tokens,
                        latency_ms,
                        json.dumps(extra or {}, ensure_ascii=False, default=str),
                    ),
                )
            conn.commit()
        except Exception as exc:
            print(f"[store] emit_usage_event failed (ignored): {exc}")
        finally:
            release_connection(conn)
