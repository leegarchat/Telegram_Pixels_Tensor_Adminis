import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


DATABASE_PATH = Path(__file__).resolve().parent.parent / "tgadmin.sqlite3"


class PostLinkDatabase:
    """Persistent links between moderation-topic messages and channel posts."""

    def __init__(self, path: Path = DATABASE_PATH):
        self.path = path
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
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
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_post_links_target "
                    "ON post_links(channel_id, channel_message_id)"
                )
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
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM post_links WHERE source_chat_id = ? AND source_message_id = ?",
                (source_chat_id, source_message_id),
            ).fetchone()
        return dict(row) if row else None

    def find_by_channel_post(self, channel_id: int, channel_message_id: int) -> dict[str, Any] | None:
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


post_links = PostLinkDatabase()
