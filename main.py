"""Entry point for the Discord bot."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
from typing import Optional

import discord
from discord.ext import commands

import config
from cogs.server_logger import ServerLogger
from cogs.youtube_tracker import YouTubeTracker, setup_commands

LOG_DIR = config.PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "bot.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)
root_logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)
root_logger.addHandler(_console_handler)

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.moderation = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=config.BOT_PREFIX,
    intents=intents,
    help_command=None,
)

tracker: Optional[YouTubeTracker] = None
server_logger: Optional[ServerLogger] = None


async def setup_bot() -> None:
    global tracker, server_logger

    if not config.validate_config():
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured.")

    tracker = YouTubeTracker(bot)
    await bot.add_cog(tracker)

    server_logger = ServerLogger(bot)
    await bot.add_cog(server_logger)

    setup_commands(bot, tracker)
    logger.info("Bot setup complete.")


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")
    logger.info("Connected to %d guild(s).", len(bot.guilds))

    try:
        synced = await bot.tree.sync()
        logger.info("Synced %d slash command(s).", len(synced))
    except discord.HTTPException:
        logger.exception("Failed to sync slash commands.")

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="for new YouTube videos",
        )
    )


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    logger.info("Joined guild: %s (%s)", guild.name, guild.id)


@bot.event
async def on_guild_remove(guild: discord.Guild) -> None:
    logger.info("Left guild: %s (%s)", guild.name, guild.id)


@bot.command(name="ping")
async def ping(ctx: commands.Context) -> None:
    await ctx.send(f"🏓 Pong! Latency: {round(bot.latency * 1000)}ms")


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    embed = discord.Embed(
        title="Discord Notification Bot",
        description="YouTube notifications plus server audit logging.",
        color=discord.Color.red(),
    )
    embed.add_field(
        name="YouTube",
        value=(
            "/setup_logs <#channel>\n"
            "/setup_yt <#channel>\n"
            "/add_yt <url>\n"
            "/remove_yt <url_or_id>\n"
            "/list_yt\n"
            "/ytinfo <url>"
        ),
        inline=False,
    )
    embed.add_field(name="Basic", value="!ping", inline=False)
    await ctx.send(embed=embed)


async def main() -> None:
    async with bot:
        await setup_bot()
        logger.info("Starting bot...")
        await bot.start(config.get_required_token())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except discord.LoginFailure:
        logger.error("Discord rejected the bot token.")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception:
        logger.exception("Fatal error.")
        sys.exit(1)
