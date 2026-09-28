"""Guild-scoped YouTube RSS monitoring and Discord notification commands."""

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

def is_trusted_or_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        owner_id = config.BOT_OWNER_ID
        if owner_id is None:
            try:
                application = await interaction.client.application_info()
                owner_id = application.owner.id if application.owner else None
            except (discord.HTTPException, discord.Forbidden):
                owner_id = None
        return (
            interaction.user.id == owner_id
            or interaction.user.id == interaction.guild.owner_id
            or db.is_trusted_user(interaction.guild.id, interaction.user.id)
        )

    return app_commands.check(predicate)


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
            for monitored in db.get_yt_monitored_channels():
                try:
                    await self._process_channel(monitored)
                except Exception:
                    logger.exception(
                        "Failed to process YouTube channel %s for guild %s",
                        monitored["yt_channel_id"],
                        monitored["guild_id"],
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
        xml = await self._fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
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

    async def _prime_channel(self, guild_id: int, channel_id: str) -> int:
        """Seed the current feed and advance this guild's cursor without notifying."""
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
        if videos:
            db.update_yt_last_video(guild_id, channel_id, videos[0]["video_id"])
        return inserted

    async def _process_channel(self, monitored: dict[str, Any]) -> None:
        guild_id = int(monitored["guild_id"])
        channel_id = monitored["yt_channel_id"]
        videos = await self.fetch_feed(channel_id)
        if not videos:
            logger.warning("RSS returned no entries for %s", channel_id)
            return

        # The RSS feed is newest-first. Work oldest-to-newest.
        videos.reverse()
        last_video_id = monitored.get("last_video_id")

        if not last_video_id:
            db.update_yt_last_video(
                guild_id, channel_id, videos[-1]["video_id"]
            )
            return

        try:
            cursor_index = next(
                i for i, video in enumerate(videos)
                if video["video_id"] == last_video_id
            )
        except StopIteration:
            # If the cursor has fallen outside the RSS window, avoid replaying
            # the entire feed. Move to the newest item without sending a burst.
            db.update_yt_last_video(
                guild_id, channel_id, videos[-1]["video_id"]
            )
            return

        new_videos = videos[cursor_index + 1:]
        if not new_videos:
            return

        settings = db.get_guild_settings(guild_id)
        target_id = (
            settings.get("yt_notification_channel_id")
            if settings else None
        )
        if not target_id:
            logger.warning(
                "No YouTube notification channel configured for guild %s",
                guild_id,
            )
            return

        for video in new_videos:
            if not await self._send_notification(int(target_id), video):
                break

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
            db.update_yt_last_video(
                guild_id, channel_id, video["video_id"]
            )

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
        self, guild_id: int, url: str
    ) -> dict[str, Any]:
        channel_id = await self.resolve_channel_id(url)
        videos = await self.fetch_feed(channel_id)
        if not videos:
            raise ValueError("The channel RSS feed is empty or unavailable.")

        channel_name = next(
            (v["channel_name"] for v in videos if v["channel_name"]),
            channel_id,
        )
        existing = db.get_yt_monitored_channel(guild_id, channel_id)
        db.add_yt_monitored_channel(
            guild_id=guild_id,
            yt_channel_id=channel_id,
            yt_channel_name=channel_name,
            yt_channel_url=f"https://www.youtube.com/channel/{channel_id}",
            last_video_id=existing.get("last_video_id") if existing else None,
        )
        if not existing:
            await self._prime_channel(guild_id, channel_id)
        return {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "video_count": len(videos),
            "already_tracked": existing is not None,
        }


