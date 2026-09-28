"""Entry point for the Discord bot."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
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
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
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

BOT_VERSION = "2.0.0"
DEVELOPER_CREDIT = "GujjuMui"
START_TIME = datetime.now(timezone.utc)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.moderation = True
intents.voice_states = True
intents.invites = True
intents.emojis_and_stickers = True

bot = commands.Bot(
    command_prefix=config.BOT_PREFIX,
    intents=intents,
    help_command=None,
)

tracker: Optional[YouTubeTracker] = None
server_logger: Optional[ServerLogger] = None
bot_owner_id: Optional[int] = config.BOT_OWNER_ID


async def resolve_bot_owner_id() -> Optional[int]:
    global bot_owner_id
    if bot_owner_id is not None:
        return bot_owner_id
    try:
        application = await bot.application_info()
        owner = application.owner
        bot_owner_id = owner.id if owner else None
        if bot_owner_id:
            logger.info("Resolved Discord Application Owner as bot owner: %s", bot_owner_id)
    except (discord.HTTPException, discord.Forbidden):
        logger.exception("Could not resolve Discord Application Owner.")
    return bot_owner_id


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


async def _send_command_error(
    interaction: discord.Interaction,
    title: str,
    description: str,
) -> None:
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.red(),
    )
    embed.set_footer(text=f"Discord Notification Bot v{BOT_VERSION}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        logger.exception("Could not send application command error response.")


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    original = getattr(error, "original", error)

    if isinstance(error, (app_commands.CheckFailure, app_commands.MissingPermissions)):
        await _send_command_error(
            interaction,
            "🔒 Permission Required",
            "You need server-owner, bot-owner, or trusted-user access to run this command.",
        )
        return

    if isinstance(error, app_commands.CommandOnCooldown):
        await _send_command_error(
            interaction,
            "⏳ Slow Down",
            f"Please wait **{error.retry_after:.1f}s** before trying again.",
        )
        return

    if isinstance(original, discord.Forbidden):
        await _send_command_error(
            interaction,
            "🚫 Discord Permission Error",
            "The bot does not have the Discord permissions required for this action.",
        )
        return

    if isinstance(original, discord.HTTPException) and original.status == 429:
        await _send_command_error(
            interaction,
            "⏱️ Discord Rate Limited",
            "Discord temporarily rate-limited this request. Please try again shortly.",
        )
        return

    logger.exception(
        "Unhandled application command error for /%s: %s",
        getattr(interaction.command, "qualified_name", "unknown"),
        error,
    )
    await _send_command_error(
        interaction,
        "⚠️ Command Error",
        "Something went wrong while processing that command. The error was logged for diagnosis.",
    )


@bot.event
async def on_ready() -> None:
    await resolve_bot_owner_id()

    try:
        from database import db
        table_counts = db.table_counts()
        db_status = "SQLite Connected"
    except Exception:
        table_counts = {}
        db_status = "SQLite Error"
        logger.exception("Database health check failed during startup.")

    user_count = sum(
        (guild.member_count or len(guild.members))
        for guild in bot.guilds
    )
    discord_py_version = getattr(discord, "__version__", "unknown")
    start_text = START_TIME.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    banner = [
        "┌─────────────────────────────────────────────────────────────┐",
        f"│ 🚀 BOT NAME: {bot.user or 'Discord Notification Bot'} v{BOT_VERSION} (Production)",
        f"│ 👤 DEVELOPER / POWERED BY: {DEVELOPER_CREDIT} / Discord Notification Bot",
        f"│ 🤖 DISCORD.PY VERSION: {discord_py_version}",
        f"│ 📊 SERVERS CONNECTED: {len(bot.guilds)} | USERS: {user_count}",
        f"│ 💾 DATABASE: {db_status} | STATUS: Online & Ready",
        f"│ 🕒 START TIME: {start_text}",
        "└─────────────────────────────────────────────────────────────┘",
    ]
    print("\n" + "\n".join(banner))

    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")
    logger.info("Connected to %d guild(s).", len(bot.guilds))
    logger.info("Loaded Cogs: YouTubeTracker, ServerLogger")
    logger.info("Database tables synchronized: %s", table_counts)
    if server_logger:
        logger.info("Categorized server logging is loaded.")

    try:
        synced = await bot.tree.sync()
        logger.info(
            "Synced %d slash command(s): %s",
            len(synced),
            ", ".join(f"/{command.name}" for command in synced),
        )
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
        title="📖 Discord Notification Bot",
        description="Use **/help** for the interactive command guide.",
        color=discord.Color.red(),
    )
    embed.add_field(
        name="Quick Commands",
        value="/about\n/botstatus\n/ytinfo <url>\n!ping",
        inline=False,
    )
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
    except Exception as exc:
        if isinstance(exc, discord.errors.PrivilegedIntentsRequired):
            logger.error(
                "Discord rejected the connection because one or more privileged "
                "intents are disabled in the Developer Portal. Enable the required "
                "privileged intents for this application and restart the bot."
            )
            sys.exit(1)
        logger.exception("Fatal error.")
        sys.exit(1)
