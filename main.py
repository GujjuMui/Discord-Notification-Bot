"""Entry point for the YouTube Notification Bot."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

import discord
from discord.ext import commands

import config
from cogs.youtube_tracker import YouTubeTracker, setup_commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=config.BOT_PREFIX,
    intents=intents,
    help_command=None,
)

tracker: Optional[YouTubeTracker] = None


async def setup_bot() -> None:
    global tracker

    if not config.validate_config():
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured.")

    tracker = YouTubeTracker(bot)
    await bot.add_cog(tracker)
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
        title="YouTube Notification Bot",
        description="Track YouTube channels and receive notifications for new uploads.",
        color=discord.Color.red(),
    )
    embed.add_field(
        name="YouTube",
        value=(
            "/addchannel <url>\n"
            "/removechannel <channel_id>\n"
            "/listchannels\n"
            "/settarget <channel_id> <channel>\n"
            "/pausechannel <channel_id>\n"
            "/resumechannel <channel_id>\n"
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
