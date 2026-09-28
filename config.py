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

_db_value = Path(os.getenv("DATABASE_PATH", "youtube_bot.db"))
DATABASE_PATH = _db_value if _db_value.is_absolute() else PROJECT_ROOT / _db_value

HTTP_USER_AGENT = os.getenv(
    "HTTP_USER_AGENT",
    "YouTube-Notification-Bot/1.0 (+https://github.com/GujjuMui/Discord-Notification-Bot)",
)

try:
    POLL_INTERVAL = max(30, int(os.getenv("POLL_INTERVAL", "60")))
except ValueError as exc:
    raise ValueError("POLL_INTERVAL must be a whole number of seconds.") from exc

try:
    HTTP_TIMEOUT = max(5, int(os.getenv("HTTP_TIMEOUT", "20")))
except ValueError as exc:
    raise ValueError("HTTP_TIMEOUT must be a whole number of seconds.") from exc

BOT_PREFIX = os.getenv("BOT_PREFIX", "!")


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
