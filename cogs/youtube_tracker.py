"""YouTube RSS monitoring and Discord notification commands."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp
import discord
import feedparser
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import db

logger = logging.getLogger(__name__)

CHANNEL_ID_RE = re.compile(r"UC[a-zA-Z0-9_-]{22}")
CHANNEL_ID_PATTERNS = (
    re.compile(r'"channelId":"(UC[a-zA-Z0-9_-]{22})"'),
    re.compile(r'"externalId":"(UC[a-zA-Z0-9_-]{22})"'),
    re.compile(
        r'<meta[^>]+itemprop=["\']channelId["\'][^>]+content=["\']'
        r'(UC[a-zA-Z0-9_-]{22})["\']',
        re.I,
    ),
    re.compile(r"/channel/(UC[a-zA-Z0-9_-]{22})"),
)


class YouTubeTracker(commands.Cog):
    """Poll YouTube RSS feeds and send one Discord notification per video."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self._poll_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT),
            headers={"User-Agent": config.HTTP_USER_AGENT},
        )
        self.poll_loop.change_interval(seconds=config.POLL_INTERVAL)
        self.poll_loop.start()

    async def cog_unload(self) -> None:
        self.poll_loop.cancel()
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    @tasks.loop(seconds=60)
    async def poll_loop(self) -> None:
        async with self._poll_lock:
            for channel in db.get_active_channels():
                try:
                    await self._process_channel(channel)
                except Exception:
                    logger.exception(
                        "Failed to process YouTube channel %s",
                        channel["channel_id"],
                    )

    @poll_loop.before_loop
    async def before_poll_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _fetch(self, url: str) -> str:
        if not self.session or self.session.closed:
            raise RuntimeError("HTTP session is not available")
        async with self.session.get(url, allow_redirects=True) as response:
            response.raise_for_status()
            return await response.text()

    async def resolve_channel_id(self, url: str) -> str:
        normalized = url.strip()
        if not normalized.startswith(("http://", "https://")):
            normalized = "https://" + normalized

        match = CHANNEL_ID_RE.search(normalized)
        if match:
            return match.group(0)

        parsed = urlparse(normalized)
        if parsed.netloc.lower() not in {
            "youtube.com", "www.youtube.com", "m.youtube.com"
        }:
            raise ValueError("Please provide a valid youtube.com channel URL.")

        html = await self._fetch(normalized)
        for pattern in CHANNEL_ID_PATTERNS:
            match = pattern.search(html)
            if match:
                return match.group(1)

        raise ValueError(
            "Could not find the channel ID. Try a full /channel/UC... URL."
        )

    async def fetch_feed(self, channel_id: str) -> list[dict[str, Any]]:
        xml = await self._fetch(config.YOUTUBE_RSS_URL.format(channel_id=channel_id))
        parsed = feedparser.parse(xml)

        if getattr(parsed, "bozo", False) and not parsed.entries:
            raise RuntimeError("YouTube RSS returned invalid or empty XML.")

        videos: list[dict[str, Any]] = []
        for entry in parsed.entries:
            video_id = str(getattr(entry, "yt_videoid", "") or "").strip()
            if not video_id:
                entry_id = str(getattr(entry, "id", "") or "")
                if entry_id.startswith("yt:video:"):
                    video_id = entry_id.rsplit(":", 1)[-1]
            if not video_id:
                continue

            thumbnail_url = None
            thumbnails = getattr(entry, "media_thumbnail", None)
            if thumbnails:
                try:
                    thumbnail_url = thumbnails[0].get("url")
                except (IndexError, AttributeError, TypeError):
                    pass

            author = str(getattr(entry, "author", "") or "").strip()
            videos.append({
                "video_id": video_id,
                "channel_id": channel_id,
                "channel_name": author,
                "title": str(getattr(entry, "title", "Untitled video")),
                "video_url": str(
                    getattr(entry, "link", "")
                    or f"https://www.youtube.com/watch?v={video_id}"
                ),
                "thumbnail_url": thumbnail_url,
                "published_at": str(getattr(entry, "published", "") or "") or None,
                "is_short": False,
                "is_live": False,
            })
        return videos

    async def _prime_channel(self, channel_id: str) -> int:
        """Record existing feed entries as already known, without notifying."""
        videos = await self.fetch_feed(channel_id)
        inserted = 0
        for video in videos:
            if db.add_video(
                video_id=video["video_id"],
                channel_id=video["channel_id"],
                title=video["title"],
                video_url=video["video_url"],
                thumbnail_url=video["thumbnail_url"],
                published_at=video["published_at"],
                is_short=video["is_short"],
                is_live=video["is_live"],
                notified=True,
            ):
                inserted += 1
        return inserted

    async def _process_channel(self, channel: dict[str, Any]) -> None:
        videos = await self.fetch_feed(channel["channel_id"])
        if not videos:
            logger.warning("RSS returned no entries for %s", channel["channel_id"])
            return

        if not db.has_videos(channel["channel_id"]):
            for video in videos:
                db.add_video(
                    video_id=video["video_id"],
                    channel_id=video["channel_id"],
                    title=video["title"],
                    video_url=video["video_url"],
                    thumbnail_url=video["thumbnail_url"],
                    published_at=video["published_at"],
                    is_short=video["is_short"],
                    is_live=video["is_live"],
                    notified=True,
                )
            logger.info(
                "Initialized %s with %d existing videos",
                channel["channel_name"],
                len(videos),
            )
            return

        videos.reverse()

        for video in videos:
            existing = db.get_video(video["video_id"])
            if existing and existing.get("notified_at"):
                continue

            if not existing:
                db.add_video(
                    video_id=video["video_id"],
                    channel_id=video["channel_id"],
                    title=video["title"],
                    video_url=video["video_url"],
                    thumbnail_url=video["thumbnail_url"],
                    published_at=video["published_at"],
                    is_short=video["is_short"],
                    is_live=video["is_live"],
                    notified=False,
                )

            target_id = channel["discord_channel_id"] or config.DISCORD_CHANNEL_ID
            if not target_id:
                logger.warning(
                    "No Discord target configured for YouTube channel %s",
                    channel["channel_id"],
                )
                continue

            if await self._send_notification(int(target_id), video):
                db.mark_video_notified(video["video_id"])

    async def _send_notification(
        self, discord_channel_id: int, video: dict[str, Any]
    ) -> bool:
        channel = self.bot.get_channel(discord_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(discord_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.error("Could not access Discord channel %s: %s", discord_channel_id, exc)
                return False

        if not hasattr(channel, "send"):
            logger.error("Discord channel %s is not sendable", discord_channel_id)
            return False

        embed = discord.Embed(
            title=f"🎥 {video['title']}",
            url=video["video_url"],
            description=f"New video from **{video.get('channel_name') or 'YouTube'}**",
            color=discord.Color.red(),
        )

        published = self._parse_datetime(video.get("published_at"))
        if published:
            embed.timestamp = published
        if video.get("thumbnail_url"):
            embed.set_thumbnail(url=video["thumbnail_url"])
        embed.set_footer(text="YouTube Notification Bot")

        try:
            await channel.send(embed=embed)
            logger.info("Sent notification for video %s", video["video_id"])
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.error("Failed to send notification for %s: %s", video["video_id"], exc)
            return False

    @staticmethod
    def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    async def add_channel(
        self, url: str, discord_channel_id: Optional[int] = None
    ) -> dict[str, Any]:
        channel_id = await self.resolve_channel_id(url)
        videos = await self.fetch_feed(channel_id)
        if not videos:
            raise ValueError("The channel RSS feed is empty or unavailable.")

        channel_name = next(
            (v["channel_name"] for v in videos if v["channel_name"]),
            channel_id,
        )
        db.add_channel(
            channel_id=channel_id,
            channel_name=channel_name,
            channel_url=f"https://www.youtube.com/channel/{channel_id}",
            discord_channel_id=discord_channel_id or config.DISCORD_CHANNEL_ID,
        )
        await self._prime_channel(channel_id)
        return {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "video_count": len(videos),
        }


def _manage_server():
    return app_commands.checks.has_permissions(manage_guild=True)


def setup_commands(bot: commands.Bot, tracker: YouTubeTracker) -> None:
    @bot.tree.command(name="addchannel", description="Start tracking a YouTube channel.")
    @_manage_server()
    @app_commands.describe(url="YouTube channel URL or @handle URL")
    async def addchannel(interaction: discord.Interaction, url: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            result = await tracker.add_channel(url)
            await interaction.followup.send(
                f"Now tracking {result['channel_name']} ({result['channel_id']}). "
                f"Seeded {result['video_count']} existing entries without notifying them.",
                ephemeral=True,
            )
        except Exception as exc:
            logger.exception("addchannel failed")
            await interaction.followup.send(
                f"Could not add channel: {exc}", ephemeral=True
            )

    @bot.tree.command(name="removechannel", description="Stop tracking a YouTube channel.")
    @_manage_server()
    @app_commands.describe(channel_id="YouTube channel ID (UC...)")
    async def removechannel(interaction: discord.Interaction, channel_id: str) -> None:
        changed = db.remove_channel(channel_id.strip())
        await interaction.response.send_message(
            "Channel paused. Video history was kept to prevent duplicate notifications if re-added."
            if changed else "Channel not found.",
            ephemeral=True,
        )

    @bot.tree.command(name="listchannels", description="List tracked YouTube channels.")
    @_manage_server()
    async def listchannels(interaction: discord.Interaction) -> None:
        channels = db.get_active_channels()
        if not channels:
            await interaction.response.send_message(
                "No active YouTube channels are being tracked.", ephemeral=True
            )
            return

        lines = [
            f"{c['channel_name']} ({c['channel_id']}) -> "
            f"<#{c['discord_channel_id'] or config.DISCORD_CHANNEL_ID}>"
            for c in channels
        ]
        await interaction.response.send_message("\\n".join(lines), ephemeral=True)

    @bot.tree.command(
        name="settarget",
        description="Set the Discord notification channel for a YouTube channel.",
    )
    @_manage_server()
    @app_commands.describe(
        channel_id="YouTube channel ID (UC...)",
        channel="Discord channel that receives notifications",
    )
    async def settarget(
        interaction: discord.Interaction,
        channel_id: str,
        channel: discord.TextChannel,
    ) -> None:
        if not db.set_discord_channel(channel_id.strip(), channel.id):
            await interaction.response.send_message(
                "Channel not found.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Notifications for {channel_id} will be sent to {channel.mention}.",
            ephemeral=True,
        )

    @bot.tree.command(name="pausechannel", description="Pause a YouTube channel.")
    @_manage_server()
    @app_commands.describe(channel_id="YouTube channel ID (UC...)")
    async def pausechannel(interaction: discord.Interaction, channel_id: str) -> None:
        changed = db.set_channel_active(channel_id.strip(), False)
        await interaction.response.send_message(
            "Channel paused." if changed else "Channel not found.",
            ephemeral=True,
        )

    @bot.tree.command(name="resumechannel", description="Resume a YouTube channel.")
    @_manage_server()
    @app_commands.describe(channel_id="YouTube channel ID (UC...)")
    async def resumechannel(interaction: discord.Interaction, channel_id: str) -> None:
        changed = db.set_channel_active(channel_id.strip(), True)
        await interaction.response.send_message(
            "Channel resumed." if changed else "Channel not found.",
            ephemeral=True,
        )

    @bot.tree.command(name="ytinfo", description="Resolve a YouTube URL to its channel ID.")
    @app_commands.describe(url="YouTube channel URL or @handle URL")
    async def ytinfo(interaction: discord.Interaction, url: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            channel_id = await tracker.resolve_channel_id(url)
            videos = await tracker.fetch_feed(channel_id)
            name = next(
                (v["channel_name"] for v in videos if v["channel_name"]),
                channel_id,
            )
            await interaction.followup.send(
                f"{name}\\nChannel ID: {channel_id}\\n"
                f"RSS entries available: {len(videos)}",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(
                f"Could not resolve channel: {exc}", ephemeral=True
            )
