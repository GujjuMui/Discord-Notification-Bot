"""SQLite persistence for guild configuration, YouTube tracking, and audit cache."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import config


class Database:
    """SQLite data layer for persistent bot state."""

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
            # Legacy tables are retained so existing video history remains usable.
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

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trusted_users (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    added_by TEXT NOT NULL,
                    added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    audit_log_channel_id INTEGER,
                    yt_notification_channel_id INTEGER,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS yt_monitored_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    yt_channel_id TEXT NOT NULL,
                    yt_channel_name TEXT,
                    yt_channel_url TEXT NOT NULL,
                    discord_target_channel_id TEXT NOT NULL,
                    last_video_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, yt_channel_id, discord_target_channel_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS message_cache (
                    message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    author_tag TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    attachments TEXT NOT NULL DEFAULT '[]',
                    timestamp TEXT NOT NULL
                )
            """)

            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_videos_channel_id ON videos(channel_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_videos_video_id ON videos(video_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_yt_monitored_guild ON yt_monitored_channels(guild_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_yt_monitored_channel ON yt_monitored_channels(yt_channel_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_yt_monitored_route "
                "ON yt_monitored_channels(guild_id, yt_channel_id, discord_target_channel_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_cache_guild_channel ON message_cache(guild_id, channel_id)"
            )

            columns = {
                row["name"]
                for row in cursor.execute("PRAGMA table_info(videos)").fetchall()
            }
            if "notified_at" not in columns:
                cursor.execute("ALTER TABLE videos ADD COLUMN notified_at TEXT")

            # Migrate the old guild-wide YouTube routing table to the new
            # per-YouTube-channel/per-Discord-channel routing model.
            yt_columns = {
                row["name"]
                for row in cursor.execute(
                    "PRAGMA table_info(yt_monitored_channels)"
                ).fetchall()
            }
            yt_sql_row = cursor.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'yt_monitored_channels'"
            ).fetchone()
            yt_sql = (yt_sql_row["sql"] or "") if yt_sql_row else ""
            needs_yt_migration = (
                "discord_target_channel_id" not in yt_columns
                or "UNIQUE(guild_id, yt_channel_id)" in yt_sql
            )

            if needs_yt_migration:
                cursor.execute("ALTER TABLE yt_monitored_channels RENAME TO yt_monitored_channels_legacy")
                cursor.execute("""
                    CREATE TABLE yt_monitored_channels (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id TEXT NOT NULL,
                        yt_channel_id TEXT NOT NULL,
                        yt_channel_name TEXT,
                        yt_channel_url TEXT NOT NULL,
                        discord_target_channel_id TEXT NOT NULL,
                        last_video_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(guild_id, yt_channel_id, discord_target_channel_id)
                    )
                """)
                cursor.execute("""
                    INSERT OR IGNORE INTO yt_monitored_channels
                        (guild_id, yt_channel_id, yt_channel_name, yt_channel_url,
                         discord_target_channel_id, last_video_id, created_at)
                    SELECT
                        legacy.guild_id,
                        legacy.yt_channel_id,
                        legacy.yt_channel_name,
                        legacy.yt_channel_url,
                        COALESCE(CAST(gs.yt_notification_channel_id AS TEXT), '0'),
                        legacy.last_video_id,
                        legacy.created_at
                    FROM yt_monitored_channels_legacy AS legacy
                    LEFT JOIN guild_settings AS gs
                        ON gs.guild_id = legacy.guild_id
                """)
                cursor.execute("DROP TABLE yt_monitored_channels_legacy")

    # Trusted users / RBAC -----------------------------------------------

    def add_trusted_user(self, guild_id: int, user_id: int, added_by: int) -> bool:
        with self._cursor() as cursor:
            cursor.execute("""
                INSERT OR REPLACE INTO trusted_users
                    (guild_id, user_id, added_by, added_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """, (str(guild_id), str(user_id), str(added_by)))
            return cursor.rowcount > 0

    def remove_trusted_user(self, guild_id: int, user_id: int) -> bool:
        with self._cursor() as cursor:
            cursor.execute("""
                DELETE FROM trusted_users
                WHERE guild_id = ? AND user_id = ?
            """, (str(guild_id), str(user_id)))
            return cursor.rowcount > 0

    def is_trusted_user(self, guild_id: int, user_id: int) -> bool:
        with self._cursor() as cursor:
            row = cursor.execute("""
                SELECT 1 FROM trusted_users
                WHERE guild_id = ? AND user_id = ?
                LIMIT 1
            """, (str(guild_id), str(user_id))).fetchone()
            return row is not None

    def get_trusted_users(self, guild_id: int) -> List[Dict[str, Any]]:
        with self._cursor() as cursor:
            rows = cursor.execute("""
                SELECT guild_id, user_id, added_by, added_at
                FROM trusted_users
                WHERE guild_id = ?
                ORDER BY added_at ASC
            """, (str(guild_id),)).fetchall()
            return [dict(row) for row in rows]

    # Guild configuration -------------------------------------------------

    def set_audit_log_channel(self, guild_id: int, channel_id: int) -> None:
        with self._cursor() as cursor:
            cursor.execute("""
                INSERT INTO guild_settings (guild_id, audit_log_channel_id, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id) DO UPDATE SET
                    audit_log_channel_id = excluded.audit_log_channel_id,
                    updated_at = CURRENT_TIMESTAMP
            """, (guild_id, channel_id))

    def set_yt_notification_channel(self, guild_id: int, channel_id: int) -> None:
        with self._cursor() as cursor:
            cursor.execute("""
                INSERT INTO guild_settings (guild_id, yt_notification_channel_id, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id) DO UPDATE SET
                    yt_notification_channel_id = excluded.yt_notification_channel_id,
                    updated_at = CURRENT_TIMESTAMP
            """, (guild_id, channel_id))

    def get_guild_settings(self, guild_id: int) -> Optional[Dict[str, Any]]:
        with self._cursor() as cursor:
            row = cursor.execute("""
                SELECT guild_id, audit_log_channel_id,
                       yt_notification_channel_id, updated_at
                FROM guild_settings
                WHERE guild_id = ?
            """, (guild_id,)).fetchone()
            return dict(row) if row else None

    # Guild YouTube sources ----------------------------------------------

    def add_yt_monitored_channel(
        self,
        guild_id: int,
        yt_channel_id: str,
        yt_channel_name: str,
        yt_channel_url: str,
        discord_target_channel_id: int,
        last_video_id: Optional[str] = None,
    ) -> bool:
        with self._cursor() as cursor:
            cursor.execute("""
                INSERT INTO yt_monitored_channels
                    (guild_id, yt_channel_id, yt_channel_name, yt_channel_url,
                     discord_target_channel_id, last_video_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, yt_channel_id, discord_target_channel_id)
                DO UPDATE SET
                    yt_channel_name = excluded.yt_channel_name,
                    yt_channel_url = excluded.yt_channel_url,
                    last_video_id = COALESCE(
                        yt_monitored_channels.last_video_id,
                        excluded.last_video_id
                    )
            """, (
                str(guild_id),
                yt_channel_id,
                yt_channel_name,
                yt_channel_url,
                str(discord_target_channel_id),
                last_video_id,
            ))
            return cursor.rowcount > 0

    def remove_yt_monitored_channel(
        self,
        guild_id: int,
        yt_channel_id: str,
        discord_target_channel_id: Optional[int] = None,
    ) -> bool:
        with self._cursor() as cursor:
            if discord_target_channel_id is None:
                cursor.execute("""
                    DELETE FROM yt_monitored_channels
                    WHERE guild_id = ? AND yt_channel_id = ?
                """, (str(guild_id), yt_channel_id))
            else:
                cursor.execute("""
                    DELETE FROM yt_monitored_channels
                    WHERE guild_id = ?
                      AND yt_channel_id = ?
                      AND discord_target_channel_id = ?
                """, (
                    str(guild_id),
                    yt_channel_id,
                    str(discord_target_channel_id),
                ))
            return cursor.rowcount > 0

    def get_yt_monitored_channels(
        self, guild_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        with self._cursor() as cursor:
            if guild_id is None:
                rows = cursor.execute("""
                    SELECT id, guild_id, yt_channel_id, yt_channel_name,
                           yt_channel_url, discord_target_channel_id,
                           last_video_id, created_at
                    FROM yt_monitored_channels
                    ORDER BY guild_id, yt_channel_name, discord_target_channel_id
                """).fetchall()
            else:
                rows = cursor.execute("""
                    SELECT id, guild_id, yt_channel_id, yt_channel_name,
                           yt_channel_url, discord_target_channel_id,
                           last_video_id, created_at
                    FROM yt_monitored_channels
                    WHERE guild_id = ?
                    ORDER BY yt_channel_name, discord_target_channel_id
                """, (str(guild_id),)).fetchall()
            return [dict(row) for row in rows]

    def get_yt_monitored_channel(
        self,
        guild_id: int,
        yt_channel_id: str,
        discord_target_channel_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        with self._cursor() as cursor:
            if discord_target_channel_id is None:
                row = cursor.execute("""
                    SELECT id, guild_id, yt_channel_id, yt_channel_name,
                           yt_channel_url, discord_target_channel_id,
                           last_video_id, created_at
                    FROM yt_monitored_channels
                    WHERE guild_id = ? AND yt_channel_id = ?
                    ORDER BY id
                    LIMIT 1
                """, (str(guild_id), yt_channel_id)).fetchone()
            else:
                row = cursor.execute("""
                    SELECT id, guild_id, yt_channel_id, yt_channel_name,
                           yt_channel_url, discord_target_channel_id,
                           last_video_id, created_at
                    FROM yt_monitored_channels
                    WHERE guild_id = ?
                      AND yt_channel_id = ?
                      AND discord_target_channel_id = ?
                """, (
                    str(guild_id),
                    yt_channel_id,
                    str(discord_target_channel_id),
                )).fetchone()
            return dict(row) if row else None

    def update_yt_last_video(
        self,
        guild_id: int,
        yt_channel_id: str,
        discord_target_channel_id: int,
        video_id: str,
    ) -> bool:
        with self._cursor() as cursor:
            cursor.execute("""
                UPDATE yt_monitored_channels
                SET last_video_id = ?
                WHERE guild_id = ?
                  AND yt_channel_id = ?
                  AND discord_target_channel_id = ?
            """, (
                video_id,
                str(guild_id),
                yt_channel_id,
                str(discord_target_channel_id),
            ))
            return cursor.rowcount > 0

    # Persistent message cache -------------------------------------------

    def cache_message(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        author_tag: str,
        content: str,
        attachments: List[str],
        timestamp: str,
    ) -> None:
        payload = json.dumps(attachments, ensure_ascii=False)
        with self._cursor() as cursor:
            cursor.execute("""
                INSERT INTO message_cache
                    (message_id, guild_id, channel_id, author_tag, content, attachments, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    guild_id = excluded.guild_id,
                    channel_id = excluded.channel_id,
                    author_tag = excluded.author_tag,
                    content = excluded.content,
                    attachments = excluded.attachments,
                    timestamp = excluded.timestamp
            """, (
                message_id,
                guild_id,
                channel_id,
                author_tag,
                content or "",
                payload,
                timestamp,
            ))

    @staticmethod
    def _decode_message(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        try:
            data["attachments"] = json.loads(data["attachments"] or "[]")
        except json.JSONDecodeError:
            data["attachments"] = []
        return data

    def get_cached_message(self, message_id: int) -> Optional[Dict[str, Any]]:
        with self._cursor() as cursor:
            row = cursor.execute("""
                SELECT message_id, guild_id, channel_id, author_tag,
                       content, attachments, timestamp
                FROM message_cache
                WHERE message_id = ?
            """, (message_id,)).fetchone()
            return self._decode_message(row) if row else None

    def get_cached_messages(
        self, message_ids: List[int], guild_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        if not message_ids:
            return []

        placeholders = ",".join("?" for _ in message_ids)
        params: List[Any] = list(message_ids)
        guild_clause = ""
        if guild_id is not None:
            guild_clause = " AND guild_id = ?"
            params.append(guild_id)

        query = (
            "SELECT message_id, guild_id, channel_id, author_tag, "
            "content, attachments, timestamp FROM message_cache "
            f"WHERE message_id IN ({placeholders}){guild_clause} "
            "ORDER BY timestamp"
        )

        with self._cursor() as cursor:
            rows = cursor.execute(query, params).fetchall()
            return [self._decode_message(row) for row in rows]

    def delete_cached_message(self, message_id: int) -> Optional[Dict[str, Any]]:
        data = self.get_cached_message(message_id)
        if data:
            with self._cursor() as cursor:
                cursor.execute(
                    "DELETE FROM message_cache WHERE message_id = ?",
                    (message_id,),
                )
        return data

    # Legacy methods retained so existing video history remains compatible.

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
        with self._cursor() as cursor:
            # videos.channel_id has a foreign key to channels.channel_id.
            # YouTube monitoring uses the newer guild-scoped table, so make
            # sure the legacy parent row exists before storing video history.
            cursor.execute("""
                INSERT INTO channels
                    (channel_id, channel_name, channel_url, is_active)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(channel_id) DO NOTHING
            """, (
                channel_id,
                channel_id,
                f"https://www.youtube.com/channel/{channel_id}",
            ))

            cursor.execute("""
                INSERT OR IGNORE INTO videos
                    (video_id, channel_id, title, video_url, thumbnail_url,
                     published_at, notified_at, is_short, is_live)
                VALUES (?, ?, ?, ?, ?, ?, CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP ELSE NULL END, ?, ?)
            """, (
                video_id,
                channel_id,
                title,
                video_url,
                thumbnail_url,
                published_at,
                1 if notified else 0,
                1 if is_short else 0,
                1 if is_live else 0,
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

    def mark_video_notified(self, video_id: str) -> bool:
        with self._cursor() as cursor:
            cursor.execute(
                "UPDATE videos SET notified_at = CURRENT_TIMESTAMP WHERE video_id = ?",
                (video_id,),
            )
            return cursor.rowcount > 0

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
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
            return row["value"] if row else default


db = Database()
