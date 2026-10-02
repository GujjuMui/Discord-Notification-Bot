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
HTTP_USER_AGENT = "YouTube-Notification-Bot/1.0 (+https://github.com/GujjuMui/Discord-Notification-Bot)"
# Default 300 s (5 min) — keeps RSS polling well within free-tier rate limits.
# Set POLL_INTERVAL=60 in .env only for testing; production should use 180–300.
_poll_raw = os.getenv("POLL_INTERVAL", "300").strip()
try:
    POLL_INTERVAL: int = max(60, int(_poll_raw))
except ValueError:
    POLL_INTERVAL = 300
HTTP_TIMEOUT = 20
BOT_PREFIX = "!"

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
#                          Default 900 (15 min).  Only fires when a push was
#                          missed; not the primary notification path.
_fallback_raw = os.getenv("FALLBACK_POLL_INTERVAL", "900").strip()
try:
    FALLBACK_POLL_INTERVAL: int = max(300, int(_fallback_raw))
except ValueError:
    FALLBACK_POLL_INTERVAL = 900


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
