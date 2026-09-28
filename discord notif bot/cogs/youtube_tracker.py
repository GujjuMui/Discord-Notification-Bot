"""YouTube Tracker Cog for Discord Bot.
Handles background polling of YouTube RSS feeds and sends notifications for new videos.
"""
import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp
import discord
import feedparser
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import db

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class YouTubeTracker(commands.Cog):
    """Cog for tracking YouTube channels and notifying about new videos."""

    def __init__(self, bot: commands.Bot):
        """Initialize the YouTube tracker cog.

        Args:
            bot: The Discord bot instance
        """
        self.bot = bot
        self.poll_interval = config.POLL_INTERVAL
        self._session: Optional[aiohttp.ClientSession] = None

        # Start the background task
        self.check_youtube.start()
        logger.info("YouTube Tracker cog initialized")

    async def cog_load(self) -> None:
        """Called when the cog is loaded."""
        self._session = aiohttp.ClientSession()
        logger.info("YouTube Tracker: HTTP session created")

    async def cog_unload(self) -> None:
        """Called when the cog is unloaded."""
        if self._session:
            await self._session.close()
        self.check_youtube.cancel()
        logger.info("YouTube Tracker: HTTP session closed, task cancelled")

    @property
    def session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ==================== YouTube RSS Functions ====================

    async def fetch_rss_feed(self, channel_id: str) -> Optional[Dict[str, Any]]:
        """Fetch and parse YouTube RSS feed for a channel.

        Args:
            channel_id: YouTube channel ID

        Returns:
            Parsed feed data or None on error
        """
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    logger.warning(f"RSS feed returned status {response.status} for {channel_id}")
                    return None

                content = await response.text()
                feed = feedparser.parse(content)

                if feed.bozo:
                    logger.warning(f"RSS feed parse error for {channel_id}: {feed.bozo_exception}")
                    return None

                return feed

        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching RSS for {channel_id}")
        except aiohttp.ClientError as e:
            logger.error(f"HTTP error fetching RSS for {channel_id}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error fetching RSS for {channel_id}: {e}")

        return None

    def extract_video_info(self, entry: Any) -> Dict[str, Any]:
        """Extract video information from RSS entry.

        Args:
            entry: Feed entry object

        Returns:
            Dictionary with video information
        """
        # Extract video ID from YouTube URL
        video_url = entry.get("link", "")
        video_id_match = re.search(r"v=([a-zA-Z0-9_-]{11})", video_url)
        video_id = video_id_match.group(1) if video_id_match else ""

        # Extract thumbnail
        thumbnail_url = ""
        if "media_thumbnail" in entry:
            thumbnail_url = entry.media_thumbnail[0].get("url", "")
        elif "media_content" in entry:
            for media in entry.media_content:
                if "medium" in media and media.get("medium") == "video":
                    thumbnail_url = media.get("url", "").replace("/blob/", "/")
                    break

        # Determine if it's a short or live stream
        is_short = "/shorts/" in video_url.lower() or (
            "yt_duration" in entry and int(entry.get("yt_duration", 0)) <= 60
        )
        is_live = entry.get("yt_live", "") == "live"

        # Get published date
        published_at = entry.get("published", "")

        return {
            "video_id": video_id,
            "title": entry.get("title", "Untitled"),
            "video_url": video_url,
            "thumbnail_url": thumbnail_url,
            "published_at": published_at,
            "is_short": is_short,
            "is_live": is_live,
            "author": entry.get("author", "Unknown")
        }

    async def check_channel_for_new_videos(
        self,
        channel: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Check a channel for new videos.

        Args:
            channel: Channel dictionary from database

        Returns:
            List of new video dictionaries
        """
        channel_id = channel["channel_id"]
        feed = await self.fetch_rss_feed(channel_id)

        if not feed:
            return []

        new_videos = []

        # Process each entry in the feed (newest first)
        for entry in feed.entries[:5]:  # Check latest 5 videos
            video_info = self.extract_video_info(entry)

            # Check if we already notified about this video
            if not db.video_exists(video_info["video_id"]):
                # Add to database and include in new videos
                db.add_video(
                    video_id=video_info["video_id"],
                    channel_id=channel_id,
                    title=video_info["title"],
                    video_url=video_info["video_url"],
                    thumbnail_url=video_info["thumbnail_url"],
                    published_at=video_info["published_at"],
                    is_short=video_info["is_short"],
                    is_live=video_info["is_live"]
                )
                new_videos.append(video_info)
                logger.info(f"New video detected: {video_info['title']} ({video_info['video_id']})")

        return new_videos

    async def send_notification(
        self,
        channel: discord.TextChannel,
        video_info: Dict[str, Any],
        youtube_channel_name: str
    ) -> None:
        """Send Discord notification for a new video.

        Args:
            channel: Discord text channel to send to
            video_info: Video information dictionary
            youtube_channel_name: Name of the YouTube channel
        """
        # Determine content type badge
        if video_info["is_live"]:
            badge = "🔴 LIVE"
            color = discord.Color.red()
        elif video_info["is_short"]:
            badge = "⚡ SHORT"
            color = discord.Color.orange()
        else:
            badge = "🎬 NEW VIDEO"
            color = discord.Color.blue()

        # Create rich embed
        embed = discord.Embed(
            title=video_info["title"],
            url=video_info["video_url"],
            color=color,
            timestamp=datetime.now()
        )

        # Set author to YouTube channel name
        embed.set_author(
            name=youtube_channel_name,
            icon_url="https://www.youtube.com/favicon.ico"
        )

        # Add video type badge as field
        embed.add_field(name="Type", value=badge, inline=True)

        # Add timestamp if available
        if video_info["published_at"]:
            try:
                pub_date = datetime.fromisoformat(
                    video_info["published_at"].replace("Z", "+00:00")
                )
                embed.add_field(
                    name="Published",
                    value=f"<t:{int(pub_date.timestamp())}:R>",
                    inline=True
                )
            except (ValueError, AttributeError):
                pass

        # Add thumbnail if available
        if video_info["thumbnail_url"]:
            embed.set_thumbnail(url=video_info["thumbnail_url"])

        # Add footer
        embed.set_footer(
            text="YouTube Notification Bot",
            icon_url="https://www.youtube.com/favicon.ico"
        )

        try:
            await channel.send(embed=embed)
            logger.info(f"Notification sent for: {video_info['title']}")
        except discord.Forbidden:
            logger.error(f"Permission denied to send message in channel {channel.id}")
        except discord.HTTPException as e:
            logger.error(f"Failed to send notification: {e}")

    async def process_all_channels(self) -> None:
        """Process all active channels for new videos."""
        channels = db.get_active_channels()

        if not channels:
            logger.debug("No active channels to check")
            return

        logger.info(f"Checking {len(channels)} channels for new videos")

        for channel in channels:
            try:
                new_videos = await self.check_channel_for_new_videos(channel)

                if new_videos:
                    # Get Discord channel to send notifications
                    discord_channel_id = channel.get("discord_channel_id") or config.get_channel_id()

                    if not discord_channel_id:
                        logger.warning(f"No Discord channel set for {channel['channel_name']}")
                        continue

                    # Get Discord channel object
                    discord_channel = self.bot.get_channel(discord_channel_id)

                    if not discord_channel or not isinstance(discord_channel, discord.TextChannel):
                        logger.warning(f"Discord channel {discord_channel_id} not found or not text channel")
                        continue

                    # Send notification for each new video
                    for video in new_videos:
                        await self.send_notification(
                            discord_channel,
                            video,
                            channel["channel_name"]
                        )

                        # Small delay to avoid rate limiting
                        await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"Error processing channel {channel['channel_name']}: {e}")

    # ==================== Background Task ====================

    @tasks.loop(seconds=60)
    async def check_youtube(self) -> None:
        """Background task to check YouTube feeds periodically."""
        try:
            await self.process_all_channels()
        except Exception as e:
            logger.error(f"Error in YouTube check loop: {e}")

    @check_youtube.before_loop
    async def before_check_youtube(self) -> None:
        """Wait for bot to be ready before starting the loop."""
        await self.bot.wait_until_ready()
        logger.info("Bot ready, starting YouTube check loop")

    # ==================== Utility Functions ====================

    @staticmethod
    def extract_channel_id(url: str) -> Optional[str]:
        """Extract YouTube channel ID from various URL formats.

        Args:
            url: YouTube channel URL

        Returns:
            Channel ID or None if not found
        """
        # Handle @handle URLs
        handle_match = re.search(r"youtube\.com/@([^/?]+)", url)
        if handle_match:
            return f"@{handle_match.group(1)}"

        # Handle /channel/UC... URLs
        channel_match = re.search(r"youtube\.com/channel/([a-zA-Z0-9_-]{22})", url)
        if channel_match:
            return channel_match.group(1)

        # Handle /c/... URLs
        custom_match = re.search(r"youtube\.com/c/([^/?]+)", url)
        if custom_match:
            return f"c/{custom_match.group(1)}"

        # Handle /user/... URLs
        user_match = re.search(r"youtube\.com/user/([^/?]+)", url)
        if user_match:
            return f"u/{user_match.group(1)}"

        # Handle /@handle format directly
        at_handle_match = re.search(r"@([^/?]+)", url)
        if at_handle_match:
            return f"@{at_handle_match.group(1)}"

        # Handle /playlist?list=... (extract channel from videos)
        playlist_match = re.search(r"youtube\.com/playlist\?list=([a-zA-Z0-9_-]+)", url)
        if playlist_match:
            return f"playlist:{playlist_match.group(1)}"

        return None

    async def resolve_channel_id(self, url_or_handle: str) -> Optional[str]:
        """Resolve a YouTube URL or handle to a channel ID.

        Args:
            url_or_handle: YouTube URL or @handle

        Returns:
            YouTube channel ID or None if resolution fails
        """
        # If it looks like a direct channel ID, return it
        if re.match(r"^UC[a-zA-Z0-9_-]{22}$", url_or_handle):
            return url_or_handle

        # Extract potential channel identifier from URL
        channel_id = self.extract_channel_id(url_or_handle)

        if not channel_id:
            return None

        # If it's a full URL, we need to fetch the actual channel ID
        if channel_id.startswith("@") or channel_id.startswith("c/") or channel_id.startswith("u/"):
            # Try to resolve using YouTube RSS feed
            # For handles, we can try the RSS URL directly
            if channel_id.startswith("@"):
                # Try to find the channel through web scraping or API
                # For now, return as-is and let the user provide the actual ID
                logger.info(f"Handle detected: {channel_id}. Note: You may need to provide the actual channel ID.")
                return None

        return channel_id

    async def get_channel_info_from_feed(self, channel_id: str) -> Optional[Dict[str, str]]:
        """Get channel name from RSS feed.

        Args:
            channel_id: YouTube channel ID

        Returns:
            Dictionary with channel name and URL, or None
        """
        feed = await self.fetch_rss_feed(channel_id)

        if not feed or not feed.feed:
            return None

        return {
            "channel_name": feed.feed.get("title", "Unknown Channel"),
            "channel_url": f"https://www.youtube.com/channel/{channel_id}"
        }


# ==================== Discord Slash Commands ====================

def setup(bot: commands.Bot) -> None:
    """Setup function to add cog to bot."""
    bot.add_cog(YouTubeTracker(bot))
    logger.info("YouTube Tracker cog loaded")


# Extended commands for the bot
async def setup_commands(bot: commands.Bot, tracker: YouTubeTracker) -> None:
    """Setup slash commands for the YouTube tracker."""

    @bot.tree.command(name="addchannel", description="Add a YouTube channel to track")
    @app_commands.describe(url="YouTube channel URL or @handle")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add_channel(interaction: discord.Interaction, url: str):
        """Add a YouTube channel to track."""
        await interaction.response.defer()

        # Extract channel ID
        channel_id = tracker.extract_channel_id(url)

        if not channel_id:
            await interaction.followup.send(
                "❌ Could not extract channel ID from the URL. Please use a valid YouTube channel URL.",
                ephemeral=True
            )
            return

        # If it's a handle, we need to try to resolve it
        if channel_id.startswith("@") or channel_id.startswith("c/") or channel_id.startswith("u/"):
            await interaction.followup.send(
                f"⚠️ Custom URLs (/{channel_id}) require manual channel ID lookup. "
                f"Please provide the channel ID (starts with UC) or use a /channel/ URL instead.",
                ephemeral=True
            )
            return

        # Check if channel already exists
        existing = db.get_channel_by_id(channel_id)
        if existing:
            await interaction.followup.send(
                f"⚠️ Channel `{existing['channel_name']}` is already being tracked!",
                ephemeral=True
            )
            return

        # Try to get channel info from RSS
        channel_info = await tracker.get_channel_info_from_feed(channel_id)

        if not channel_info:
            await interaction.followup.send(
                "❌ Could not fetch channel information. Please check the URL and try again.",
                ephemeral=True
            )
            return

        # Add channel to database
        discord_channel_id = interaction.channel_id if isinstance(interaction.channel, discord.TextChannel) else None

        success = db.add_channel(
            channel_id=channel_id,
            channel_name=channel_info["channel_name"],
            channel_url=channel_info["channel_url"],
            discord_channel_id=discord_channel_id
        )

        if success:
            await interaction.followup.send(
                f"✅ Now tracking YouTube channel: **{channel_info['channel_name']}**\n"
                f"📺 Channel ID: `{channel_id}`\n"
                f"🔗 {channel_info['channel_url']}"
            )
        else:
            await interaction.followup.send(
                "❌ Failed to add channel. It may already exist.",
                ephemeral=True
            )

    @bot.tree.command(name="removechannel", description="Remove a YouTube channel from tracking")
    @app_commands.describe(channel_id="YouTube channel ID to remove")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_channel(interaction: discord.Interaction, channel_id: str):
        """Remove a YouTube channel from tracking."""
        await interaction.response.defer()

        # Try to find the channel in our database
        channel = db.get_channel_by_id(channel_id)

        if not channel:
            # Try to find by name
            channels = db.get_all_channels()
            for ch in channels:
                if ch["channel_name"].lower() == channel_id.lower():
                    channel = ch
                    break

        if not channel:
            await interaction.followup.send(
                f"❌ Channel `{channel_id}` is not being tracked.",
                ephemeral=True
            )
            return

        # Remove the channel
        success = db.remove_channel(channel["channel_id"])

        if success:
            await interaction.followup.send(
                f"✅ Stopped tracking channel: **{channel['channel_name']}**"
            )
        else:
            await interaction.followup.send(
                "❌ Failed to remove channel.",
                ephemeral=True
            )

    @bot.tree.command(name="listchannels", description="List all tracked YouTube channels")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_channels(interaction: discord.Interaction):
        """List all tracked YouTube channels."""
        await interaction.response.defer()

        channels = db.get_all_channels()

        if not channels:
            await interaction.followup.send(
                "📭 No YouTube channels are being tracked yet.\n"
                "Use `/addchannel` to add one!"
            )
            return

        # Create embed with channel list
        embed = discord.Embed(
            title="📺 Tracked YouTube Channels",
            color=discord.Color.blue()
        )

        for ch in channels:
            status = "✅ Active" if ch["is_active"] else "⏸️ Paused"
            channel_mention = f"<#{ch['discord_channel_id']}>" if ch.get("discord_channel_id") else "Not set"

            embed.add_field(
                name=f"{ch['channel_name']}",
                value=f"ID: `{ch['channel_id']}`\n"
                      f"Status: {status}\n"
                      f"Notifies: {channel_mention}\n"
                      f"[YouTube]({ch['channel_url']})",
                inline=True
            )

        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="settarget", description="Set Discord channel for notifications")
    @app_commands.describe(channel="Discord channel to send notifications to")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_target(interaction: discord.Interaction, channel: discord.TextChannel):
        """Set the Discord channel for notifications."""
        await interaction.response.defer()

        # Update all channels to use this channel, or set a default
        # For now, let's set the channel in settings
        db.set_setting("default_discord_channel", str(channel.id))

        # Also update the current channel for this context
        if isinstance(interaction.channel, discord.TextChannel):
            # Update all existing channels to use this channel
            channels = db.get_all_channels()
            for ch in channels:
                db.set_discord_channel(ch["channel_id"], channel.id)

        await interaction.followup.send(
            f"✅ Notifications will now be sent to {channel.mention}"
        )

    @bot.tree.command(name="pausechannel", description="Pause notifications for a channel")
    @app_commands.describe(channel_id="YouTube channel ID to pause")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def pause_channel(interaction: discord.Interaction, channel_id: str):
        """Pause notifications for a channel."""
        await interaction.response.defer()

        channel = db.get_channel_by_id(channel_id)
        if not channel:
            await interaction.followup.send(
                f"❌ Channel `{channel_id}` not found.",
                ephemeral=True
            )
            return

        db.set_channel_active(channel_id, False)
        await interaction.followup.send(
            f"⏸️ Paused tracking for **{channel['channel_name']}**"
        )

    @bot.tree.command(name="resumechannel", description="Resume notifications for a channel")
    @app_commands.describe(channel_id="YouTube channel ID to resume")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def resume_channel(interaction: discord.Interaction, channel_id: str):
        """Resume notifications for a channel."""
        await interaction.response.defer()

        channel = db.get_channel_by_id(channel_id)
        if not channel:
            await interaction.followup.send(
                f"❌ Channel `{channel_id}` not found.",
                ephemeral=True
            )
            return

        db.set_channel_active(channel_id, True)
        await interaction.followup.send(
            f"✅ Resumed tracking for **{channel['channel_name']}**"
        )

    @bot.tree.command(name="ytinfo", description="Get info about a YouTube channel")
    @app_commands.describe(url="YouTube channel URL or ID")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def yt_info(interaction: discord.Interaction, url: str):
        """Get information about a YouTube channel."""
        await interaction.response.defer()

        channel_id = tracker.extract_channel_id(url)

        if not channel_id:
            await interaction.followup.send(
                "❌ Could not extract channel ID from the URL.",
                ephemeral=True
            )
            return

        # Check if it's already tracked
        tracked = db.get_channel_by_id(channel_id)

        # Try to get channel info from RSS
        channel_info = await tracker.get_channel_info_from_feed(channel_id)

        if not channel_info:
            await interaction.followup.send(
                "❌ Could not fetch channel information. Please check the URL.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title=channel_info["channel_name"],
            url=channel_info["channel_url"],
            color=discord.Color.blue()
        )

        embed.add_field(name="Channel ID", value=f"`{channel_id}`", inline=False)
        embed.add_field(
            name="Status",
            value="✅ Currently tracked" if tracked else "❌ Not tracked",
            inline=False
        )

        if tracked:
            embed.add_field(
                name="Active",
                value="Yes" if tracked["is_active"] else "No",
                inline=True
            )
            if tracked.get("discord_channel_id"):
                embed.add_field(
                    name="Notifications",
                    value=f"<#{tracked['discord_channel_id']}>",
                    inline=True
                )

        await interaction.followup.send(embed=embed)

    logger.info("Slash commands registered")


# Export for main.py
__all__ = ["YouTubeTracker", "setup", "setup_commands"]