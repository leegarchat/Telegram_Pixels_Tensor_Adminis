"""SQLite persistence for channel-post links and pending moderation submissions.

Tables:
  post_links — maps a moderation-topic message to the published channel message,
    so /change and /remove can find the channel post later.
  pending_submissions — durable moderation queue; each submission is stored twice
    (once per header message id and once per content message id) so moderators
    can reply to either message with /approve or /cancel.

Both tables survive bot restarts, unlike the in-memory FSM storage.
"""
# sqlite3: embedded DB; closing: safe connection cleanup; Path: DB file location.
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


# Default database file next to the project root: <root>/tgadmin.sqlite3.
DATABASE_PATH = Path(__file__).resolve().parent.parent / "tgadmin.sqlite3"


class PostLinkDatabase:
    """Thin SQLite wrapper for post links and the pending-submission queue."""

    def __init__(self, path: Path = DATABASE_PATH):
        """Bind to a database file and create tables/indexes when missing."""
        self.path = path
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        """Open a new connection with Row factory and a 10s busy timeout."""
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        """Create tables and indexes with IF NOT EXISTS (safe to call repeatedly)."""
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

    def add_link(
        self,
        source_chat_id: int,
        source_message_id: int,
        source_thread_id: int | None,
        channel_id: int,
        channel_message_id: int,
        media_type: str = "text",
    ) -> None:
        """Insert or replace a topic-message -> channel-message mapping (upsert)."""
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO post_links (
                        source_chat_id, source_message_id, source_thread_id,
                        channel_id, channel_message_id, media_type
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_chat_id, source_message_id) DO UPDATE SET
                        source_thread_id = excluded.source_thread_id,
                        channel_id = excluded.channel_id,
                        channel_message_id = excluded.channel_message_id,
                        media_type = excluded.media_type,
                        created_at = CURRENT_TIMESTAMP
                    """,
                    (
                        source_chat_id,
                        source_message_id,
                        source_thread_id,
                        channel_id,
                        channel_message_id,
                        media_type,
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

    def add_pending_submission(
        self,
        mod_chat_id: int,
        header_message_id: int,
        content_message_id: int,
        user_id: int,
        family_key: str,
        text: str,
        photo_id: str | None,
    ) -> None:
        """Store a submission under BOTH message ids (header and content).

        Writing two rows with the same payload lets moderators reply to either
        the header ("New Submission...") or the content message itself — both
        lookups hit the same submission data.
        """
        # Set of (chat, message) keys to upsert: header row + content row.
        rows = {(mod_chat_id, content_message_id), (mod_chat_id, header_message_id)}
        with closing(self._connect()) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT INTO pending_submissions (
                        mod_chat_id, message_id, content_message_id,
                        user_id, family_key, text, photo_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(mod_chat_id, message_id) DO UPDATE SET
                        content_message_id = excluded.content_message_id,
                        user_id = excluded.user_id,
                        family_key = excluded.family_key,
                        text = excluded.text,
                        photo_id = excluded.photo_id,
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


# Global singleton: import `post_links` anywhere to access the SQLite store.
post_links = PostLinkDatabase()
