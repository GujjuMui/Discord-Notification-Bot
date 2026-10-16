"""Environment configuration for the Discord bot.

Only secrets and deployment-specific storage paths belong here. Guild/channel
configuration and YouTube sources are stored in SQLite and managed via slash
commands.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(ENV_FILE)

DISCORD_BOT_TOKEN: Optional[str] = os.getenv("DISCORD_BOT_TOKEN")

_owner_id = os.getenv("BOT_OWNER_ID", "").strip()

try:
    BOT_OWNER_ID: Optional[int] = int(_owner_id) if _owner_id else None
except ValueError as exc:
    raise ValueError("BOT_OWNER_ID must be a Discord user ID.") from exc

_db_value = Path(os.getenv("DATABASE_PATH", "youtube_bot.db"))
DATABASE_PATH = _db_value if _db_value.is_absolute() else PROJECT_ROOT / _db_value

SYNC_COMMANDS = os.getenv("SYNC_COMMANDS", "false").strip().lower() in {"true", "1"}
HTTP_USER_AGENT = os.getenv(
    "HTTP_USER_AGENT",
    "YouTube-Notification-Bot/1.0 (+https://github.com/GujjuMui/Discord-Notification-Bot)",
)
# Default 300 s (5 min) — keeps RSS polling well within free-tier rate limits.
# Set POLL_INTERVAL=60 in .env only for testing; production should use 180–300.
_poll_raw = os.getenv("POLL_INTERVAL", "300").strip()
try:
    POLL_INTERVAL: int = max(60, int(_poll_raw))
except ValueError:
    POLL_INTERVAL = 300
HTTP_TIMEOUT = 20
BOT_PREFIX = "!"

# Bot display name — used in permission request DMs and role creation.
BOT_NAME: str = os.getenv("BOT_NAME", "P U M M E L O")

# Bot version — single source of truth.
BOT_VERSION: str = os.getenv("BOT_VERSION", "3.3.1")

# ---------------------------------------------------------------------------
# WebSub (PubSubHubbub) push engine
# ---------------------------------------------------------------------------
# WEBHOOK_URL  — the publicly-reachable URL Google will POST push notifications
#                to.  Must end with /youtube/webhook.
#                e.g. https://your-railway-app.up.railway.app/youtube/webhook
#                Leave empty (default) to run in RSS-fallback-only mode.
WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "").strip()

# WEBHOOK_PORT — the TCP port the embedded aiohttp webhook server listens on.
#                Railway maps this automatically when you expose a port.
#                Default 8080 — matches Railway's default HTTP port.
_webhook_port_raw = os.getenv("WEBHOOK_PORT", "8080").strip()
try:
    WEBHOOK_PORT: int = int(_webhook_port_raw)
except ValueError:
    WEBHOOK_PORT = 8080

# FALLBACK_POLL_INTERVAL — seconds between RSS fallback polls.
#                          Default 60s — runs every minute as an active safety
#                          net alongside WebSub push. Catches any pushes Google
#                          delays or fails t    o deliver.
#                          Min: 60 (enforced in cog_load).
_fallback_raw = os.getenv("FALLBACK_POLL_INTERVAL", "60").strip()
try:
    FALLBACK_POLL_INTERVAL: int = max(60, int(_fallback_raw))
except ValueError:
    FALLBACK_POLL_INTERVAL = 60

# WEBSUB_HUB_URL — Google's PubSubHubbub hub endpoint.
#                  Only change this if Google migrates to a new hub URL.
WEBSUB_HUB_URL: str = os.getenv("WEBSUB_HUB_URL", "https://pubsubhubbub.appspot.com/subscribe")

# WEBSUB_LEASE_SECONDS — requested WebSub lease duration in seconds.
#                        Default 604800 (7 days). Bot re-subscribes at T-24h.
_lease_raw = os.getenv("WEBSUB_LEASE_SECONDS", "604800").strip()
try:
    WEBSUB_LEASE_SECONDS: int = max(3600, int(_lease_raw))
except ValueError:
    WEBSUB_LEASE_SECONDS = 604800

# WEBSUB_MAX_BODY_BYTES — maximum push payload size accepted from Google.
#                         Default 524288 (512 KB). Legitimate payloads are <10 KB.
_body_raw = os.getenv("WEBSUB_MAX_BODY_BYTES", "524288").strip()
try:
    WEBSUB_MAX_BODY_BYTES: int = max(1024, int(_body_raw))
except ValueError:
    WEBSUB_MAX_BODY_BYTES = 524288


def get_required_token() -> str:
    token = (DISCORD_BOT_TOKEN or "").strip()
    if not token:
        raise ValueError(
            "DISCORD_BOT_TOKEN is missing. Copy .env.example to .env and add your bot token."
        )
    return token


def validate_config() -> bool:
    """Validate configuration without logging or exposing secrets."""
    return bool((DISCORD_BOT_TOKEN or "").strip())


# ---------------------------------------------------------------------------
# Feature flags — opt-in cog loading
# ---------------------------------------------------------------------------
# ENABLE_MODERATION — set true to load the moderation cog (ban/kick/timeout/
#                     purge/broadcast and all related slash + prefix commands).
#                     Default: false — cog is not loaded, commands never register.
ENABLE_MODERATION: bool = os.getenv("ENABLE_MODERATION", "false").strip().lower() in {"true", "1"}

# ENABLE_CHANNEL_MANAGER — set true to load the channel manager cog
#                          (/channelmanager, /leaveguild).
#                          Default: false.
ENABLE_CHANNEL_MANAGER: bool = os.getenv("ENABLE_CHANNEL_MANAGER", "false").strip().lower() in {"true", "1"}


# ---------------------------------------------------------------------------
# Guild allowlist
# ---------------------------------------------------------------------------
# ALLOWED_GUILD_IDS — comma-separated list of guild IDs that are permitted to
#                     use bot commands.  Any guild not on this list will have
#                     all slash commands and prefix commands blocked.
#
#                     If left empty, all guilds are permitted (open mode).
#
#                     Example: ALLOWED_GUILD_IDS=123456789,987654321
_allowed_raw = os.getenv("ALLOWED_GUILD_IDS", "").strip()
ALLOWED_GUILD_IDS: frozenset[int] = frozenset(
    int(gid.strip())
    for gid in _allowed_raw.split(",")
    if gid.strip().isdigit()
)


# ---------------------------------------------------------------------------
# Server logging
# ---------------------------------------------------------------------------
# LOG_CATEGORY_NAME — name of the Discord category that houses audit log channels.
#                     Default: '📁 SERVER LOGS'
LOG_CATEGORY_NAME: str = os.getenv("LOG_CATEGORY_NAME", "📁 SERVER LOGS")

# LOG_CHANNEL_NAMES — mapping of log type to Discord channel name.
#                     Override individual names via LOG_CHANNEL_<TYPE> env vars.
LOG_CHANNEL_NAMES: dict[str, str] = {
    "chat":    os.getenv("LOG_CHANNEL_CHAT",    "chat-logs"),
    "member":  os.getenv("LOG_CHANNEL_MEMBER",  "member-logs"),
    "profile": os.getenv("LOG_CHANNEL_PROFILE", "profile-logs"),
    "role":    os.getenv("LOG_CHANNEL_ROLE",    "role-logs"),
    "channel": os.getenv("LOG_CHANNEL_CHANNEL", "channel-logs"),
    "server":  os.getenv("LOG_CHANNEL_SERVER",  "server-logs"),
    "voice":   os.getenv("LOG_CHANNEL_VOICE",   "voice-logs"),
    "mod":     os.getenv("LOG_CHANNEL_MOD",     "moderation-logs"),
}

# MESSAGE_RETENTION_DAYS — how many days of messages to keep in the cache.
#                          Default: 30 days.
_retention_raw = os.getenv("MESSAGE_RETENTION_DAYS", "30").strip()
try:
    MESSAGE_RETENTION_DAYS: int = max(1, int(_retention_raw))
except ValueError:
    MESSAGE_RETENTION_DAYS = 30

# LOG_MAX_BYTES — max size of each rotating log file before rotation.
#                 Default: 10485760 (10 MB).
_log_max_raw = os.getenv("LOG_MAX_BYTES", "10485760").strip()
try:
    LOG_MAX_BYTES: int = max(1024 * 1024, int(_log_max_raw))
except ValueError:
    LOG_MAX_BYTES = 10 * 1024 * 1024

# LOG_BACKUP_COUNT — number of rotated log file backups to keep.
#                    Default: 5.
_log_backup_raw = os.getenv("LOG_BACKUP_COUNT", "5").strip()
try:
    LOG_BACKUP_COUNT: int = max(1, int(_log_backup_raw))
except ValueError:
    LOG_BACKUP_COUNT = 5
