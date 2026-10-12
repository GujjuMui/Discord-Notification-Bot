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
from cogs.utility import Utility
from cogs.moderation import Moderation
from cogs.channel_manager import ChannelManager
from utils.helpers import format_mentions

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

class _ConsoleNoiseFilter(logging.Filter):
    """Keep optional dependency warnings out of the terminal while retaining them in bot.log."""
    _ignored = {
        "PyNaCl is not installed, voice will NOT be supported",
        "davey is not installed, voice will NOT be supported",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() not in self._ignored


_console_handler = logging.StreamHandler()
_console_handler.addFilter(_ConsoleNoiseFilter())
_console_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)
root_logger.addHandler(_console_handler)

logger = logging.getLogger(__name__)

BOT_VERSION = "3.3.1"
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

allowed_mentions = discord.AllowedMentions(
    everyone=False,  # Never ping @everyone by default — elevate per call-site only
    roles=False,     # Same for roles — prevents accidental mass pings
    users=True,      # User mentions are safe by default (only pings specific users)
    replied_user=True,
)

bot = commands.Bot(
    command_prefix=config.BOT_PREFIX,
    intents=intents,
    help_command=None,
    allowed_mentions=allowed_mentions,
)

# Exposed to public/about and private health dashboards without importing main from cogs.
bot.bot_started_at = START_TIME
bot.allowed_mentions = allowed_mentions
bot.format_mentions = format_mentions
bot.bot_version = BOT_VERSION


# ---------------------------------------------------------------------------
# Global guild allowlist — blocks all slash commands in unauthorized guilds
# ---------------------------------------------------------------------------

