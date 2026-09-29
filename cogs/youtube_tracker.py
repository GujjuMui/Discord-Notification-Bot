"""Guild-scoped YouTube RSS monitoring and Discord notification commands."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import psutil
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


async def user_is_authorized(interaction: discord.Interaction) -> bool:
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


def _format_uptime(started_at: Optional[datetime]) -> str:
    if not started_at:
        return "Unknown"
    seconds = max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{days}d {hours}h {minutes}m {seconds}s"


class HelpSelect(discord.ui.Select):
    def __init__(self, user_id: int, admin_view: bool):
        self.user_id = user_id
        self.admin_view = admin_view
        options = [
            discord.SelectOption(label="Overview & Getting Started", value="overview", emoji="🏠", description="Quick start guide and bot overview."),
            discord.SelectOption(label="YouTube Feed Routing", value="youtube", emoji="📺", description="Add, remove, and list YouTube notification routes."),
            discord.SelectOption(label=("Audit Logging Setup" if admin_view else "Audit Logging Setup 🔒 Admin/Trusted Required"), value="logging", emoji="📁", description="Configure the 8-channel server logging system."),
            discord.SelectOption(label=("Trust & Permissions" if admin_view else "Trust & Permissions 🔒 Admin/Trusted Required"), value="trust", emoji="🛡️", description="Manage trusted users and administrative access."),
            discord.SelectOption(label="System & Health", value="health", emoji="📊", description="View /botstatus and /about."),
        ]
        super().__init__(placeholder="Select a help category…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This help menu belongs to another user. Run /help to open your own.", ephemeral=True)
            return

        locked = not self.admin_view
        embeds = {
            "overview": discord.Embed(
                title="🏠 Overview & Getting Started",
                description="Discord Notification Bot combines YouTube feed routing with full categorized server audit logging.\n\nStart with /about for live bot information, then use /add_yt to route a YouTube channel into a Discord channel.",
                color=discord.Color.blurple(),
            ),
            "youtube": discord.Embed(
                title="📺 YouTube Feed Routing",
                description="/add_yt <url> <#target_channel> — subscribe a YouTube source.\n/remove_yt <url_or_id> [#target_channel] — remove one route or all routes.\n/list_yt — view all configured routes.\n/ytinfo <url> — resolve a YouTube channel and inspect its RSS feed.",
                color=discord.Color.red(),
            ),
            "logging": discord.Embed(
                title="📁 Audit Logging Setup",
                description=("🔒 Admin/Trusted Required\n\n" if locked else "") + "/setup_logs auto_create:True creates the final 8-channel logging system under 📁 SERVER LOGS.\n\nChannels: chat, member, profile, role, channel, server, voice, and moderation.",
                color=discord.Color.gold(),
            ),
            "trust": discord.Embed(
                title="🛡️ Trust & Permissions",
                description=("🔒 Admin/Trusted Required\n\n" if locked else "") + "/trust add <@user>\n/trust remove <@user>\n/trust list\n\nAdministrative setup commands are restricted to the bot owner, server owner, and trusted users.",
                color=discord.Color.green(),
            ),
            "health": discord.Embed(
                title="📊 System & Health",
                description="/botstatus — live operational dashboard (Admin/Trusted/Owner).\n/about — public bot profile with live server, feed, uptime, and latency stats.",
                color=discord.Color.blue(),
            ),
        }
        await interaction.response.send_message(embed=embeds[self.values[0]], ephemeral=True)


class HelpView(discord.ui.View):
    def __init__(self, user_id: int, admin_view: bool):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.add_item(HelpSelect(user_id, admin_view))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This help menu belongs to another user. Run /help to open your own.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


class YouTubeTracker(commands.Cog):
    """Poll YouTube RSS feeds and send one Discord notification per video."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self._poll_lock = asyncio.Lock()
        self._missing_target_alerted: set[tuple[int, int]] = set()

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
            monitored_channels = db.get_yt_monitored_channels()
            grouped: dict[str, list[dict[str, Any]]] = {}
            for monitored in monitored_channels:
                grouped.setdefault(monitored["yt_channel_id"], []).append(monitored)
            for yt_channel_id, subscriptions in grouped.items():
                try:
                    activity = await self.fetch_channel_activity(yt_channel_id)
                    await self._dispatch_activity(yt_channel_id, activity, subscriptions)
                except Exception:
                    logger.exception("Failed to process YouTube activity for channel %s", yt_channel_id)

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

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            if value.get("simpleText"):
                return str(value["simpleText"]).strip()
            runs = value.get("runs")
            if isinstance(runs, list):
                return "".join(str(run.get("text", "")) for run in runs if isinstance(run, dict)).strip()
        return str(value).strip()

    @staticmethod
    def _walk_json(value: Any):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from YouTubeTracker._walk_json(child)
        elif isinstance(value, list):
            for child in value:
                yield from YouTubeTracker._walk_json(child)

    @staticmethod
    def _extract_initial_data(html: str) -> Optional[dict[str, Any]]:
        markers = ('var ytInitialData = ', 'window["ytInitialData"] = ', "window['ytInitialData'] = ")
        start = -1
        for marker in markers:
            start = html.find(marker)
            if start >= 0:
                start += len(marker)
                break
        if start < 0:
            return None
        while start < len(html) and html[start].isspace():
            start += 1
        if start >= len(html) or html[start] != "{":
            return None
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(html)):
            char = html[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start:index + 1])
                    except json.JSONDecodeError:
                        return None
        return None

    @classmethod
    def _thumbnail_from_node(cls, node: dict[str, Any]) -> Optional[str]:
        thumbnails = node.get("thumbnail", {}).get("thumbnails", [])
        if isinstance(thumbnails, list) and thumbnails:
            item = thumbnails[-1]
            if isinstance(item, dict) and item.get("url"):
                return str(item["url"])
        return None

    async def _fetch_playlist_feed(
        self,
        playlist_id: str,
        channel_id: str,
        content_type: str,
    ) -> list[dict[str, Any]]:
        xml = await self._fetch(
            f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"
        )
        parsed = feedparser.parse(xml)
        items: list[dict[str, Any]] = []
        for entry in parsed.entries:
            video_id = str(getattr(entry, "yt_videoid", "") or "").strip()
            if not video_id:
                continue
            thumbnails = getattr(entry, "media_thumbnail", None)
            thumbnail_url = None
            if thumbnails:
                try:
                    thumbnail_url = thumbnails[0].get("url")
                except (IndexError, AttributeError, TypeError):
                    pass
            items.append({
                "content_id": video_id,
                "content_type": content_type,
                "video_id": video_id,
                "channel_id": channel_id,
                "channel_name": str(getattr(entry, "author", "") or "").strip(),
                "title": str(getattr(entry, "title", "Untitled")),
                "video_url": (
                    f"https://www.youtube.com/shorts/{video_id}"
                    if content_type == "short"
                    else f"https://www.youtube.com/watch?v={video_id}"
                ),
                "thumbnail_url": thumbnail_url,
                "published_at": str(getattr(entry, "published", "") or "") or None,
                "description": "",
                "duration": None,
                "status": "UNKNOWN" if content_type == "live" else None,
                "scheduled_start": None,
                "post_text": None,
                "post_images": [],
                "is_short": content_type == "short",
                "is_live": content_type == "live",
            })
        return items

    async def fetch_feed(self, channel_id: str) -> list[dict[str, Any]]:
        return await self._fetch_playlist_feed(channel_id, channel_id, "video")

    async def _enrich_live_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in items[:8]:
            try:
                html = await self._fetch(item["video_url"])
                if re.search(r'"isLiveNow":true|\\\"isLiveNow\\\":true', html):
                    item["status"] = "LIVE"
                elif "upcomingEventData" in html or re.search(r'"isUpcoming":true|\\\"isUpcoming\\\":true', html):
                    item["status"] = "UPCOMING"
                else:
                    item["status"] = "ENDED"
                match = re.search(r'"startTime":"(\d+)"', html)
                if match:
                    item["scheduled_start"] = datetime.fromtimestamp(
                        int(match.group(1)),
                        tz=timezone.utc,
                    ).isoformat()
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
                logger.debug("Could not enrich live item %s", item["content_id"])
        return items

    @classmethod
    def _community_items_from_page(
        cls,
        html: str,
        channel_id: str,
    ) -> list[dict[str, Any]]:
        data = cls._extract_initial_data(html)
        if not data:
            return []
        items: dict[str, dict[str, Any]] = {}
        for node in cls._walk_json(data):
            post_id = str(node.get("postId") or node.get("externalPostId") or "").strip()
            if not post_id:
                continue
            text = (
                cls._text(node.get("contentText"))
                or cls._text(node.get("content"))
                or cls._text(node.get("headline"))
            )
            images: list[str] = []
            attachment = node.get("backstageAttachment") or node.get("backstageAttachmentRenderer")
            if isinstance(attachment, dict):
                for child in cls._walk_json(attachment):
                    image = child.get("image") if isinstance(child, dict) else None
                    thumbs = image.get("thumbnails", []) if isinstance(image, dict) else []
                    if isinstance(thumbs, list) and thumbs:
                        url = thumbs[-1].get("url") if isinstance(thumbs[-1], dict) else None
                        if url:
                            images.append(str(url))
            items[post_id] = {
                "content_id": post_id,
                "content_type": "community",
                "channel_id": channel_id,
                "channel_name": "",
                "title": "Community Post",
                "video_url": f"https://www.youtube.com/post/{post_id}",
                "thumbnail_url": images[0] if images else None,
                "published_at": None,
                "description": "",
                "duration": None,
                "status": None,
                "scheduled_start": None,
                "post_text": text or "New community post",
                "post_images": list(dict.fromkeys(images))[:4],
            }
        return list(items.values())

    async def fetch_channel_activity(self, channel_id: str) -> list[dict[str, Any]]:
        activity: list[dict[str, Any]] = []
        feeds = (
            (f"UULF{channel_id[2:]}", "video"),
            (f"UUSH{channel_id[2:]}", "short"),
            (f"UULV{channel_id[2:]}", "live"),
        )
        for playlist_id, content_type in feeds:
            try:
                items = await self._fetch_playlist_feed(playlist_id, channel_id, content_type)
                if content_type == "live":
                    items = await self._enrich_live_items(items)
                activity.extend(items)
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
                logger.warning("Could not fetch YouTube %s feed for %s", content_type, channel_id)

        try:
            html = await self._fetch(
                f"https://www.youtube.com/channel/{channel_id}/community"
            )
            activity.extend(self._community_items_from_page(html, channel_id))
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
            logger.warning("Could not fetch YouTube community surface for %s", channel_id)

        if not activity:
            activity = await self.fetch_feed(channel_id)
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for item in activity:
            key = (item["content_id"], item["content_type"])
            if key not in merged:
                merged[key] = item
            else:
                for field in (
                    "thumbnail_url",
                    "published_at",
                    "duration",
                    "status",
                    "scheduled_start",
                    "description",
                ):
                    if item.get(field):
                        merged[key][field] = item[field]
        return list(merged.values())

    async def _prime_subscription(
        self,
        guild_id: int,
        yt_channel_id: str,
        subscription: dict[str, Any],
        activity: list[dict[str, Any]],
    ) -> None:
        enabled = self._normalize_content_types(subscription.get("content_types"))
        for item in activity:
            if item["content_type"] in enabled:
                db.mark_yt_content_notified(
                    guild_id,
                    yt_channel_id,
                    item["content_id"],
                    item["content_type"],
                )

    async def _prime_subscription(
        self,
        guild_id: int,
        yt_channel_id: str,
        discord_target_channel_id: int,
        videos: list[dict[str, Any]],
    ) -> int:
        """Seed a subscription's current feed and advance its cursor."""
        if videos:
            db.update_yt_last_video(
                guild_id,
                yt_channel_id,
                discord_target_channel_id,
                videos[0]["video_id"],
            )

        # The subscription cursor is sufficient to prevent current RSS entries
        # from being announced, so we do not need to write every historical
        # entry into the legacy videos table.
        return len(videos)

    @staticmethod
    def _new_videos_for_subscription(
        videos: list[dict[str, Any]],
        last_video_id: Optional[str],
    ) -> list[dict[str, Any]]:
        if not videos or not last_video_id:
            return []

        oldest_first = list(reversed(videos))
        try:
            cursor_index = next(
                i for i, video in enumerate(oldest_first)
                if video["video_id"] == last_video_id
            )
        except StopIteration:
            return []

        return oldest_first[cursor_index + 1:]

    async def _dispatch_feed(
        self,
        yt_channel_id: str,
        videos: list[dict[str, Any]],
        subscriptions: list[dict[str, Any]],
    ) -> None:
        if not videos:
            logger.warning("RSS returned no entries for %s", yt_channel_id)
            return

        # Fetch the RSS feed once, then route each new video to every
        # subscribed Discord target concurrently.
        oldest_first = list(reversed(videos))
        pending: dict[str, list[dict[str, Any]]] = {}

        for subscription in subscriptions:
            guild_id = int(subscription["guild_id"])
            target_id = int(subscription["discord_target_channel_id"])
            last_video_id = subscription.get("last_video_id")

            if target_id <= 0:
                logger.warning(
                    "Skipping YouTube subscription %s/%s with no valid Discord target",
                    guild_id,
                    yt_channel_id,
                )
                continue

            if not last_video_id:
                db.update_yt_last_video(
                    guild_id,
                    yt_channel_id,
                    target_id,
                    oldest_first[-1]["video_id"],
                )
                continue

            try:
                cursor_index = next(
                    i for i, video in enumerate(oldest_first)
                    if video["video_id"] == last_video_id
                )
            except StopIteration:
                db.update_yt_last_video(
                    guild_id,
                    yt_channel_id,
                    target_id,
                    oldest_first[-1]["video_id"],
                )
                continue

            for video in oldest_first[cursor_index + 1:]:
                pending.setdefault(video["video_id"], []).append(subscription)

        for video in oldest_first:
            targets = pending.get(video["video_id"], [])
            if not targets:
                continue

            results = await asyncio.gather(
                *[
                    self._send_notification(
                        int(subscription["discord_target_channel_id"]),
                        video,
                        int(subscription["guild_id"]),
                        int(subscription["ping_role_id"]) if subscription.get("ping_role_id") else None,
                    )
                    for subscription in targets
                ],
                return_exceptions=True,
            )

            for subscription, result in zip(targets, results):
                if isinstance(result, Exception):
                    logger.exception(
                        "Failed dispatching video %s to Discord target %s",
                        video["video_id"],
                        subscription["discord_target_channel_id"],
                        exc_info=result,
                    )
                    continue

                if not result:
                    continue

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
                    int(subscription["guild_id"]),
                    yt_channel_id,
                    int(subscription["discord_target_channel_id"]),
                    video["video_id"],
                )

    async def _send_notification(
        self,
        discord_channel_id: int,
        video: dict[str, Any],
        guild_id: Optional[int] = None,
        ping_role_id: Optional[int] = None,
    ) -> bool:
        channel = self.bot.get_channel(discord_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(discord_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.error("YouTube target channel %s is unavailable: %s", discord_channel_id, exc)
                if guild_id is not None:
                    await self._notify_missing_target(guild_id, discord_channel_id)
                return False

        if not hasattr(channel, "send"):
            logger.error("Discord channel %s is not sendable", discord_channel_id)
            if guild_id is not None:
                await self._notify_missing_target(guild_id, discord_channel_id)
            return False

        channel_name = video.get("channel_name") or "YouTube"
        embed = discord.Embed(
            title=f"🎥 {video['title']}",
            url=video["video_url"],
            description=f"**{channel_name}** just uploaded a new video!",
            color=discord.Color(0xFF0000),
        )
        embed.add_field(name="Channel", value=f"**{channel_name}**", inline=True)
        if video.get("published_at"):
            embed.add_field(name="Published", value=str(video["published_at"]), inline=True)
        embed.add_field(name="Video", value=f"[Watch on YouTube]({video['video_url']})", inline=False)

        published = self._parse_datetime(video.get("published_at"))
        if published:
            embed.timestamp = published
        if video.get("thumbnail_url"):
            embed.set_image(url=video["thumbnail_url"])
        embed.set_footer(text="YouTube Notification Bot • New upload")

        content = (
            f"Hey <@&{ping_role_id}>! **[{channel_name}]** just uploaded a new video!"
            if ping_role_id
            else f"**{channel_name}** just uploaded a new video!"
        )
        try:
            await channel.send(
                content=content or None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
            if guild_id is not None:
                self._missing_target_alerted.discard((guild_id, discord_channel_id))
            logger.info("Sent notification for video %s", video["video_id"])
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.error("Failed to send notification for %s: %s", video["video_id"], exc)
            if guild_id is not None and isinstance(exc, discord.Forbidden):
                await self._notify_missing_target(guild_id, discord_channel_id)
            return False

    async def _notify_missing_target(self, guild_id: int, channel_id: int) -> None:
        key = (guild_id, channel_id)
        if key in self._missing_target_alerted:
            return
        self._missing_target_alerted.add(key)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        message = (
            f"⚠️ YouTube notification target <#{channel_id}> is unavailable or I cannot "
            "send there. The route remains stored, but notifications will pause until "
            "the target is restored or removed with /remove_yt."
        )
        try:
            owner = guild.owner or await self.bot.fetch_user(guild.owner_id)
            if owner:
                await owner.send(message)
                return
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Could not DM guild owner about missing YouTube target %s", channel_id)
        fallback = guild.system_channel
        if fallback and hasattr(fallback, "send"):
            try:
                await fallback.send(message)
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Could not use guild fallback channel for missing YouTube target %s", channel_id)

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
        self,
        guild_id: int,
        url: str,
        discord_target_channel_id: int,
        ping_role_id: Optional[int] = None,
    ) -> dict[str, Any]:
        channel_id = await self.resolve_channel_id(url)
        videos = await self.fetch_feed(channel_id)
        if not videos:
            raise ValueError("The channel RSS feed is empty or unavailable.")

        channel_name = next(
            (v["channel_name"] for v in videos if v["channel_name"]),
            channel_id,
        )
        existing = db.get_yt_monitored_channel(
            guild_id,
            channel_id,
            discord_target_channel_id,
        )

        db.add_yt_monitored_channel(
            guild_id=guild_id,
            yt_channel_id=channel_id,
            yt_channel_name=channel_name,
            yt_channel_url=f"https://www.youtube.com/channel/{channel_id}",
            discord_target_channel_id=discord_target_channel_id,
            ping_role_id=ping_role_id,
            last_video_id=existing.get("last_video_id") if existing else None,
        )

        if not existing:
            await self._prime_subscription(
                guild_id,
                channel_id,
                discord_target_channel_id,
                videos,
            )

        return {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "video_count": len(videos),
            "already_tracked": existing is not None,
            "discord_target_channel_id": discord_target_channel_id,
            "ping_role_id": ping_role_id,
            "channel_url": f"https://www.youtube.com/channel/{channel_id}",
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
    @bot.tree.command(name="about", description="Learn about the bot and view live public statistics.")
    async def about(interaction: discord.Interaction) -> None:
        started_at = getattr(interaction.client, "bot_started_at", None)
        uptime = _format_uptime(started_at)
        latency = f"{round(interaction.client.latency * 1000)} ms" if interaction.client.latency >= 0 else "Unavailable"
        feed_count = db.count_yt_feeds()
        embed = discord.Embed(
            title="🤖 Discord Notification Bot",
            description="A production-focused Discord bot for YouTube feed routing and full categorized server audit logging.",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="✨ Mission", value="Deliver reliable YouTube notifications while preserving detailed, organized server activity history.", inline=False)
        embed.add_field(name="📺 YouTube Routing", value="RSS-based monitoring with per-server, per-channel Discord destinations.", inline=True)
        embed.add_field(name="📁 Audit Logging", value="8 dedicated channels covering chat, members, profiles, roles, channels, server, voice, and moderation.", inline=True)
        embed.add_field(name="📊 Live Stats", value=f"Servers: **{len(interaction.client.guilds)}**\nMonitored feeds: **{feed_count}**\nUptime: **{uptime}**\nGateway latency: **{latency}**", inline=False)
        app_id = interaction.client.user.id if interaction.client.user else 0
        invite = f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot%20applications.commands&permissions=2147601408"
        repo_url = "https://github.com/GujjuMui/Discord-Notification-Bot"
        embed.add_field(name="🔗 Quick Links", value=f"[Support]({repo_url}/issues) • [Invite]({invite}) • [GitHub]({repo_url}) • [Docs]({repo_url}#readme)", inline=False)
        embed.set_footer(text="v2.1.0 • Developed / powered by GujjuMui")
        await interaction.response.send_message(embed=embed)


    @bot.tree.command(name="help", description="Open the interactive public command guide.")
    async def help_menu(interaction: discord.Interaction) -> None:
        admin_view = await user_is_authorized(interaction)
        embed = discord.Embed(
            title="📖 Discord Notification Bot Help",
            description="Choose a category below. This menu is user-scoped, so multiple users can use /help at the same time without affecting each other.\n\n🔒 Admin/Trusted badges mark restricted administrative features.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Quick Start", value="1. /about → overview\n2. /add_yt → add a YouTube route\n3. /setup_logs → configure audit logging", inline=False)
        await interaction.response.send_message(embed=embed, view=HelpView(interaction.user.id, admin_view))


    @bot.tree.command(name="botstatus", description="View the live bot health dashboard.")
    @is_trusted_or_owner()
    async def botstatus(interaction: discord.Interaction) -> None:
        process = psutil.Process()
        memory_mb = process.memory_info().rss / (1024 * 1024)
        cpu_percent = psutil.cpu_percent(interval=None)
        latency_ms = interaction.client.latency * 1000
        try:
            database_messages = db.count_cached_messages()
            feed_count = db.count_yt_feeds()
            db_size_mb = db.database_size_bytes() / (1024 * 1024)
            database_status = "🟢 Connected"
        except Exception:
            database_messages = feed_count = 0
            db_size_mb = 0
            database_status = "🔴 Error"
            logger.exception("Health dashboard database check failed.")
        rss_status = "🟡 No monitored feed configured"
        if tracker:
            monitored = db.get_yt_monitored_channels()
            if monitored:
                try:
                    await tracker.fetch_feed(monitored[0]["yt_channel_id"])
                    rss_status = "🟢 Reachable"
                except Exception as exc:
                    logger.warning("YouTube RSS health check failed: %s", exc)
                    rss_status = "🔴 Unreachable"
        gateway_status = "🟢 Connected" if interaction.client.is_ready() else "🔴 Disconnected"
        embed = discord.Embed(
            title="📊 System Health Dashboard",
            description="Live operational health for the bot process.",
            color=discord.Color.green() if gateway_status.startswith("🟢") and rss_status.startswith("🟢") else discord.Color.orange(),
        )
        embed.add_field(name="Discord", value=f"Gateway: **{gateway_status}**\nLatency: **{round(latency_ms)} ms**", inline=True)
        embed.add_field(name="Runtime", value=f"Uptime: **{_format_uptime(getattr(interaction.client, 'bot_started_at', None))}**\nRAM: **{memory_mb:.1f} MB**\nCPU: **{cpu_percent:.1f}%**", inline=True)
        embed.add_field(name="SQLite", value=f"Status: **{database_status}**\nMessages: **{database_messages:,}**\nYT feeds: **{feed_count:,}**\nSize: **{db_size_mb:.2f} MB**", inline=False)
        embed.add_field(name="External API", value=f"YouTube RSS: **{rss_status}**\nDiscord Gateway: **{gateway_status}**", inline=False)
        embed.set_footer(text="Admin / Trusted / Owner only")
        await interaction.response.send_message(embed=embed, ephemeral=True)


    @bot.tree.command(name="setup_logs", description="Create or map categorized server audit log channels.")
    @is_trusted_or_owner()
    @app_commands.describe(
        auto_create="Create the 📁 SERVER LOGS category and all 8 log channels automatically.",
        type="For manual mapping: chat/member/profile/role/channel/server/voice/mod.",
        channel="Existing text channel to use for the selected log type.",
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name="chat", value="chat"),
            app_commands.Choice(name="member", value="member"),
            app_commands.Choice(name="profile", value="profile"),
            app_commands.Choice(name="role", value="role"),
            app_commands.Choice(name="channel", value="channel"),
            app_commands.Choice(name="server", value="server"),
            app_commands.Choice(name="voice", value="voice"),
            app_commands.Choice(name="mod", value="mod"),
        ]
    )
    async def setup_logs(
        interaction: discord.Interaction,
        auto_create: bool = True,
        type: Optional[app_commands.Choice[str]] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        server_logger = interaction.client.get_cog("ServerLogger")
        if server_logger is None:
            await interaction.response.send_message(
                "❌ Server logger is not loaded.",
                ephemeral=True,
            )
            return

        try:
            result = await server_logger.configure_logs(
                interaction.guild,
                auto_create=auto_create,
                log_type=type.value if type else None,
                channel=channel,
            )
        except (ValueError, discord.Forbidden, discord.HTTPException, RuntimeError) as exc:
            await interaction.response.send_message(
                f"❌ Could not configure server logs: {exc}",
                ephemeral=True,
            )
            return

        if auto_create:
            mentions = "\n".join(
                f"• **{key}** → {value.mention}"
                for key, value in result.items()
            )
            await interaction.response.send_message(
                "✅ **Server logging configured.**\n"
                "Created/linked the categorized logging channels:\n" + mentions,
                ephemeral=True,
            )
        else:
            mapped = next(iter(result.values()))
            await interaction.response.send_message(
                f"✅ **{type.value if type else 'log'}** logs will now go to {mapped.mention}.",
                ephemeral=True,
            )

    @bot.tree.command(name="add_yt", description="Register a YouTube channel, destination, and optional role ping.")
    @is_trusted_or_owner()
    @app_commands.describe(
        url="YouTube channel URL or @handle URL",
        target_channel="Discord channel where notifications for this YouTube source will be posted",
        role="Optional role to ping when a new video is detected",
    )
    async def add_yt(
        interaction: discord.Interaction,
        url: str,
        target_channel: discord.TextChannel,
        role: Optional[discord.Role] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            result = await tracker.add_channel(
                interaction.guild.id,
                url,
                target_channel.id,
                role.id if role else None,
            )
            if result["already_tracked"]:
                message = (
                    f"**{result['channel_name']}** is already subscribed to "
                    f"{target_channel.mention}"
                    + (f" and pings {role.mention}." if role else ".")
                )
            else:
                message = (
                    f"✅ Subscribed **{result['channel_name']}** → "
                    f"Notifications will post in {target_channel.mention}"
                    + (f" and ping {role.mention}." if role else ".")
                )

            embed = discord.Embed(
                title="YouTube Subscription",
                description=message,
                color=discord.Color.green(),
            )
            embed.add_field(
                name="YouTube Channel",
                value=f"[Open channel]({result['channel_url'] if 'channel_url' in result else f'https://www.youtube.com/channel/{result['channel_id']}'})",
                inline=False,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception(
                "add_yt failed for guild %s",
                interaction.guild.id,
            )
            await interaction.followup.send(
                f"Could not add YouTube channel: {exc}",
                ephemeral=True,
            )

    @bot.tree.command(name="remove_yt", description="Remove a YouTube subscription from this server.")
    @is_trusted_or_owner()
    @app_commands.describe(
        url_or_id="YouTube channel URL, @handle URL, or channel ID",
        target_channel="Optional Discord target. Omit to remove every subscription for this YouTube source.",
    )
    async def remove_yt(
        interaction: discord.Interaction,
        url_or_id: str,
        target_channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        value = url_or_id.strip()
        match = CHANNEL_ID_RE.search(value)
        channel_id = match.group(0) if match else value

        if not match and value.startswith(("http://", "https://", "youtube.com", "www.youtube.com", "@")):
            await interaction.response.defer(ephemeral=True)
            try:
                channel_id = await tracker.resolve_channel_id(value)
            except Exception as exc:
                await interaction.followup.send(
                    f"Could not resolve that YouTube URL: {exc}",
                    ephemeral=True,
                )
                return

            changed = db.remove_yt_monitored_channel(
                interaction.guild.id,
                channel_id,
                target_channel.id if target_channel else None,
            )
            if target_channel:
                message = (
                    f"Removed **{channel_id}** from {target_channel.mention}."
                    if changed
                    else "That YouTube source is not subscribed to that Discord channel."
                )
            else:
                message = (
                    "Removed all Discord subscriptions for that YouTube source."
                    if changed
                    else "That YouTube source is not tracked in this server."
                )
            await interaction.followup.send(message, ephemeral=True)
            return

        changed = db.remove_yt_monitored_channel(
            interaction.guild.id,
            channel_id,
            target_channel.id if target_channel else None,
        )
        if target_channel:
            message = (
                f"Removed **{channel_id}** from {target_channel.mention}."
                if changed
                else "That YouTube source is not subscribed to that Discord channel."
            )
        else:
            message = (
                "Removed all Discord subscriptions for that YouTube source."
                if changed
                else "That YouTube source is not tracked in this server."
            )
        await interaction.response.send_message(message, ephemeral=True)

    @bot.tree.command(name="list_yt", description="List this server's tracked YouTube subscriptions.")
    @is_trusted_or_owner()
    async def list_yt(interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        channels = db.get_yt_monitored_channels(interaction.guild.id)
        if not channels:
            await interaction.response.send_message(
                "No YouTube channels are currently tracked in this server.",
                ephemeral=True,
            )
            return

        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in channels:
            grouped.setdefault(item["yt_channel_id"], []).append(item)

        embed = discord.Embed(
            title=f"📺 YouTube Tracking — {interaction.guild.name}",
            color=discord.Color.red(),
        )

        for subscriptions in grouped.values():
            first = subscriptions[0]
            destinations = []
            for item in subscriptions:
                target_id = int(item["discord_target_channel_id"])
                target = f"<#{target_id}>" if target_id > 0 else "Not configured"
                ping_role = f"<@&{item['ping_role_id']}>" if item.get("ping_role_id") else "None"
                destinations.append(
                    f"• [{item['yt_channel_name']}]({item['yt_channel_url']}) ➔ {target} "
                    f"(Pings: {ping_role})"
                )

            embed.add_field(
                name=first["yt_channel_name"][:256],
                value="\n".join(destinations)[:1024],
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="test_yt", description="Send a realistic YouTube notification preview.")
    @is_trusted_or_owner()
    @app_commands.describe(
        target_channel="Discord channel where the test notification will be posted",
        role="Optional role to ping in the test notification",
    )
    async def test_yt(
        interaction: discord.Interaction,
        target_channel: discord.TextChannel,
        role: Optional[discord.Role] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="[TEST PREVIEW] 🎥 MrBeast uploaded a new video!",
            description="A realistic preview of the YouTube upload notification.",
            color=discord.Color(0xFF0000),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Video Title", value="**I Survived 7 Days In An Abandoned City**", inline=False)
        embed.add_field(name="Channel", value="**MrBeast**", inline=True)
        embed.add_field(name="Duration", value="24:18", inline=True)
        embed.add_field(name="Published", value="Just now", inline=True)
        embed.set_image(url="https://placehold.co/1280x720/png?text=MrBeast+HD+Thumbnail")
        embed.set_footer(text="YouTube Notification Bot • TEST PREVIEW")

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Watch on YouTube",
                style=discord.ButtonStyle.link,
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                disabled=True,
            )
        )

        content = f"{role.mention} " if role else ""
        await target_channel.send(
            content=content or None,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        await interaction.response.send_message(
            f"✅ Test notification sent to {target_channel.mention}"
            + (f" with {role.mention} ping." if role else "."),
            ephemeral=True,
        )

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
