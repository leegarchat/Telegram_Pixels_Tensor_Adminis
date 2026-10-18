"""SQLite persistence for channel-post links, moderation queue, bans and cooldowns.

Tables:
  post_links — maps a moderation-topic message to the published channel message,
    so /change and /remove can find the channel post later. Also stores the
    original author (user id + display tag) so moderators can /ban by replying
    to already-published topic messages, not only to pending submissions.
  pending_submissions — durable moderation queue; each submission is stored twice
    (once per header message id and once per content message id) so moderators
    can reply to either message with /approve, /cancel or /ban.
  bans — per-topic and global user bans. A row with family_key='*global*'
    blocks the user bot-wide (/pban); otherwise it blocks a single topic
    family (/ban). expires_at=NULL means a permanent ban.

All tables survive bot restarts, unlike the in-memory FSM storage.
"""
# sqlite3: embedded DB; closing: safe connection cleanup; Path: DB file location; time: timestamps.
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


# Default database file next to the project root: <root>/tgadmin.sqlite3.
DATABASE_PATH = Path(__file__).resolve().parent.parent / "tgadmin.sqlite3"

# Special family_key marking a bot-wide (global) ban created via /pban.
GLOBAL_BAN_KEY = "*global*"


class PostLinkDatabase:
    """Thin SQLite wrapper for post links, the pending queue and bans."""

    def __init__(self, path: Path = DATABASE_PATH):
        """Bind to a database file and create/migrate tables when missing."""
        self.path = path
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        """Open a new connection with Row factory and a 10s busy timeout."""
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _table_columns(self, connection: sqlite3.Connection, table: str) -> set[str]:
        """Return the existing column names of a table (empty set when missing)."""
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"] for row in rows}

    def initialize(self) -> None:
        """Create tables/indexes with IF NOT EXISTS and apply light migrations."""
        with closing(self._connect()) as connection:
            with connection:
                # Maps mod-group messages to published channel messages.
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS post_links (
                        source_chat_id INTEGER NOT NULL,
                        source_message_id INTEGER NOT NULL,
                        source_thread_id INTEGER,
                        channel_id INTEGER NOT NULL,
                        channel_message_id INTEGER NOT NULL,
                        media_type TEXT NOT NULL DEFAULT 'text',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (source_chat_id, source_message_id)
                    )
                    """
                )
                # Fast lookup of links by their channel-side target (for /change by id).
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_post_links_target "
                    "ON post_links(channel_id, channel_message_id)"
                )
                # Durable moderation queue; two rows per submission (header + content ids).
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_submissions (
                        mod_chat_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        content_message_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        family_key TEXT NOT NULL,
                        text TEXT NOT NULL,
                        photo_id TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (mod_chat_id, message_id)
                    )
                    """
                )
                # Fast deletion of both rows by content message id.
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pending_content "
                    "ON pending_submissions(mod_chat_id, content_message_id)"
                )
                # Per-topic + global bans. NULL expires_at = permanent ban.
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bans (
                        user_id INTEGER NOT NULL,
                        family_key TEXT NOT NULL,
                        username TEXT,
                        reason TEXT,
                        banned_by INTEGER,
                        banned_at INTEGER NOT NULL,
                        expires_at INTEGER,
                        PRIMARY KEY (user_id, family_key)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_bans_user "
                    "ON bans(user_id)"
                )
                # --- Lightweight migrations for pre-existing databases ---
                post_link_cols = self._table_columns(connection, "post_links")
                if "author_user_id" not in post_link_cols:
                    connection.execute("ALTER TABLE post_links ADD COLUMN author_user_id INTEGER")
                if "author_username" not in post_link_cols:
                    connection.execute("ALTER TABLE post_links ADD COLUMN author_username TEXT")
                pending_cols = self._table_columns(connection, "pending_submissions")
                if "username" not in pending_cols:
                    connection.execute("ALTER TABLE pending_submissions ADD COLUMN username TEXT")

    # ------------------------------------------------------------------
    # post_links
    # ------------------------------------------------------------------

    def add_link(
        self,
        source_chat_id: int,
        source_message_id: int,
        source_thread_id: int | None,
        channel_id: int,
        channel_message_id: int,
        media_type: str = "text",
        author_user_id: int | None = None,
        author_username: str | None = None,
    ) -> None:
        """Insert or replace a topic-message -> channel-message mapping (upsert)."""
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO post_links (
                        source_chat_id, source_message_id, source_thread_id,
                        channel_id, channel_message_id, media_type,
                        author_user_id, author_username
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_chat_id, source_message_id) DO UPDATE SET
                        source_thread_id = excluded.source_thread_id,
                        channel_id = excluded.channel_id,
                        channel_message_id = excluded.channel_message_id,
                        media_type = excluded.media_type,
                        author_user_id = excluded.author_user_id,
                        author_username = excluded.author_username,
                        created_at = CURRENT_TIMESTAMP
                    """,
                    (
                        source_chat_id,
                        source_message_id,
                        source_thread_id,
                        channel_id,
                        channel_message_id,
                        media_type,
                        author_user_id,
                        author_username,
                    ),
                )

    def find_by_source(self, source_chat_id: int, source_message_id: int) -> dict[str, Any] | None:
        """Look up a link by the mod-group message that produced it."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM post_links WHERE source_chat_id = ? AND source_message_id = ?",
                (source_chat_id, source_message_id),
            ).fetchone()
        return dict(row) if row else None

    def find_by_channel_post(self, channel_id: int, channel_message_id: int) -> dict[str, Any] | None:
        """Look up a link by the published channel message (for /change by id/URL)."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM post_links
                WHERE channel_id = ? AND channel_message_id = ?
                ORDER BY source_chat_id, source_message_id
                LIMIT 1
                """,
                (channel_id, channel_message_id),
            ).fetchone()
        return dict(row) if row else None

    def delete_target(self, channel_id: int, channel_message_id: int) -> int:
        """Delete link rows pointing at a channel post; returns rows removed."""
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM post_links WHERE channel_id = ? AND channel_message_id = ?",
                    (channel_id, channel_message_id),
                )
                return cursor.rowcount

    # ------------------------------------------------------------------
    # pending_submissions
    # ------------------------------------------------------------------

    def add_pending_submission(
        self,
        mod_chat_id: int,
        header_message_id: int,
        content_message_id: int,
        user_id: int,
        family_key: str,
        text: str,
        photo_id: str | None,
        username: str | None = None,
    ) -> None:
        """Store a submission under BOTH message ids (header and content).

        Writing two rows with the same payload lets moderators reply to either
        the header ("New Submission...") or the content message itself — both
        lookups hit the same submission data. The author tag is stored so that
        /ban_list and ban receipts can display it without extra API calls.
        """
        # Set of (chat, message) keys to upsert: header row + content row.
        rows = {(mod_chat_id, content_message_id), (mod_chat_id, header_message_id)}
        with closing(self._connect()) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT INTO pending_submissions (
                        mod_chat_id, message_id, content_message_id,
                        user_id, family_key, text, photo_id, username
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(mod_chat_id, message_id) DO UPDATE SET
                        content_message_id = excluded.content_message_id,
                        user_id = excluded.user_id,
                        family_key = excluded.family_key,
                        text = excluded.text,
                        photo_id = excluded.photo_id,
                        username = excluded.username,
                        created_at = CURRENT_TIMESTAMP
                    """,
                    [
                        (
                            chat_id,
                            message_id,
                            content_message_id,
                            user_id,
                            family_key,
                            text,
                            photo_id,
                            username,
                        )
                        for chat_id, message_id in rows
                    ],
                )

    def get_pending_submission(
        self, mod_chat_id: int, message_id: int
    ) -> dict[str, Any] | None:
        """Fetch a queued submission by either its header or content message id."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM pending_submissions
                WHERE mod_chat_id = ? AND message_id = ?
                """,
                (mod_chat_id, message_id),
            ).fetchone()
        return dict(row) if row else None

    def delete_pending_submission(self, mod_chat_id: int, content_message_id: int) -> int:
        """Remove both rows of a submission by content id; returns rows removed."""
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    DELETE FROM pending_submissions
                    WHERE mod_chat_id = ? AND content_message_id = ?
                    """,
                    (mod_chat_id, content_message_id),
                )
                return cursor.rowcount

    # ------------------------------------------------------------------
    # bans (per-topic + global)
    # ------------------------------------------------------------------

    def add_ban(
        self,
        user_id: int,
        family_key: str,
        username: str | None = None,
        reason: str | None = None,
        banned_by: int | None = None,
        duration_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Create or replace a ban; returns the stored row as a dict.

        Args:
            user_id: Banned Telegram user id.
            family_key: Topic family key, or GLOBAL_BAN_KEY for a bot-wide ban.
            username: Display tag (@username or full name) for /ban_list.
            reason: Optional moderator-provided reason.
            banned_by: Moderator user id that issued the ban.
            duration_seconds: None or <=0 means permanent; otherwise expires
                banned_at + duration.
        """
        now = int(time.time())
        expires_at = now + int(duration_seconds) if duration_seconds and duration_seconds > 0 else None
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO bans (
                        user_id, family_key, username, reason,
                        banned_by, banned_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, family_key) DO UPDATE SET
                        username = excluded.username,
                        reason = excluded.reason,
                        banned_by = excluded.banned_by,
                        banned_at = excluded.banned_at,
                        expires_at = excluded.expires_at
                    """,
                    (user_id, family_key, username, reason, banned_by, now, expires_at),
                )
        return self.get_ban(user_id, family_key) or {
            "user_id": user_id,
            "family_key": family_key,
            "username": username,
            "reason": reason,
            "banned_by": banned_by,
            "banned_at": now,
            "expires_at": expires_at,
        }

    def remove_ban(self, user_id: int, family_key: str) -> int:
        """Remove a ban; returns the number of rows deleted (0 or 1)."""
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM bans WHERE user_id = ? AND family_key = ?",
                    (user_id, family_key),
                )
                return cursor.rowcount

    def get_ban(self, user_id: int, family_key: str) -> dict[str, Any] | None:
        """Fetch one ban row; auto-deletes it when it has already expired."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM bans WHERE user_id = ? AND family_key = ?",
                (user_id, family_key),
            ).fetchone()
            data = dict(row) if row else None
            if data and data.get("expires_at") is not None and data["expires_at"] <= int(time.time()):
                with connection:
                    connection.execute(
                        "DELETE FROM bans WHERE user_id = ? AND family_key = ?",
                        (user_id, family_key),
                    )
                return None
        return data

    def is_banned(self, user_id: int, family_key: str | None = None) -> dict[str, Any] | None:
        """Return the effective ban for a user: global ban wins over a topic ban.

        Args:
            user_id: Telegram user id to check.
            family_key: Topic family being accessed, or None to check only global.

        Returns:
            The matching ban row, or None when the user may proceed.
        """
        # A global ban blocks every topic, so it is always checked first.
        global_ban = self.get_ban(user_id, GLOBAL_BAN_KEY)
        if global_ban:
            return global_ban
        if family_key and family_key != GLOBAL_BAN_KEY:
            return self.get_ban(user_id, family_key)
        return None

    def list_bans(self, family_key: str | None = None) -> list[dict[str, Any]]:
        """List non-expired bans, optionally scoped to one family (+global rows).

        Expired rows are pruned lazily on read so /ban_list never shows stale entries.
        """
        now = int(time.time())
        with closing(self._connect()) as connection:
            with connection:
                # Drop expired temporary bans before listing.
                connection.execute("DELETE FROM bans WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
            if family_key:
                rows = connection.execute(
                    "SELECT * FROM bans WHERE family_key = ? OR family_key = ? "
                    "ORDER BY family_key, banned_at DESC",
                    (family_key, GLOBAL_BAN_KEY),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM bans ORDER BY family_key, banned_at DESC"
                ).fetchall()
        return [dict(row) for row in rows]


# Global singleton: import `post_links` anywhere to access the SQLite store.
post_links = PostLinkDatabase()
