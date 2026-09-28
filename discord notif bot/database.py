"""Database module for YouTube Notification Bot.
Manages SQLite database for tracking channels, videos, and notification settings.
"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from contextlib import contextmanager

import config


class Database:
    """SQLite database manager for YouTube Notification Bot."""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize database connection.

        Args:
            db_path: Path to SQLite database file. Defaults to config.DATABASE_PATH.
        """
        self.db_path = db_path or config.DATABASE_PATH
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _cursor(self):
        """Context manager for database cursor."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _init_database(self) -> None:
        """Initialize database tables if they don't exist."""
        with self._cursor() as cursor:
            # Table for tracked YouTube channels
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT UNIQUE NOT NULL,
                    channel_name TEXT NOT NULL,
                    channel_url TEXT NOT NULL,
                    discord_channel_id INTEGER,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Table for tracked videos (for deduplication)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT UNIQUE NOT NULL,
                    channel_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    thumbnail_url TEXT,
                    video_url TEXT NOT NULL,
                    published_at TEXT,
                    notified_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    is_short INTEGER DEFAULT 0,
                    is_live INTEGER DEFAULT 0,
                    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
                )
            """)

            # Table for notification settings
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create indexes for faster queries
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_videos_channel_id
                ON videos(channel_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_videos_video_id
                ON videos(video_id)
            """)

    # ==================== Channel Operations ====================

    def add_channel(
        self,
        channel_id: str,
        channel_name: str,
        channel_url: str,
        discord_channel_id: Optional[int] = None
    ) -> bool:
        """Add a new YouTube channel to track.

        Args:
            channel_id: YouTube channel ID
            channel_name: Display name of the channel
            channel_url: URL to the YouTube channel
            discord_channel_id: Discord channel ID for notifications

        Returns:
            True if added successfully, False if already exists
        """
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    """INSERT INTO channels
                       (channel_id, channel_name, channel_url, discord_channel_id)
                       VALUES (?, ?, ?, ?)""",
                    (channel_id, channel_name, channel_url, discord_channel_id)
                )
                return True
        except sqlite3.IntegrityError:
            # Channel already exists, update instead
            return self.update_channel(channel_id, channel_name, channel_url, discord_channel_id)

    def update_channel(
        self,
        channel_id: str,
        channel_name: str,
        channel_url: str,
        discord_channel_id: Optional[int] = None
    ) -> bool:
        """Update an existing channel's information."""
        with self._cursor() as cursor:
            cursor.execute(
                """UPDATE channels
                   SET channel_name = ?, channel_url = ?,
                       discord_channel_id = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE channel_id = ?""",
                (channel_name, channel_url, discord_channel_id, channel_id)
            )
            return cursor.rowcount > 0

    def remove_channel(self, channel_id: str) -> bool:
        """Remove a channel from tracking.

        Args:
            channel_id: YouTube channel ID to remove

        Returns:
            True if removed successfully
        """
        with self._cursor() as cursor:
            # Delete associated videos first
            cursor.execute("DELETE FROM videos WHERE channel_id = ?", (channel_id,))
            # Delete the channel
            cursor.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
            return cursor.rowcount > 0

    def get_all_channels(self) -> List[Dict[str, Any]]:
        """Get all tracked channels.

        Returns:
            List of channel dictionaries
        """
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT id, channel_id, channel_name, channel_url,
                          discord_channel_id, is_active, created_at, updated_at
                   FROM channels ORDER BY channel_name"""
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_active_channels(self) -> List[Dict[str, Any]]:
        """Get all active channels.

        Returns:
            List of active channel dictionaries
        """
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT id, channel_id, channel_name, channel_url,
                          discord_channel_id, is_active, created_at, updated_at
                   FROM channels WHERE is_active = 1 ORDER BY channel_name"""
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_channel_by_id(self, channel_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific channel by YouTube channel ID.

        Args:
            channel_id: YouTube channel ID

        Returns:
            Channel dictionary or None if not found
        """
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT id, channel_id, channel_name, channel_url,
                          discord_channel_id, is_active, created_at, updated_at
                   FROM channels WHERE channel_id = ?""",
                (channel_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def set_channel_active(self, channel_id: str, is_active: bool) -> bool:
        """Enable or disable channel tracking.

        Args:
            channel_id: YouTube channel ID
            is_active: Whether the channel should be active

        Returns:
            True if updated successfully
        """
        with self._cursor() as cursor:
            cursor.execute(
                """UPDATE channels
                   SET is_active = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE channel_id = ?""",
                (1 if is_active else 0, channel_id)
            )
            return cursor.rowcount > 0

    def set_discord_channel(self, channel_id: str, discord_channel_id: int) -> bool:
        """Set Discord channel for a YouTube channel's notifications.

        Args:
            channel_id: YouTube channel ID
            discord_channel_id: Discord channel ID

        Returns:
            True if updated successfully
        """
        with self._cursor() as cursor:
            cursor.execute(
                """UPDATE channels
                   SET discord_channel_id = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE channel_id = ?""",
                (discord_channel_id, channel_id)
            )
            return cursor.rowcount > 0

    # ==================== Video Operations ====================

    def add_video(
        self,
        video_id: str,
        channel_id: str,
        title: str,
        video_url: str,
        thumbnail_url: Optional[str] = None,
        published_at: Optional[str] = None,
        is_short: bool = False,
        is_live: bool = False
    ) -> bool:
        """Add a new video to track (for deduplication).

        Args:
            video_id: YouTube video ID
            channel_id: YouTube channel ID
            title: Video title
            video_url: URL to the video
            thumbnail_url: URL to video thumbnail
            published_at: ISO timestamp of publish date
            is_short: Whether this is a YouTube Short
            is_live: Whether this is a live stream

        Returns:
            True if added successfully, False if already exists
        """
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    """INSERT INTO videos
                       (video_id, channel_id, title, video_url, thumbnail_url,
                        published_at, is_short, is_live)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (video_id, channel_id, title, video_url, thumbnail_url,
                     published_at, 1 if is_short else 0, 1 if is_live else 0)
                )
                return True
        except sqlite3.IntegrityError:
            return False

    def video_exists(self, video_id: str) -> bool:
        """Check if a video has already been tracked.

        Args:
            video_id: YouTube video ID

        Returns:
            True if video exists in database
        """
        with self._cursor() as cursor:
            cursor.execute("SELECT 1 FROM videos WHERE video_id = ?", (video_id,))
            return cursor.fetchone() is not None

    def get_recent_videos(self, channel_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent videos for a channel.

        Args:
            channel_id: YouTube channel ID
            limit: Maximum number of videos to return

        Returns:
            List of video dictionaries
        """
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT video_id, channel_id, title, thumbnail_url,
                          video_url, published_at, is_short, is_live
                   FROM videos
                   WHERE channel_id = ?
                   ORDER BY notified_at DESC LIMIT ?""",
                (channel_id, limit)
            )
            return [dict(row) for row in cursor.fetchall()]

    # ==================== Settings Operations ====================

    def set_setting(self, key: str, value: str) -> None:
        """Set a configuration setting.

        Args:
            key: Setting key
            value: Setting value
        """
        with self._cursor() as cursor:
            cursor.execute(
                """INSERT OR REPLACE INTO settings (key, value, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)""",
                (key, value)
            )

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a configuration setting.

        Args:
            key: Setting key
            default: Default value if key not found

        Returns:
            Setting value or default
        """
        with self._cursor() as cursor:
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else default


# Global database instance
db = Database()