def setup_commands(bot: commands.Bot, tracker: YouTubeTracker) -> None:
    trust_group = app_commands.Group(name="trust", description="Manage trusted users.")

    @trust_group.command(name="add", description="Trust a user for administrative bot commands.")
    @is_trusted_or_owner()
    @app_commands.describe(user="User to trust in this server")
    async def trust_add(interaction: discord.Interaction, user: discord.Member) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return
        db.add_trusted_user(interaction.guild.id, user.id, interaction.user.id)
        await interaction.response.send_message(f"✅ {user.mention} is now trusted.", ephemeral=True)

    @trust_group.command(name="remove", description="Revoke a user's trusted status.")
    @is_trusted_or_owner()
    @app_commands.describe(user="User to remove from this server's trusted list")
    async def trust_remove(interaction: discord.Interaction, user: discord.Member) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return
        changed = db.remove_trusted_user(interaction.guild.id, user.id)
        await interaction.response.send_message("✅ Trusted status removed." if changed else "User is not trusted.", ephemeral=True)

    @trust_group.command(name="list", description="List trusted users in this server.")
    @is_trusted_or_owner()
    async def trust_list(interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return
        users = db.get_trusted_users(interaction.guild.id)
        embed = discord.Embed(title=f"🛡️ Trusted Users — {interaction.guild.name}", color=discord.Color.blurple())
        embed.description = "No trusted users are configured." if not users else "\n".join(f"<@{row['user_id']}> — added by <@{row['added_by']}>" for row in users)[:4096]
        await interaction.response.send_message(embed=embed, ephemeral=True)

    bot.tree.add_command(trust_group)
    @bot.tree.command(name="setup_logs", description="Set this server's audit log channel.")
    @is_trusted_or_owner()
    @app_commands.describe(channel="Channel where server audit logs will be posted")
    async def setup_logs(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return
        db.set_audit_log_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(f"Audit logs will now be sent to {channel.mention}.", ephemeral=True)

    @bot.tree.command(name="setup_yt", description="Set this server's YouTube notification channel.")
    @is_trusted_or_owner()
    @app_commands.describe(channel="Channel where YouTube notifications will be posted")
    async def setup_yt(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return
        db.set_yt_notification_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(f"YouTube notifications will now be sent to {channel.mention}.", ephemeral=True)

    @bot.tree.command(name="add_yt", description="Register a YouTube channel for this server.")
    @is_trusted_or_owner()
    @app_commands.describe(url="YouTube channel URL or @handle URL")
    async def add_yt(interaction: discord.Interaction, url: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await tracker.add_channel(interaction.guild.id, url)
            status = "already tracked" if result["already_tracked"] else "now tracking"
            await interaction.followup.send(
                f"**{result['channel_name']}** ({result['channel_id']}) is {status}. "
                f"Current RSS entries: {result['video_count']}.",
                ephemeral=True,
            )
        except Exception as exc:
            logger.exception("add_yt failed for guild %s", interaction.guild.id)
            await interaction.followup.send(f"Could not add YouTube channel: {exc}", ephemeral=True)

    @bot.tree.command(name="remove_yt", description="Remove a tracked YouTube channel from this server.")
    @is_trusted_or_owner()
    @app_commands.describe(url_or_id="YouTube channel URL or channel ID")
    async def remove_yt(interaction: discord.Interaction, url_or_id: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return
        value = url_or_id.strip()
        match = CHANNEL_ID_RE.search(value)
        channel_id = match.group(0) if match else value
        if not match and value.startswith(("http://", "https://")):
            await interaction.response.defer(ephemeral=True)
            try:
                channel_id = await tracker.resolve_channel_id(value)
            except Exception as exc:
                await interaction.followup.send(f"Could not resolve that YouTube URL: {exc}", ephemeral=True)
                return
            changed = db.remove_yt_monitored_channel(interaction.guild.id, channel_id)
            await interaction.followup.send(
                "YouTube channel removed from this server." if changed else "That YouTube channel is not tracked in this server.",
                ephemeral=True,
            )
            return
        changed = db.remove_yt_monitored_channel(interaction.guild.id, channel_id)
        await interaction.response.send_message(
            "YouTube channel removed from this server." if changed else "That YouTube channel is not tracked in this server.",
            ephemeral=True,
        )

    @bot.tree.command(name="list_yt", description="List this server's tracked YouTube channels.")
    @is_trusted_or_owner()
    async def list_yt(interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return
        channels = db.get_yt_monitored_channels(interaction.guild.id)
        settings = db.get_guild_settings(interaction.guild.id)
        target_id = settings.get("yt_notification_channel_id") if settings else None
        if not channels:
            await interaction.response.send_message("No YouTube channels are currently tracked in this server.", ephemeral=True)
            return
        target = f"<#{target_id}>" if target_id else "Not configured"
        embed = discord.Embed(
            title=f"📺 YouTube Tracking — {interaction.guild.name}",
            description=f"Notification destination: {target}",
            color=discord.Color.red(),
        )
        for item in channels:
            embed.add_field(
                name=item["yt_channel_name"][:256],
                value=(
                    f"[YouTube channel]({item['yt_channel_url']})\n"
                    f"ID: {item['yt_channel_id']}\n"
                    f"Destination: {target}"
                )[:1024],
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="ytinfo", description="Resolve a YouTube URL to its channel ID.")
    @app_commands.describe(url="YouTube channel URL or @handle URL")
    async def ytinfo(interaction: discord.Interaction, url: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            channel_id = await tracker.resolve_channel_id(url)
            videos = await tracker.fetch_feed(channel_id)
            name = next((v["channel_name"] for v in videos if v["channel_name"]), channel_id)
            await interaction.followup.send(
                f"{name}\nChannel ID: {channel_id}\nRSS entries available: {len(videos)}",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(f"Could not resolve channel: {exc}", ephemeral=True)
