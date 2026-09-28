"""SQLite persistence for the YouTube Notification Bot."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import config


class Database:
    """Small SQLite data layer with persistent video deduplication."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or config.DATABASE_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def _cursor(self):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_database(self) -> None:
        with self._cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT UNIQUE NOT NULL,
                    channel_name TEXT NOT NULL,
                    channel_url TEXT NOT NULL,
                    discord_channel_id INTEGER,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT UNIQUE NOT NULL,
                    channel_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    thumbnail_url TEXT,
                    video_url TEXT NOT NULL,
                    published_at TEXT,
                    notified_at TEXT,
                    is_short INTEGER NOT NULL DEFAULT 0,
                    is_live INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_videos_channel_id ON videos(channel_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_videos_video_id ON videos(video_id)"
            )

            columns = {
                row["name"]
                for row in cursor.execute("PRAGMA table_info(videos)").fetchall()
            }
            if "notified_at" not in columns:
                cursor.execute("ALTER TABLE videos ADD COLUMN notified_at TEXT")

    def add_channel(
        self,
        channel_id: str,
        channel_name: str,
        channel_url: str,
        discord_channel_id: Optional[int] = None,
    ) -> bool:
        with self._cursor() as cursor:
            cursor.execute("""
                INSERT INTO channels
                    (channel_id, channel_name, channel_url, discord_channel_id, is_active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(channel_id) DO UPDATE SET
                    channel_name = excluded.channel_name,
                    channel_url = excluded.channel_url,
                    discord_channel_id = COALESCE(
                        excluded.discord_channel_id,
                        channels.discord_channel_id
                    ),
                    is_active = 1,
                    updated_at = CURRENT_TIMESTAMP
            """, (channel_id, channel_name, channel_url, discord_channel_id))
            return cursor.rowcount > 0

    def update_channel(
        self,
        channel_id: str,
        channel_name: str,
        channel_url: str,
        discord_channel_id: Optional[int] = None,
    ) -> bool:
        with self._cursor() as cursor:
            cursor.execute("""
                UPDATE channels
                SET channel_name = ?,
                    channel_url = ?,
                    discord_channel_id = COALESCE(?, discord_channel_id),
                    is_active = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ?
            """, (channel_name, channel_url, discord_channel_id, channel_id))
            return cursor.rowcount > 0

    def remove_channel(self, channel_id: str) -> bool:
        """Pause instead of deleting history, preserving deduplication."""
        return self.set_channel_active(channel_id, False)

    def get_all_channels(self) -> List[Dict[str, Any]]:
        with self._cursor() as cursor:
            rows = cursor.execute("""
                SELECT id, channel_id, channel_name, channel_url,
                       discord_channel_id, is_active, created_at, updated_at
                FROM channels
                ORDER BY channel_name
            """).fetchall()
            return [dict(row) for row in rows]

    def get_active_channels(self) -> List[Dict[str, Any]]:
        with self._cursor() as cursor:
            rows = cursor.execute("""
                SELECT id, channel_id, channel_name, channel_url,
                       discord_channel_id, is_active, created_at, updated_at
                FROM channels
                WHERE is_active = 1
                ORDER BY channel_name
            """).fetchall()
            return [dict(row) for row in rows]

    def get_channel_by_id(self, channel_id: str) -> Optional[Dict[str, Any]]:
        with self._cursor() as cursor:
            row = cursor.execute("""
                SELECT id, channel_id, channel_name, channel_url,
                       discord_channel_id, is_active, created_at, updated_at
                FROM channels
                WHERE channel_id = ?
            """, (channel_id,)).fetchone()
            return dict(row) if row else None

    def set_channel_active(self, channel_id: str, is_active: bool) -> bool:
        with self._cursor() as cursor:
            cursor.execute("""
                UPDATE channels
                SET is_active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ?
            """, (1 if is_active else 0, channel_id))
            return cursor.rowcount > 0

    def set_discord_channel(self, channel_id: str, discord_channel_id: int) -> bool:
        with self._cursor() as cursor:
            cursor.execute("""
                UPDATE channels
                SET discord_channel_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ?
            """, (discord_channel_id, channel_id))
            return cursor.rowcount > 0

    def add_video(
        self,
        video_id: str,
        channel_id: str,
        title: str,
        video_url: str,
        thumbnail_url: Optional[str] = None,
        published_at: Optional[str] = None,
        is_short: bool = False,
        is_live: bool = False,
        notified: bool = False,
    ) -> bool:
        """Insert a video once. Returns False if it already exists."""
        with self._cursor() as cursor:
            cursor.execute("""
                INSERT OR IGNORE INTO videos
                    (video_id, channel_id, title, video_url, thumbnail_url,
                     published_at, notified_at, is_short, is_live)
                VALUES (?, ?, ?, ?, ?, ?, CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP ELSE NULL END, ?, ?)
            """, (
                video_id, channel_id, title, video_url, thumbnail_url, published_at,
                1 if notified else 0, 1 if is_short else 0, 1 if is_live else 0,
            ))
            return cursor.rowcount > 0

    def get_video(self, video_id: str) -> Optional[Dict[str, Any]]:
        with self._cursor() as cursor:
            row = cursor.execute("""
                SELECT video_id, channel_id, title, thumbnail_url,
                       video_url, published_at, notified_at, is_short, is_live
                FROM videos
                WHERE video_id = ?
            """, (video_id,)).fetchone()
            return dict(row) if row else None

    def video_exists(self, video_id: str) -> bool:
        return self.get_video(video_id) is not None

    def has_videos(self, channel_id: str) -> bool:
        with self._cursor() as cursor:
            row = cursor.execute(
                "SELECT 1 FROM videos WHERE channel_id = ? LIMIT 1",
                (channel_id,),
            ).fetchone()
            return row is not None

    def mark_video_notified(self, video_id: str) -> bool:
        with self._cursor() as cursor:
            cursor.execute("""
                UPDATE videos
                SET notified_at = CURRENT_TIMESTAMP
                WHERE video_id = ?
            """, (video_id,))
            return cursor.rowcount > 0

    def get_recent_videos(
        self, channel_id: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._cursor() as cursor:
            rows = cursor.execute("""
                SELECT video_id, channel_id, title, thumbnail_url,
                       video_url, published_at, notified_at, is_short, is_live
                FROM videos
                WHERE channel_id = ?
                ORDER BY COALESCE(published_at, notified_at) DESC
                LIMIT ?
            """, (channel_id, limit)).fetchall()
            return [dict(row) for row in rows]

    def set_setting(self, key: str, value: str) -> None:
        with self._cursor() as cursor:
            cursor.execute("""
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
            """, (key, value))

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._cursor() as cursor:
            row = cursor.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default


db = Database()
