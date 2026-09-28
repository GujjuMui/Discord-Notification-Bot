"""Configuration loader for YouTube Notification Bot.
Loads environment variables from .env file using python-dotenv.
"""
import os
from pathlib import Path
from typing import Optional

# Load .env file from project root
from dotenv import load_dotenv

# Get the directory where this file is located
PROJECT_ROOT = Path(__file__).parent
ENV_FILE = PROJECT_ROOT / ".env"

# Load environment variables from .env file if it exists
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
else:
    # Try loading from current working directory as fallback
    load_dotenv()

# Discord Configuration
DISCORD_BOT_TOKEN: Optional[str] = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID: Optional[int] = (
    int(os.getenv("DISCORD_CHANNEL_ID"))
    if os.getenv("DISCORD_CHANNEL_ID")
    else None
)

# Bot Configuration
BOT_PREFIX: str = os.getenv("BOT_PREFIX", "!")
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "60"))

# YouTube RSS Feed URL Template
YOUTUBE_RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

# Database Configuration
DATABASE_PATH: Path = PROJECT_ROOT / "youtube_bot.db"


def get_required_token() -> str:
    """Get the Discord bot token, raising error if not set."""
    if not DISCORD_BOT_TOKEN:
        raise ValueError(
            "DISCORD_BOT_TOKEN not set. Please create a .env file with your bot token.\n"
            "Copy .env.example to .env and add your DISCORD_BOT_TOKEN."
        )
    return DISCORD_BOT_TOKEN


def get_channel_id() -> Optional[int]:
    """Get the default Discord channel ID for notifications."""
    return DISCORD_CHANNEL_ID


def validate_config() -> bool:
    """Validate that required configuration is present."""
    if not DISCORD_BOT_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN not configured")
        return False
    return True