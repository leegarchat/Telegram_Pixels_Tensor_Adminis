import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


DATABASE_PATH = Path(__file__).resolve().parent.parent / "tgadmin.sqlite3"


class PostLinkDatabase:
    """Persistent links between moderation-topic messages and channel posts."""

    def __init__(self, path: Path = DATABASE_PATH):
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
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


post_links = PostLinkDatabase()
