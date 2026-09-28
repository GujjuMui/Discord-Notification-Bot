"""YouTube Notification Bot - Main Entry Point.
A Discord bot that monitors YouTube channels and sends notifications for new videos.
"""
import asyncio
import logging
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.youtube_tracker import YouTubeTracker, setup_commands

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Bot instance with necessary intents
intents = discord.Intents.default()
intents.message_content = True  # Required for reading message content
intents.presences = False
intents.members = False

# Create bot instance
bot = commands.Bot(
    command_prefix=config.BOT_PREFIX,
    intents=intents,
    help_command=None,
    case_insensitive=True
)

# Store tracker reference
tracker: YouTubeTracker = None


async def setup_bot() -> None:
    """Initialize and setup the bot."""
    global tracker

    # Validate configuration
    if not config.validate_config():
        logger.error("Configuration validation failed. Please check your .env file.")
        sys.exit(1)

    # Add YouTube tracker cog
    tracker = YouTubeTracker(bot)
    await bot.add_cog(tracker)

    # Setup slash commands
    await setup_commands(bot, tracker)

    logger.info("Bot setup complete")


@bot.event
async def on_ready() -> None:
    """Called when the bot is ready and connected."""
    logger.info(f"Bot logged in as: {bot.user}")
    logger.info(f"Bot ID: {bot.user.id}")
    logger.info(f"Guilds: {len(bot.guilds)}")

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

    # Set bot status
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="for new YouTube videos"
        )
    )


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    """Called when the bot joins a new guild."""
    logger.info(f"Joined guild: {guild.name} (ID: {guild.id})")


@bot.event
async def on_guild_remove(guild: discord.Guild) -> None:
    """Called when the bot leaves a guild."""
    logger.info(f"Left guild: {guild.name} (ID: {guild.id})")


@bot.event
async def on_command_error(
    ctx: commands.Context,
    error: commands.CommandError
) -> None:
    """Handle command errors."""
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Command on cooldown. Try again in {error.retry_after:.1f}s")
    else:
        logger.error(f"Command error: {error}")
        await ctx.send("❌ An error occurred while executing the command.")


# ==================== Basic Commands ====================

@bot.command(name="ping", description="Check if the bot is responding")
async def ping(ctx: commands.Context) -> None:
    """Simple ping command to check bot responsiveness."""
    await ctx.send(f"🏓 Pong! Latency: {round(bot.latency * 1000)}ms")


@bot.command(name="help", description="Show help information")
async def help_command(ctx: commands.Context) -> None:
    """Show help information about available commands."""
    embed = discord.Embed(
        title="YouTube Notification Bot - Help",
        description="Track YouTube channels and get notified about new videos!",
        color=discord.Color.blue()
    )

    # Slash commands section
    embed.add_field(
        name="📺 YouTube Commands",
        value="""`/addchannel <url>` - Add a YouTube channel to track
`/removechannel <channel_id>` - Stop tracking a channel
`/listchannels` - List all tracked channels
`/settarget <channel>` - Set notification channel
`/pausechannel <channel_id>` - Pause notifications
`/resumechannel <channel_id>` - Resume notifications
`/ytinfo <url>` - Get channel info""",
        inline=False
    )

    # Basic commands
    embed.add_field(
        name="🔧 Basic Commands",
        value="""`!ping` - Check bot latency
`!help` - Show this help message""",
        inline=False
    )

    embed.set_footer(text="Use these commands to manage your YouTube notifications!")
    await ctx.send(embed=embed)


# ==================== Bot Setup ====================

async def main() -> None:
    """Main entry point for the bot."""
    # Create cogs directory if it doesn't exist
    cogs_dir = Path(__file__).parent / "cogs"
    if not cogs_dir.exists():
        cogs_dir.mkdir(parents=True, exist_ok=True)

    # Initialize bot
    async with bot:
        await setup_bot()
        logger.info("Starting bot...")

        try:
            await bot.start(config.get_required_token())
        except KeyboardInterrupt:
            logger.info("Bot shutdown requested")
        except discord.LoginFailure:
            logger.error("Invalid bot token. Please check your .env file.")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)