@bot.tree.interaction_check
async def global_guild_check(interaction: discord.Interaction) -> bool:
    """Reject all slash command interactions from guilds not on the allowlist."""
    allowed = config.ALLOWED_GUILD_IDS
    if not allowed:
        # No allowlist configured — permit all guilds (default open mode)
        return True
    guild = interaction.guild
    if guild is None:
        # DM interactions — allow through (no guild context)
        return True
    if guild.id in allowed:
        return True
    # Unauthorized guild — raise CheckFailure so on_app_command_error handles it
    raise app_commands.CheckFailure(f"Guild {guild.id} is not authorized.")

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
    bot.server_logger = server_logger

    utility = Utility(bot)
    await bot.add_cog(utility)

    if config.ENABLE_MODERATION:
        moderation = Moderation(bot)
        await bot.add_cog(moderation)
        logger.info("Moderation cog loaded (ENABLE_MODERATION=true).")
    else:
        logger.info("Moderation cog disabled (set ENABLE_MODERATION=true to enable).")

    if config.ENABLE_CHANNEL_MANAGER:
        channel_manager = ChannelManager(bot)
        await bot.add_cog(channel_manager)
        logger.info("Channel manager cog loaded (ENABLE_CHANNEL_MANAGER=true).")
    else:
        logger.info("Channel manager cog disabled (set ENABLE_CHANNEL_MANAGER=true to enable).")

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
    except discord.NotFound:
        # Interaction token expired (>3s elapsed) — log at debug, not error.
        # This commonly happens when a check predicate does async I/O and the
        # token expires before the error handler can respond.
        logger.debug(
            "Interaction token expired before error response could be sent "
            "(command: %s, user: %s)",
            getattr(interaction.command, "qualified_name", "unknown"),
            interaction.user,
        )
    except discord.HTTPException:
        logger.exception("Could not send application command error response.")


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    original = getattr(error, "original", error)

    # Guild allowlist check — block all commands in unauthorized guilds
    if isinstance(error, app_commands.CheckFailure):
        guild = interaction.guild
        allowed = config.ALLOWED_GUILD_IDS
        if guild and allowed and guild.id not in allowed:
            try:
                await interaction.response.send_message(
                    "⛔ This bot is not authorized to operate in this server.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return
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
        f"│ 📡 WEBSUB: {'Active (' + config.WEBHOOK_URL[:40] + '...)' if config.WEBHOOK_URL else 'RSS fallback only (set WEBHOOK_URL to enable)'}",
        f"│ 🕒 START TIME: {start_text}",
        "└─────────────────────────────────────────────────────────────┘",
    ]
    print("\n" + "\n".join(banner))

    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")
    logger.info("Connected to %d guild(s).", len(bot.guilds))
    logger.info("Loaded Cogs: YouTubeTracker, ServerLogger, Utility, Moderation")
    logger.info("Database tables synchronized: %s", table_counts)
    if server_logger:
        logger.info("Categorized server logging is loaded.")

    if config.SYNC_COMMANDS:
        try:
            synced = await bot.tree.sync()
            logger.info(
                "Synced %d global slash command(s): %s",
                len(synced),
                ", ".join(f"/{command.name}" for command in synced),
            )
        except discord.HTTPException:
            logger.exception("Failed to sync slash commands.")
    else:
        logger.info(
            "Automatic slash command sync disabled. Use /sync when a command tree update is needed."
        )

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="for new YouTube activity",
        )
    )


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    logger.info("Joined guild: %s (%s)", guild.name, guild.id)

    # Notify the bot owner when the bot is added to an unknown/unauthorized guild.
    owner_id = bot_owner_id or await resolve_bot_owner_id()
    if owner_id is None:
        return

    # Check if this guild is authorized
    allowed = config.ALLOWED_GUILD_IDS
    is_authorized = (not allowed) or (guild.id in allowed)

    try:
        owner = await bot.fetch_user(owner_id)
        status = "✅ Authorized" if is_authorized else "⚠️ NOT in allowlist — commands blocked"
        await owner.send(
            f"📥 **Bot added to a new server**\n"
            f"**Name:** {guild.name}\n"
            f"**ID:** `{guild.id}`\n"
            f"**Members:** {guild.member_count}\n"
            f"**Owner:** {guild.owner} (`{guild.owner_id}`)\n"
            f"**Status:** {status}\n\n"
            f"Add `{guild.id}` to `ALLOWED_GUILD_IDS` in Railway to authorize it, "
            f"or use `/guildlist` to review all servers."
        )
    except (discord.Forbidden, discord.HTTPException):
        logger.warning("Could not DM owner about new guild join: %s (%s)", guild.name, guild.id)


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


@bot.command(name="guildlist")
async def guildlist(ctx: commands.Context) -> None:
    """Owner-only: list all guilds the bot is in with authorization status."""
    owner_id = bot_owner_id or await resolve_bot_owner_id()
    if ctx.author.id != owner_id:
        return  # Silently ignore non-owners

    allowed = config.ALLOWED_GUILD_IDS
    lines: list[str] = []

    for guild in sorted(bot.guilds, key=lambda g: g.name.lower()):
        authorized = (not allowed) or (guild.id in allowed)
        status = "✅" if authorized else "⚠️"

        # Try to get an invite link for the first available text channel
        invite_url = ""
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)
            if perms.create_instant_invite:
                try:
                    invite = await ch.create_invite(max_age=3600, max_uses=1, reason="guildlist owner command")
                    invite_url = invite.url
                    break
                except (discord.Forbidden, discord.HTTPException):
                    continue

        link_part = f" — {invite_url}" if invite_url else " — (no invite perm)"
        lines.append(
            f"{status} **{guild.name}** (`{guild.id}`) {guild.member_count} members{link_part}"
        )

    # Split into chunks to stay under Discord's 2000-char limit
    chunks: list[str] = []
    current = f"**Bot is in {len(bot.guilds)} guild(s):**\n"
    for line in lines:
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)

    for chunk in chunks:
        await ctx.author.send(chunk)

    try:
        await ctx.message.add_reaction("✅")
    except (discord.Forbidden, discord.HTTPException):
        pass


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
