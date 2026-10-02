"""YouTube WebSub (PubSubHubbub) push engine + RSS fallback poller.

Architecture:
  PRIMARY  — Google PubSubHubbub push notifications via an embedded aiohttp
             webhook server.  Zero polling overhead; Google pushes within
             seconds of a new upload.
  FALLBACK — A lightweight 15-minute RSS poll that catches any pushes Google
             failed to deliver (rare but documented).
  DEDUP    — SQLite yt_content_route_cache keyed on (guild, channel, content_id)
             prevents duplicate Discord alerts regardless of which path fired.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import aiohttp
import aiohttp.web
import discord
import feedparser
import psutil
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import db
from utils.helpers import format_mentions, extract_mention_ids

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & guards
# ---------------------------------------------------------------------------

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

_ALLOWED_FETCH_HOSTS = frozenset({
    "www.youtube.com",
    "youtube.com",
    "m.youtube.com",
    "img.youtube.com",
    "i.ytimg.com",
    "yt3.ggpht.com",
    "yt3.googleusercontent.com",
    "pubsubhubbub.appspot.com",
})

_MENTION_STRIP_RE = re.compile(
    r"@(everyone|here)|<@[!&]?\d+>|<#\d+>|<@\d+>",
    re.I,
)

# Atom namespace used in YouTube's feed and WebSub push payloads
_YT_NS  = "http://www.youtube.com/xml/schemas/2015"
_ATOM_NS = "http://www.w3.org/2005/Atom"

# WebSub hub endpoint
_WEBSUB_HUB = "https://pubsubhubbub.appspot.com/subscribe"

# Lease duration to request — 7 days (604800 s).  We re-subscribe at T-24 h.
_LEASE_SECONDS = 604800

# oEmbed endpoint — used for lightweight content-type classification
_OEMBED_URL = "https://www.youtube.com/oembed?url={url}&format=json"


# ---------------------------------------------------------------------------
# Permission helper (mirrors server_logger.is_trusted_or_owner)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Help menu UI (unchanged from original)
# ---------------------------------------------------------------------------

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
            discord.SelectOption(label="Overview & Getting Started", value="overview", emoji="🏠",
                                 description="Quick start guide and bot overview."),
            discord.SelectOption(label="YouTube Feed Routing", value="youtube", emoji="📺",
                                 description="Add, remove, and list YouTube notification routes."),
            discord.SelectOption(
                label=("Audit Logging Setup" if admin_view else "Audit Logging Setup 🔒 Admin/Trusted Required"),
                value="logging", emoji="📁",
                description="Configure the 8-channel server logging system."),
            discord.SelectOption(
                label=("Trust & Permissions" if admin_view else "Trust & Permissions 🔒 Admin/Trusted Required"),
                value="trust", emoji="🛡️",
                description="Manage trusted users and administrative access."),
            discord.SelectOption(label="System & Health", value="health", emoji="📊",
                                 description="View /botstatus and /about."),
        ]
        super().__init__(placeholder="Select a help category…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This help menu belongs to another user. Run /help to open your own.",
                ephemeral=True,
            )
            return
        locked = not self.admin_view
        embeds = {
            "overview": discord.Embed(
                title="🏠 Overview & Getting Started",
                description=(
                    "Discord Notification Bot combines YouTube feed routing with full "
                    "categorized server audit logging.\n\nStart with /about for live bot "
                    "information, then use /add_yt to route a YouTube channel into a Discord channel."
                ),
                color=discord.Color.blurple(),
            ),
            "youtube": discord.Embed(
                title="📺 YouTube Feed Routing",
                description=(
                    "/add_yt <url> <#target_channel> [role] [types] — subscribe and filter content.\n"
                    "/remove_yt <url_or_id> [#target_channel] — remove one route or all routes.\n"
                    "/list_yt — view all configured routes.\n"
                    "/ytinfo <url> — resolve a YouTube channel and inspect its RSS feed."
                ),
                color=discord.Color.red(),
            ),
            "logging": discord.Embed(
                title="📁 Audit Logging Setup",
                description=(
                    ("🔒 Admin/Trusted Required\n\n" if locked else "")
                    + "/setup_logs auto_create:True creates the final 8-channel logging system "
                      "under 📁 SERVER LOGS.\n\nChannels: chat, member, profile, role, channel, "
                      "server, voice, and moderation."
                ),
                color=discord.Color.gold(),
            ),
            "trust": discord.Embed(
                title="🛡️ Trust & Permissions",
                description=(
                    ("🔒 Admin/Trusted Required\n\n" if locked else "")
                    + "/trust add <@user>\n/trust remove <@user>\n/trust list\n\n"
                      "Administrative setup commands are restricted to the bot owner, "
                      "server owner, and trusted users."
                ),
                color=discord.Color.green(),
            ),
            "health": discord.Embed(
                title="📊 System & Health",
                description=(
                    "/botstatus — live operational dashboard (Admin/Trusted/Owner).\n"
                    "/about — public bot profile with live server, feed, uptime, and latency stats."
                ),
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
            await interaction.response.send_message(
                "This help menu belongs to another user. Run /help to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


# ---------------------------------------------------------------------------
# YouTubeTracker cog
# ---------------------------------------------------------------------------

class YouTubeTracker(commands.Cog):
    """WebSub push engine with RSS fallback for YouTube notifications."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None

        # WebSub webhook server state
        self._webhook_runner: Optional[aiohttp.web.AppRunner] = None
        self._webhook_site: Optional[aiohttp.web.TCPSite] = None

        # RSS fallback state
        self._poll_lock = asyncio.Lock()
        self._missing_target_alerted: set[tuple[int, int]] = set()

        # Warmup: tracks which (guild_id, yt_channel_id, target_id) routes have
        # been seeded this process lifetime so we never spam historical videos.
        self._warmed_up: set[tuple[str, str, str]] = set()

        # RSS exponential backoff
        self._rss_fail_count: dict[str, int] = {}
        self._rss_skip_until: dict[str, int] = {}
        self._poll_cycle: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT),
            headers={"User-Agent": config.HTTP_USER_AGENT},
        )

        # Start webhook server if WEBHOOK_URL is configured
        webhook_url = getattr(config, "WEBHOOK_URL", None) or ""
        webhook_port = int(getattr(config, "WEBHOOK_PORT", 8080))
        if webhook_url:
            await self._start_webhook_server(webhook_port)
            logger.info("[WebSub] Webhook server started on port %d", webhook_port)
        else:
            logger.info(
                "[WebSub] WEBHOOK_URL not set — WebSub disabled. "
                "Set WEBHOOK_URL=https://your-domain.com/youtube/webhook in Railway."
            )

        # Start background tasks — fallback RSS runs every 60s as safety net
        self.poll_loop.change_interval(seconds=max(60, getattr(config, "FALLBACK_POLL_INTERVAL", 60)))
        self.poll_loop.start()
        self.resubscribe_loop.start()

    async def cog_unload(self) -> None:
        self.poll_loop.cancel()
        self.resubscribe_loop.cancel()
        await self._stop_webhook_server()
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    # ------------------------------------------------------------------
    # WebSub webhook server
    # ------------------------------------------------------------------

    async def _start_webhook_server(self, port: int) -> None:
        app = aiohttp.web.Application()
        app.router.add_get("/youtube/webhook", self._handle_websub_verify)
        app.router.add_post("/youtube/webhook", self._handle_websub_push)
        app.router.add_get("/health", self._handle_health)

        self._webhook_runner = aiohttp.web.AppRunner(app)
        await self._webhook_runner.setup()
        self._webhook_site = aiohttp.web.TCPSite(
            self._webhook_runner, host="0.0.0.0", port=port
        )
        await self._webhook_site.start()

    async def _stop_webhook_server(self) -> None:
        if self._webhook_site:
            await self._webhook_site.stop()
        if self._webhook_runner:
            await self._webhook_runner.cleanup()
        self._webhook_runner = None
        self._webhook_site = None

    async def _handle_health(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text="OK")

    async def _handle_websub_verify(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Handle Google's hub verification challenge (GET).

        Google sends:
          hub.mode        = 'subscribe' | 'unsubscribe'
          hub.topic       = feed URL
          hub.challenge   = random string we must echo back
          hub.lease_seconds = granted lease duration
        """
        params = request.rel_url.query
        mode      = params.get("hub.mode", "")
        challenge = params.get("hub.challenge", "")
        topic     = params.get("hub.topic", "")
        lease_sec = int(params.get("hub.lease_seconds", _LEASE_SECONDS))

        if mode not in ("subscribe", "unsubscribe") or not challenge:
            logger.warning("[WebSub] Bad verification request: mode=%r topic=%r", mode, topic)
            return aiohttp.web.Response(status=400, text="bad request")

        # Extract channel_id from topic URL
        match = re.search(r"channel_id=(UC[a-zA-Z0-9_-]{22})", topic)
        if not match:
            logger.warning("[WebSub] Verification topic has no valid channel_id: %r", topic)
            return aiohttp.web.Response(status=400, text="unknown topic")

        channel_id = match.group(1)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_sec)
        ).isoformat()
        db.update_yt_websub_lease(channel_id, expires_at, verified=True)

        logger.info(
            "[WebSub] Verified %s subscription for %s (lease %ds, expires %s)",
            mode, channel_id, lease_sec, expires_at,
        )
        # Echo the challenge to confirm subscription
        return aiohttp.web.Response(text=challenge)

    async def _handle_websub_push(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Handle incoming Atom push notification (POST) from Google."""
        try:
            body = await request.read()
        except Exception as exc:
            logger.warning("[WebSub] Could not read push body: %s", exc)
            return aiohttp.web.Response(status=400)

        # Acknowledge immediately — Google retries if we don't respond fast.
        # Attach a done-callback so any unhandled exception inside
        # _process_push_payload is logged rather than silently swallowed.
        task = asyncio.ensure_future(self._process_push_payload(body))
        task.add_done_callback(
            lambda t: logger.exception(
                "[WebSub] Unhandled error in push payload processor"
            ) if not t.cancelled() and t.exception() else None
        )
        return aiohttp.web.Response(status=204)

    async def _process_push_payload(self, body: bytes) -> None:
        """Parse the Atom XML payload Google sends on new uploads."""
        try:
            root = ET.fromstring(body.decode("utf-8", errors="replace"))
        except ET.ParseError as exc:
            logger.warning("[WebSub] XML parse error in push payload: %s", exc)
            return

        ns = {
            "atom": _ATOM_NS,
            "yt":   _YT_NS,
        }

        for entry in root.findall("atom:entry", ns):
            try:
                video_id_el   = entry.find("yt:videoId", ns)
                channel_id_el = entry.find("yt:channelId", ns)
                title_el      = entry.find("atom:title", ns)
                published_el  = entry.find("atom:published", ns)

                if video_id_el is None or channel_id_el is None:
                    continue

                video_id   = (video_id_el.text or "").strip()
                channel_id = (channel_id_el.text or "").strip()
                title      = (title_el.text if title_el is not None else "") or "Untitled"
                published  = (published_el.text if published_el is not None else "") or None

                if not video_id or not CHANNEL_ID_RE.fullmatch(channel_id):
                    continue

                logger.info(
                    "[WebSub] Push received: video=%s channel=%s title=%r",
                    video_id, channel_id, title[:60],
                )

                item = await self._classify_and_build_item(
                    video_id, channel_id, title, published
                )
                subscriptions = [
                    s for s in db.get_yt_monitored_channels()
                    if s["yt_channel_id"] == channel_id
                ]
                if subscriptions:
                    await self._dispatch_activity(channel_id, [item], subscriptions)
                else:
                    logger.warning(
                        "[WebSub] Push received for untracked channel %s — ignoring.",
                        channel_id,
                    )
            except Exception as exc:
                logger.exception(
                    "[WebSub] Failed to process push entry (video=%s): %s",
                    (video_id_el.text if video_id_el is not None else "?"), exc,
                )

    # ------------------------------------------------------------------
    # Content classification via oEmbed
    # ------------------------------------------------------------------

    async def _classify_and_build_item(
        self,
        video_id: str,
        channel_id: str,
        title: str,
        published: Optional[str],
    ) -> dict[str, Any]:
        """Use oEmbed to classify the video and get the channel name."""
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        content_type = "video"
        channel_name = ""

        try:
            oembed_url = _OEMBED_URL.format(url=video_url)
            async with self.session.get(
                oembed_url,
                timeout=aiohttp.ClientTimeout(total=8),
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    channel_name = data.get("author_name", "") or ""
                    # oEmbed type "video" covers both uploads and Shorts;
                    # "rich" typically means a live stream embed.
                    oembed_type = data.get("type", "video")
                    if oembed_type == "rich":
                        content_type = "live"
        except Exception:
            pass

        # Shorts heuristic: thumbnail ratio is typically 9:16 (taller than wide).
        # We also check if the video URL resolves to /shorts/ via a HEAD request.
        if content_type == "video":
            content_type = await self._detect_short(video_id)

        thumbnail_url = await self.get_valid_yt_thumbnail(video_id)
        final_url = (
            f"https://www.youtube.com/shorts/{video_id}"
            if content_type == "short"
            else video_url
        )

        return {
            "content_id":   video_id,
            "content_type": content_type,
            "video_id":     video_id,
            "channel_id":   channel_id,
            "channel_name": channel_name,
            "title":        title,
            "video_url":    final_url,
            "thumbnail_url": thumbnail_url,
            "published_at": published,
            "description":  "",
            "duration":     None,
            "status":       None,
            "scheduled_start": None,
            "post_text":    None,
            "post_images":  [],
            "is_short":     content_type == "short",
            "is_live":      content_type == "live",
        }

    async def _detect_short(self, video_id: str) -> str:
        """Return 'short' if the video is a YouTube Short, else 'video'.

        YouTube Shorts redirect /shorts/VIDEO_ID → same URL with status 200.
        A regular video returns 303/301 → /watch?v=.
        We use a HEAD request to avoid downloading the page body.
        """
        if not self.session or self.session.closed:
            return "video"
        try:
            shorts_url = f"https://www.youtube.com/shorts/{video_id}"
            async with self.session.head(
                shorts_url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=6),
            ) as resp:
                # 200 = it's a Short; 3xx = redirected away = regular video
                if resp.status == 200:
                    return "short"
        except Exception:
            pass
        return "video"

    # ------------------------------------------------------------------
    # WebSub subscription management
    # ------------------------------------------------------------------

    async def subscribe_channel(self, channel_id: str) -> bool:
        """Send a subscribe request to Google's WebSub hub."""
        webhook_url = getattr(config, "WEBHOOK_URL", None) or ""
        if not webhook_url:
            return False

        # Sanitize: strip trailing slashes/fragments so Google's strict URL
        # validator doesn't reject with "Invalid parameter: hub.callback".
        # Must be https://, no fragment, port in 80-90/440-450/1024-65535.
        callback = webhook_url.rstrip("/").split("#")[0].strip()
        if not callback.startswith("https://"):
            logger.warning(
                "[WebSub] WEBHOOK_URL must start with https:// — skipping subscribe for %s",
                channel_id,
            )
            return False

        topic = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        payload = {
            "hub.callback":      callback,
            "hub.topic":         topic,
            "hub.mode":          "subscribe",
            "hub.lease_seconds": str(_LEASE_SECONDS),
            "hub.verify":        "async",
        }
        try:
            async with self.session.post(
                _WEBSUB_HUB,
                data=payload,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as resp:
                # Hub returns 202 Accepted; verification comes via GET callback
                if resp.status in (200, 202, 204):
                    logger.info(
                        "[WebSub] Subscribe request accepted for %s (HTTP %d)",
                        channel_id, resp.status,
                    )
                    return True
                body = await resp.text()
                logger.warning(
                    "[WebSub] Subscribe failed for %s: HTTP %d — %s",
                    channel_id, resp.status, body[:200],
                )
                return False
        except Exception as exc:
            logger.warning("[WebSub] Subscribe request error for %s: %s", channel_id, exc)
            return False

    async def unsubscribe_channel(self, channel_id: str) -> bool:
        """Send an unsubscribe request to Google's WebSub hub."""
        webhook_url = getattr(config, "WEBHOOK_URL", None) or ""
        if not webhook_url:
            return False

        callback = webhook_url.rstrip("/").split("#")[0].strip()
        topic = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        payload = {
            "hub.callback": callback,
            "hub.topic":    topic,
            "hub.mode":     "unsubscribe",
            "hub.verify":   "async",
        }
        try:
            async with self.session.post(
                _WEBSUB_HUB,
                data=payload,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as resp:
                return resp.status in (200, 202, 204)
        except Exception as exc:
            logger.warning("[WebSub] Unsubscribe error for %s: %s", channel_id, exc)
            return False

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    @tasks.loop(hours=6)
    async def resubscribe_loop(self) -> None:
        """Re-subscribe channels whose WebSub lease expires within 24 hours."""
        webhook_url = getattr(config, "WEBHOOK_URL", None) or ""
        if not webhook_url:
            return

        # On the very first run after startup, wait 15 seconds so Railway's
        # reverse proxy and TLS termination are fully ready before sending
        # subscribe requests to Google (avoids HTTP 400 on cold boot).
        if self._poll_cycle == 0:
            await asyncio.sleep(15)

        channels = db.get_channels_needing_resubscription(within_hours=24)
        if not channels:
            return

        logger.info(
            "[WebSub] Re-subscribing %d channel(s) with expiring leases.",
            len(channels),
        )
        for row in channels:
            channel_id = row["yt_channel_id"]
            ok = await self.subscribe_channel(channel_id)
            if not ok:
                logger.warning(
                    "[WebSub] Re-subscribe failed for %s — will retry next cycle.",
                    channel_id,
                )
            # Spread requests to avoid hammering the hub
            await asyncio.sleep(1)

    @resubscribe_loop.before_loop
    async def before_resubscribe_loop(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=900)
    async def poll_loop(self) -> None:
        """Fallback RSS poll — catches any WebSub pushes Google failed to deliver."""
        async with self._poll_lock:
            self._poll_cycle += 1
            monitored_channels = db.get_yt_monitored_channels()
            grouped: dict[str, list[dict[str, Any]]] = {}
            for monitored in monitored_channels:
                grouped.setdefault(monitored["yt_channel_id"], []).append(monitored)

            for yt_channel_id, subscriptions in grouped.items():
                skip_until = self._rss_skip_until.get(yt_channel_id, 0)
                if self._poll_cycle < skip_until:
                    logger.debug(
                        "[RSS Fallback] Skipping %s for %d more cycle(s).",
                        yt_channel_id, skip_until - self._poll_cycle,
                    )
                    continue

                try:
                    feed_items = await self.fetch_feed(yt_channel_id)

                    # Successful fetch — reset backoff counters
                    if yt_channel_id in self._rss_fail_count:
                        self._rss_fail_count.pop(yt_channel_id, None)
                        self._rss_skip_until.pop(yt_channel_id, None)
                        logger.info("[RSS Fallback] Feed recovered for %s.", yt_channel_id)

                    await self._warmup_subscription(yt_channel_id, feed_items, subscriptions)
                    await self._dispatch_activity(yt_channel_id, feed_items, subscriptions)

                except aiohttp.ClientResponseError as exc:
                    if exc.status == 404:
                        fails = self._rss_fail_count.get(yt_channel_id, 0) + 1
                        self._rss_fail_count[yt_channel_id] = fails
                        skip_cycles = min(2 ** (fails - 1), 16)
                        self._rss_skip_until[yt_channel_id] = self._poll_cycle + skip_cycles
                        logger.warning(
                            "[RSS Fallback] 404 for %s (fail #%d). "
                            "Backing off %d cycle(s).",
                            yt_channel_id, fails, skip_cycles,
                        )
                    else:
                        logger.exception(
                            "[RSS Fallback] HTTP error for %s", yt_channel_id
                        )
                except Exception:
                    logger.exception(
                        "[RSS Fallback] Unexpected error for %s", yt_channel_id
                    )

    @poll_loop.before_loop
    async def before_poll_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _fetch(self, url: str) -> str:
        if not self.session or self.session.closed:
            raise RuntimeError("HTTP session is not available")
        parsed_host = urlparse(url).hostname or ""
        if parsed_host.lower() not in _ALLOWED_FETCH_HOSTS:
            raise ValueError(
                f"Blocked outbound fetch to disallowed host: {parsed_host!r}"
            )
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
        if parsed.netloc.lower() not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
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
            text = value
        elif isinstance(value, (int, float)):
            text = str(value)
        elif isinstance(value, dict):
            for key in ("text", "simpleText", "content", "value", "title", "label"):
                if key in value:
                    result = YouTubeTracker._text(value.get(key))
                    if result:
                        return result
            runs = value.get("runs")
            if isinstance(runs, list):
                return "".join(
                    YouTubeTracker._text(run.get("text", ""))
                    for run in runs
                    if isinstance(run, dict)
                ).strip()
            return ""
        elif isinstance(value, (list, tuple)):
            return " ".join(YouTubeTracker._text(item) for item in value).strip()
        else:
            text = str(value)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @classmethod
    def _clean_url(cls, value: Any) -> Optional[str]:
        text = cls._text(value)
        if not text:
            return None
        match = re.search(r"https?://\S+", text)
        return match.group(0).rstrip('.,)<>\\"\'') if match else None

    @classmethod
    def _is_direct_image_url(cls, value: Any) -> Optional[str]:
        url = cls._clean_url(value)
        if not url:
            return None
        path = urlparse(url).path.lower()
        if not (path.endswith(".jpg") or path.endswith(".jpeg") or path.endswith(".png")):
            return None
        return url

    async def get_valid_yt_thumbnail(self, video_id: str) -> Optional[str]:
        video_id = self._text(video_id)
        if not video_id:
            return None
        candidates = (
            f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
            f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
        )
        if not self.session or self.session.closed:
            return candidates[1]
        for candidate in candidates:
            try:
                async with self.session.get(
                    candidate, allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    content_type = (response.headers.get("Content-Type") or "").lower()
                    if response.status == 200 and content_type.startswith("image/"):
                        return candidate
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
        return None

    async def _thumbnail_url(self, item: dict[str, Any]) -> Optional[str]:
        video_id = self._text(item.get("video_id") or item.get("content_id"))
        if video_id:
            thumbnail = await self.get_valid_yt_thumbnail(video_id)
            if thumbnail:
                return thumbnail
        return self._is_direct_image_url(item.get("thumbnail_url"))

    async def fetch_channel_name(self, channel_id: str) -> Optional[str]:
        try:
            html = await self._fetch(f"https://www.youtube.com/channel/{channel_id}")
            return self._channel_name_from_html(html)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
            logger.warning("Could not fetch YouTube channel name for %s", channel_id)
            return None

    @classmethod
    def _channel_name_from_html(cls, html: str) -> Optional[str]:
        patterns = (
            r"<meta[^>]+itemprop=[\"']name[\"'][^>]+content=[\"']([^\"']+)[\"']",
            r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)[\"']",
            r"<meta[^>]+name=[\"']title[\"'][^>]+content=[\"']([^\"']+)[\"']",
        )
        for pattern in patterns:
            match = re.search(pattern, html, re.I)
            if not match:
                continue
            name = html_lib.unescape(match.group(1)).strip()
            name = re.sub(r"\s+", " ", name).strip()
            if name:
                if name.lower().endswith(" - youtube"):
                    name = name[:-10].strip()
                if name and not CHANNEL_ID_RE.fullmatch(name):
                    return name[:100]
        return None

    # ------------------------------------------------------------------
    # RSS feed parsing (fallback + /ytinfo)
    # ------------------------------------------------------------------

    async def fetch_feed(self, channel_id: str) -> list[dict[str, Any]]:
        """Fetch and parse the public Atom feed for a channel's uploads."""
        xml = await self._fetch(
            f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        )
        parsed = feedparser.parse(xml)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            reason = str(getattr(parsed, "bozo_exception", "unknown Atom parse error"))
            raise RuntimeError(f"RSS parse error: {reason}")

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
                "content_id":   video_id,
                "content_type": "video",
                "video_id":     video_id,
                "channel_id":   channel_id,
                "channel_name": str(getattr(entry, "author", "") or "").strip(),
                "title":        str(getattr(entry, "title", "Untitled")),
                "video_url":    f"https://www.youtube.com/watch?v={video_id}",
                "thumbnail_url": thumbnail_url,
                "published_at": str(getattr(entry, "published", "") or "") or None,
                "description":  str(getattr(entry, "summary", "") or "")[:1500],
                "duration":     None,
                "status":       None,
                "scheduled_start": None,
                "post_text":    None,
                "post_images":  [],
                "is_short":     False,
                "is_live":      False,
            })
        return items

    # ------------------------------------------------------------------
    # Warmup — seed dedup cache to prevent historical spam
    # ------------------------------------------------------------------

    async def _warmup_subscription(
        self,
        yt_channel_id: str,
        activity: list[dict[str, Any]],
        subscriptions: list[dict[str, Any]],
    ) -> None:
        for subscription in subscriptions:
            guild_id  = str(subscription["guild_id"])
            target_id = str(subscription["discord_target_channel_id"])
            key = (guild_id, yt_channel_id, target_id)
            if key in self._warmed_up:
                continue

            if activity:
                enabled = self._normalize_content_types(subscription.get("content_types"))
                eligible_ids = [
                    item["content_id"] for item in activity
                    if item["content_type"] in enabled
                ]
                cached_count = sum(
                    1 for cid in eligible_ids
                    if db.has_yt_content_been_notified(
                        int(guild_id), yt_channel_id, cid, "video", int(target_id)
                    )
                )
                already_cached = cached_count > 0
            else:
                already_cached = False
                enabled = self._normalize_content_types(subscription.get("content_types"))

            if not already_cached:
                for item in activity:
                    if item["content_type"] in enabled:
                        db.mark_yt_content_notified(
                            int(guild_id),
                            yt_channel_id,
                            item["content_id"],
                            item["content_type"],
                            int(target_id),
                        )
                logger.info(
                    "[Warmup] Seeded %d items for %s → channel %s (guild %s). "
                    "No notifications sent — only future uploads will ping.",
                    len(activity), yt_channel_id, target_id, guild_id,
                )

            self._warmed_up.add(key)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_content_types(value: Optional[str]) -> set[str]:
        raw = str(value or "all").strip().lower()
        if raw == "all":
            return {"video", "short", "live", "community"}
        allowed = {
            "videos": "video", "video": "video",
            "shorts": "short", "short": "short",
            "live": "live",
            "community": "community",
        }
        result = {
            allowed[token.strip()]
            for token in raw.replace(";", ",").split(",")
            if token.strip() in allowed
        }
        return result or {"video", "short", "live", "community"}

    @staticmethod
    def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    async def _dispatch_activity(
        self,
        yt_channel_id: str,
        activity: list[dict[str, Any]],
        subscriptions: list[dict[str, Any]],
    ) -> None:
        if not activity:
            return

        for subscription in subscriptions:
            guild_id  = int(subscription["guild_id"])
            target_id = int(subscription["discord_target_channel_id"])
            if target_id <= 0:
                continue
            enabled = self._normalize_content_types(subscription.get("content_types"))
            route_items = [item for item in activity if item["content_type"] in enabled]

            # Sort newest-first; items with no timestamp go last.
            route_items.sort(
                key=lambda item: self._parse_datetime(item.get("published_at"))
                    or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            # Cap at 3 items per dispatch to contain blast radius on cold caches
            route_items = route_items[:3]

            for item in route_items:
                content_id   = item["content_id"]
                content_type = item["content_type"]

                if db.has_yt_content_been_notified(
                    guild_id, yt_channel_id, content_id, content_type, target_id
                ):
                    continue

                if not item.get("channel_name"):
                    item["channel_name"] = subscription.get("yt_channel_name") or "YouTube"

                sent = await self._send_notification(
                    target_id,
                    item,
                    guild_id,
                    int(subscription["ping_role_id"]) if subscription.get("ping_role_id") else None,
                    db.decode_yt_ping_users(subscription.get("ping_user_ids")),
                )
                if sent:
                    db.mark_yt_content_notified(
                        guild_id, yt_channel_id, content_id, content_type, target_id
                    )
                    if content_type in {"video", "short"}:
                        db.add_video(
                            video_id=content_id,
                            channel_id=yt_channel_id,
                            title=item["title"],
                            video_url=item["video_url"],
                            thumbnail_url=item.get("thumbnail_url"),
                            published_at=item.get("published_at"),
                            is_short=content_type == "short",
                            is_live=False,
                            notified=True,
                        )

    # ------------------------------------------------------------------
    # Discord notification sender
    # ------------------------------------------------------------------

    async def _send_notification(
        self,
        discord_channel_id: int,
        item: dict[str, Any],
        guild_id: Optional[int] = None,
        ping_role_id: Optional[int] = None,
        ping_user_ids: Optional[list[int]] = None,
    ) -> bool:
        channel = self.bot.get_channel(discord_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(discord_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.error(
                    "YouTube target channel %s unavailable: %s", discord_channel_id, exc
                )
                if guild_id is not None:
                    await self._notify_missing_target(guild_id, discord_channel_id)
                return False

        if not hasattr(channel, "send"):
            return False

        content_type = self._text(item.get("content_type")) or "video"
        channel_name = (
            _MENTION_STRIP_RE.sub("", self._text(item.get("channel_name")) or "YouTube").strip()
            or "YouTube"
        )
        title = (
            _MENTION_STRIP_RE.sub("", self._text(item.get("title")) or "Untitled").strip()
            or "Untitled"
        )
        video_id = self._text(item.get("video_id") or item.get("content_id"))
        url      = self._clean_url(item.get("video_url"))
        if not url and video_id:
            url = (
                f"https://www.youtube.com/shorts/{video_id}"
                if content_type == "short"
                else f"https://www.youtube.com/watch?v={video_id}"
            )
        url = url or "https://www.youtube.com/"

        thumbnail_url = await self._thumbnail_url(item)
        upload_time   = (
            self._parse_datetime(self._text(item.get("published_at")))
            or datetime.now(timezone.utc)
        )

        if content_type == "short":
            color  = 0xFF0000
            header = f"{channel_name} - YouTube Short"
            footer = "YouTube Notification Bot • New Short"
        elif content_type == "live":
            color  = 0xE62117
            status = self._text(item.get("status")) or "LIVE"
            header = f"{channel_name} - {'LIVE NOW' if status == 'LIVE' else 'Live Stream'}"
            footer = "YouTube Notification Bot • Live Stream"
        elif content_type == "community":
            color  = 0x4A90E2
            header = f"{channel_name} - Community Post"
            footer = "YouTube Notification Bot • Community Post"
        else:
            color  = 0xFF0000
            header = channel_name
            footer = "YouTube Notification Bot • New Upload"

        embed = discord.Embed(
            title=header[:256],
            url=url,
            description=f"**{title}**"[:4096],
            color=discord.Color(color),
            timestamp=upload_time,
        )
        embed.set_author(
            name="YouTube",
            icon_url="https://www.youtube.com/s/desktop/e4d15d2c/img/favicon_144x144.png",
        )
        if thumbnail_url:
            embed.set_image(url=thumbnail_url)

        if content_type == "live":
            status    = self._text(item.get("status")) or "LIVE"
            scheduled = self._text(item.get("scheduled_start")) or "Not provided"
            embed.add_field(
                name="🔴 Stream Status",
                value=f"**{status}** • Scheduled start: **{scheduled}**",
                inline=False,
            )
        elif content_type == "community":
            post_text = self._text(item.get("post_text"))
            if post_text:
                embed.add_field(name="Post", value=post_text[:1024], inline=False)
            poll = self._text(item.get("poll_preview"))
            if poll:
                embed.add_field(name="Poll", value=poll[:1024], inline=False)
        else:
            duration = self._text(item.get("duration"))
            if duration:
                embed.add_field(name="Duration", value=f"**{duration}**", inline=True)

        embed.set_footer(
            text=footer,
            icon_url="https://www.youtube.com/s/desktop/e4d15d2c/img/favicon_144x144.png",
        )

        ping_parts = [f"<@{user_id}>" for user_id in (ping_user_ids or [])]
        if ping_role_id:
            if guild_id is not None and ping_role_id == guild_id:
                ping_parts.append("@everyone")
            else:
                ping_parts.append(f"<@&{ping_role_id}>")

        content_type_label = {
            "short": "Short",
            "live": "Live Stream",
            "community": "Community Post",
        }.get(content_type, "video")

        header_message = (
            f"New {content_type_label} from **{channel_name}**!"
            if content_type != "video"
            else f"New video from **{channel_name}**!"
        )
        watch_line = f"Watch here: <{url}>"
        msg_content = "\n".join([header_message, watch_line, *ping_parts]).strip()

        try:
            await channel.send(
                content=msg_content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    everyone=True,
                    roles=bool(ping_role_id),
                    users=bool(ping_user_ids),
                    replied_user=True,
                ),
            )
            if guild_id is not None:
                self._missing_target_alerted.discard((guild_id, discord_channel_id))
            logger.info(
                "Sent YouTube %s notification for %s",
                content_type, video_id or item.get("content_id"),
            )
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.error(
                "Failed to send YouTube %s notification for %s: %s",
                content_type, video_id or item.get("content_id"), exc,
            )
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
            f"⚠️ YouTube notification target <#{channel_id}> is unavailable. "
            "The route remains stored, but notifications will pause until the "
            "target is restored or removed with /remove_yt."
        )
        try:
            owner = guild.owner or await self.bot.fetch_user(guild.owner_id)
            if owner:
                await owner.send(message)
                return
        except (discord.Forbidden, discord.HTTPException):
            pass
        fallback = guild.system_channel
        if fallback and hasattr(fallback, "send"):
            try:
                await fallback.send(message)
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ------------------------------------------------------------------
    # /add_yt helper
    # ------------------------------------------------------------------

    async def add_channel(
        self,
        guild_id: int,
        url: str,
        discord_target_channel_id: int,
        ping_role_id: Optional[int] = None,
        ping_user_ids: Optional[list[int]] = None,
        content_types: str = "all",
        channel_name_override: Optional[str] = None,
    ) -> dict[str, Any]:
        channel_id = await self.resolve_channel_id(url)
        feed_items = await self.fetch_feed(channel_id)

        detected_name = next(
            (
                self._text(item.get("channel_name"))
                for item in feed_items
                if self._text(item.get("channel_name"))
                and not CHANNEL_ID_RE.fullmatch(self._text(item.get("channel_name")))
            ),
            None,
        )
        channel_name = self._text(channel_name_override) or detected_name
        if not channel_name:
            channel_name = await self.fetch_channel_name(channel_id)
        if not channel_name:
            raise ValueError(
                "I could not detect the YouTube channel name. "
                "Run /add_yt again and fill in the optional **channel_name** field."
            )

        existing = db.get_yt_monitored_channel(
            guild_id, channel_id, discord_target_channel_id
        )
        db.add_yt_monitored_channel(
            guild_id=guild_id,
            yt_channel_id=channel_id,
            yt_channel_name=channel_name,
            yt_channel_url=f"https://www.youtube.com/channel/{channel_id}",
            discord_target_channel_id=discord_target_channel_id,
            ping_role_id=ping_role_id,
            ping_user_ids=ping_user_ids,
            content_types=content_types,
            last_video_id=existing.get("last_video_id") if existing else None,
        )

        # Seed dedup cache so first poll never re-fires historical items
        subscription = db.get_yt_monitored_channel(
            guild_id, channel_id, discord_target_channel_id
        )
        if not existing and subscription:
            enabled = self._normalize_content_types(content_types)
            for item in feed_items:
                if item["content_type"] in enabled:
                    db.mark_yt_content_notified(
                        guild_id, channel_id, item["content_id"],
                        item["content_type"], discord_target_channel_id,
                    )

        # Kick off a WebSub subscription in the background
        asyncio.ensure_future(self.subscribe_channel(channel_id))

        return {
            "channel_id":    channel_id,
            "channel_name":  channel_name,
            "video_count":   len([i for i in feed_items if i["content_type"] in {"video", "short", "live"}]),
            "already_tracked": existing is not None,
            "discord_target_channel_id": discord_target_channel_id,
            "ping_role_id":  ping_role_id,
            "ping_user_ids": ping_user_ids or [],
            "content_types": content_types,
            "channel_url":   f"https://www.youtube.com/channel/{channel_id}",
        }


# ---------------------------------------------------------------------------
# Slash command registrations
# ---------------------------------------------------------------------------

def setup_commands(bot: commands.Bot, tracker: YouTubeTracker) -> None:

    # ── /trust group ──────────────────────────────────────────────────
    trust_group = app_commands.Group(name="trust", description="Manage trusted users.")

    @trust_group.command(name="add", description="Trust a user for administrative bot commands.")
    @is_trusted_or_owner()
    @app_commands.describe(user="User to trust in this server")
    async def trust_add(interaction: discord.Interaction, user: discord.Member) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True
            )
            return
        db.add_trusted_user(interaction.guild.id, user.id, interaction.user.id)
        await interaction.response.send_message(f"✅ {user.mention} is now trusted.", ephemeral=True)

    @trust_group.command(name="remove", description="Revoke a user's trusted status.")
    @is_trusted_or_owner()
    @app_commands.describe(user="User to remove from this server's trusted list")
    async def trust_remove(interaction: discord.Interaction, user: discord.Member) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True
            )
            return
        changed = db.remove_trusted_user(interaction.guild.id, user.id)
        await interaction.response.send_message(
            "✅ Trusted status removed." if changed else "User is not trusted.",
            ephemeral=True,
        )

    @trust_group.command(name="list", description="List trusted users in this server.")
    @is_trusted_or_owner()
    async def trust_list(interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True
            )
            return
        users = db.get_trusted_users(interaction.guild.id)
        embed = discord.Embed(
            title=f"🛡️ Trusted Users — {interaction.guild.name}",
            color=discord.Color.blurple(),
        )
        embed.description = (
            "No trusted users are configured."
            if not users
            else "\n".join(
                f"<@{row['user_id']}> — added by <@{row['added_by']}>"
                for row in users
            )[:4096]
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    bot.tree.add_command(trust_group)

    # ── /sync ─────────────────────────────────────────────────────────
    @bot.tree.command(name="sync", description="Sync the global slash command tree immediately.")
    @is_trusted_or_owner()
    async def sync_commands(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            synced = await bot.tree.sync()
            logger.info(
                "Manual sync by %s: %d command(s): %s",
                interaction.user, len(synced),
                ", ".join(f"/{c.name}" for c in synced),
            )
            await interaction.followup.send(
                f"✅ Synced **{len(synced)}** global slash command(s).", ephemeral=True
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Discord rejected the sync (HTTP {exc.status}). Try again later.",
                ephemeral=True,
            )
        except Exception as exc:
            logger.exception("Unexpected error during manual sync.")
            await interaction.followup.send(f"❌ Unexpected error: {exc}", ephemeral=True)

    # ── /about ────────────────────────────────────────────────────────
    @bot.tree.command(name="about", description="Learn about the bot and view live public statistics.")
    async def about(interaction: discord.Interaction) -> None:
        started_at = getattr(interaction.client, "bot_started_at", None)
        uptime  = _format_uptime(started_at)
        latency = (
            f"{round(interaction.client.latency * 1000)} ms"
            if interaction.client.latency >= 0 else "Unavailable"
        )
        feed_count  = db.count_yt_feeds()
        webhook_url = getattr(config, "WEBHOOK_URL", None) or ""
        push_status = "🟢 WebSub active" if webhook_url else "🟡 RSS fallback only"
        embed = discord.Embed(
            title="🤖 Discord Notification Bot",
            description=(
                "A production-focused Discord bot for YouTube feed routing "
                "and full categorized server audit logging."
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="✨ Mission", value="Deliver reliable YouTube notifications while preserving detailed, organized server activity history.", inline=False)
        embed.add_field(name="📺 YouTube Routing", value=f"Multi-content monitoring with per-server destinations.\n{push_status}", inline=True)
        embed.add_field(name="📁 Audit Logging", value="8 dedicated channels covering chat, members, profiles, roles, channels, server, voice, and moderation.", inline=True)
        embed.add_field(
            name="📊 Live Stats",
            value=f"Servers: **{len(interaction.client.guilds)}**\nMonitored feeds: **{feed_count}**\nUptime: **{uptime}**\nGateway latency: **{latency}**",
            inline=False,
        )
        app_id = interaction.client.user.id if interaction.client.user else 0
        invite   = f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot%20applications.commands&permissions=2147601408"
        repo_url = "https://github.com/GujjuMui/Discord-Notification-Bot"
        embed.add_field(
            name="🔗 Quick Links",
            value=f"[Support]({repo_url}/issues) • [Invite]({invite}) • [GitHub]({repo_url}) • [Docs]({repo_url}#readme)",
            inline=False,
        )
        embed.set_footer(text="v3.0.0 • Developed / powered by GujjuMui")
        await interaction.response.send_message(embed=embed)

    # ── /help ─────────────────────────────────────────────────────────
    @bot.tree.command(name="help", description="Open the interactive public command guide.")
    async def help_menu(interaction: discord.Interaction) -> None:
        admin_view = await user_is_authorized(interaction)
        embed = discord.Embed(
            title="📖 Discord Notification Bot Help",
            description=(
                "Choose a category below. This menu is user-scoped, so multiple users "
                "can use /help at the same time without affecting each other.\n\n"
                "🔒 Admin/Trusted badges mark restricted administrative features."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Quick Start",
            value="1. /about → overview\n2. /add_yt → add a YouTube route\n3. /setup_logs → configure audit logging",
            inline=False,
        )
        await interaction.response.send_message(
            embed=embed, view=HelpView(interaction.user.id, admin_view)
        )

    # ── /botstatus ────────────────────────────────────────────────────
    @bot.tree.command(name="botstatus", description="View the live bot health dashboard.")
    @is_trusted_or_owner()
    async def botstatus(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        process    = psutil.Process()
        memory_mb  = process.memory_info().rss / (1024 * 1024)
        cpu_percent = psutil.cpu_percent(interval=None)
        latency_ms = interaction.client.latency * 1000
        try:
            db_messages  = db.count_cached_messages()
            feed_count   = db.count_yt_feeds()
            db_size_mb   = db.database_size_bytes() / (1024 * 1024)
            db_status    = "🟢 Connected"
        except Exception:
            db_messages = feed_count = 0
            db_size_mb  = 0
            db_status   = "🔴 Error"
            logger.exception("Health dashboard DB check failed.")

        rss_status = "🟡 No monitored feed configured"
        if tracker:
            monitored = db.get_yt_monitored_channels()
            if monitored:
                try:
                    await tracker.fetch_feed(monitored[0]["yt_channel_id"])
                    rss_status = "🟢 Reachable"
                except Exception as exc:
                    logger.warning("RSS health check failed: %s", exc)
                    rss_status = "🔴 Unreachable"

        webhook_url = getattr(config, "WEBHOOK_URL", None) or ""
        push_status = "🟢 WebSub active" if webhook_url else "🟡 RSS fallback only"
        gateway_status = "🟢 Connected" if interaction.client.is_ready() else "🔴 Disconnected"

        embed = discord.Embed(
            title="📊 System Health Dashboard",
            description="Live operational health for the bot process.",
            color=(
                discord.Color.green()
                if gateway_status.startswith("🟢") and rss_status.startswith("🟢")
                else discord.Color.orange()
            ),
        )
        embed.add_field(
            name="Discord",
            value=f"Gateway: **{gateway_status}**\nLatency: **{round(latency_ms)} ms**",
            inline=True,
        )
        embed.add_field(
            name="Runtime",
            value=(
                f"Uptime: **{_format_uptime(getattr(interaction.client, 'bot_started_at', None))}**\n"
                f"RAM: **{memory_mb:.1f} MB**\nCPU: **{cpu_percent:.1f}%**"
            ),
            inline=True,
        )
        embed.add_field(
            name="SQLite",
            value=(
                f"Status: **{db_status}**\nMessages: **{db_messages:,}**\n"
                f"YT feeds: **{feed_count:,}**\nSize: **{db_size_mb:.2f} MB**"
            ),
            inline=False,
        )
        embed.add_field(
            name="External APIs",
            value=f"YouTube RSS: **{rss_status}**\nWebSub push: **{push_status}**",
            inline=False,
        )
        embed.set_footer(text="Admin / Trusted / Owner only")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /setup_logs ───────────────────────────────────────────────────
    @bot.tree.command(name="setup_logs", description="Create or map categorized server audit log channels.")
    @is_trusted_or_owner()
    @app_commands.describe(
        auto_create="Create the 📁 SERVER LOGS category and all 8 log channels automatically.",
        type="For manual mapping: chat/member/profile/role/channel/server/voice/mod.",
        channel="Existing text channel to use for the selected log type.",
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name="chat",    value="chat"),
            app_commands.Choice(name="member",  value="member"),
            app_commands.Choice(name="profile", value="profile"),
            app_commands.Choice(name="role",    value="role"),
            app_commands.Choice(name="channel", value="channel"),
            app_commands.Choice(name="server",  value="server"),
            app_commands.Choice(name="voice",   value="voice"),
            app_commands.Choice(name="mod",     value="mod"),
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
                "This command can only be used inside a server.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        server_logger = interaction.client.get_cog("ServerLogger")
        if server_logger is None:
            await interaction.followup.send("❌ Server logger is not loaded.", ephemeral=True)
            return
        try:
            result = await server_logger.configure_logs(
                interaction.guild,
                auto_create=auto_create,
                log_type=type.value if type else None,
                channel=channel,
            )
        except (ValueError, discord.Forbidden, discord.HTTPException, RuntimeError) as exc:
            await interaction.followup.send(
                f"❌ Could not configure server logs: {exc}", ephemeral=True
            )
            return
        if auto_create:
            mentions = "\n".join(
                f"• **{key}** → {value.mention}" for key, value in result.items()
            )
            await interaction.followup.send(
                "✅ **Server logging configured.**\n"
                "Created/linked the categorized logging channels:\n" + mentions,
                ephemeral=True,
            )
        else:
            mapped = next(iter(result.values()))
            await interaction.followup.send(
                f"✅ **{type.value if type else 'log'}** logs will now go to {mapped.mention}.",
                ephemeral=True,
            )

    # ── /add_yt ───────────────────────────────────────────────────────
    @bot.tree.command(
        name="add_yt",
        description="Register a YouTube channel, destination, role, and content filter.",
    )
    @is_trusted_or_owner()
    @app_commands.describe(
        url="YouTube channel URL or @handle URL",
        target_channel="Discord channel where notifications will be posted",
        role="Optional role to ping, including @everyone or @here.",
        target_user="Optional user to ping for matching activity",
        types="Content types: all, videos, shorts, live, or community",
        channel_name="Optional display name if the channel name cannot be detected automatically",
    )
    @app_commands.choices(
        types=[
            app_commands.Choice(name="all",       value="all"),
            app_commands.Choice(name="videos",    value="videos"),
            app_commands.Choice(name="shorts",    value="shorts"),
            app_commands.Choice(name="live",      value="live"),
            app_commands.Choice(name="community", value="community"),
        ]
    )
    async def add_yt(
        interaction: discord.Interaction,
        url: str,
        target_channel: discord.TextChannel,
        role: Optional[discord.Role] = None,
        target_user: Optional[discord.Member] = None,
        types: Optional[app_commands.Choice[str]] = None,
        channel_name: Optional[str] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        selected_types = types.value if types else "all"
        try:
            result = await tracker.add_channel(
                interaction.guild.id,
                url,
                target_channel.id,
                role.id if role else None,
                [target_user.id] if target_user else [],
                selected_types,
                channel_name_override=channel_name,
            )
            if role:
                role_display = "@everyone" if role.is_default() else role.mention
                ping_text = f" and pings {role_display}."
            else:
                ping_text = "."

            if result["already_tracked"]:
                message = (
                    f"**{result['channel_name']}** is already subscribed to "
                    f"{target_channel.mention}{ping_text}"
                )
            else:
                message = (
                    f"✅ Subscribed **{result['channel_name']}** → "
                    f"Notifications will post in {target_channel.mention}{ping_text}"
                )

            webhook_url = getattr(config, "WEBHOOK_URL", None) or ""
            push_note   = (
                "\n\n🚀 **WebSub push subscription requested** — Google will deliver "
                "new uploads within seconds once verified."
                if webhook_url
                else "\n\n📡 **RSS fallback mode** — add `WEBHOOK_URL` to Railway for instant push."
            )

            embed = discord.Embed(
                title="YouTube Subscription",
                description=message + push_note,
                color=discord.Color.green(),
            )
            embed.add_field(name="Content Filter", value=f"**{selected_types}**", inline=True)
            channel_url = result.get("channel_url") or f"https://www.youtube.com/channel/{result['channel_id']}"
            embed.add_field(name="YouTube Channel", value=f"[Open channel]({channel_url})", inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("add_yt failed for guild %s", interaction.guild.id)
            await interaction.followup.send(
                f"Could not add YouTube channel: {exc}", ephemeral=True
            )

    # ── /remove_yt ────────────────────────────────────────────────────
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
                "This command can only be used inside a server.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        value = url_or_id.strip()
        match = CHANNEL_ID_RE.search(value)
        if not match and value.startswith(("http://", "https://", "youtube.com", "www.youtube.com", "@")):
            try:
                channel_id = await tracker.resolve_channel_id(value)
            except Exception as exc:
                await interaction.followup.send(
                    f"Could not resolve that YouTube URL: {exc}", ephemeral=True
                )
                return
        else:
            channel_id = match.group(0) if match else value

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

    # ── /list_yt ──────────────────────────────────────────────────────
    @bot.tree.command(name="list_yt", description="List tracked YouTube sources, destinations, and content filters.")
    @is_trusted_or_owner()
    async def list_yt(interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True
            )
            return

        # Defer first — DB read + embed build can exceed 3s on busy instances
        await interaction.response.defer(ephemeral=True)

        channels = db.get_yt_monitored_channels(interaction.guild.id)
        if not channels:
            await interaction.followup.send(
                "No YouTube channels are currently tracked in this server.", ephemeral=True
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
                target_id    = int(item["discord_target_channel_id"])
                target       = f"<#{target_id}>" if target_id > 0 else "Not configured"
                role_id_raw  = int(item["ping_role_id"]) if item.get("ping_role_id") else None
                ping_role    = (
                    "@everyone" if role_id_raw == int(interaction.guild.id)
                    else f"<@&{role_id_raw}>" if role_id_raw
                    else "None"
                )
                ping_users   = db.decode_yt_ping_users(item.get("ping_user_ids"))
                ping_user_text = ", ".join(f"<@{uid}>" for uid in ping_users) or "None"
                filters      = item.get("content_types") or "all"
                lease_expires = item.get("lease_expires_at")
                push_icon    = "🟢" if lease_expires else "🟡"
                destinations.append(
                    f"• [{item['yt_channel_name']}]({item['yt_channel_url']}) ➔ {target} "
                    f"(Filters: {filters} | Role: {ping_role} | Users: {ping_user_text} | Push: {push_icon})"
                )
            embed.add_field(
                name=first["yt_channel_name"][:256],
                value="\n".join(destinations)[:1024],
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /test_yt ──────────────────────────────────────────────────────
    @bot.tree.command(name="test_yt", description="Send a realistic YouTube notification preview.")
    @is_trusted_or_owner()
    @app_commands.describe(
        target_channel="Discord channel where the test notification will be posted",
        role="Optional role to ping in the test notification",
        target_user="Optional user to ping in the test notification",
    )
    async def test_yt(
        interaction: discord.Interaction,
        target_channel: discord.TextChannel,
        role: Optional[discord.Role] = None,
        target_user: Optional[discord.Member] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(
                "This command can only be used inside a server.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="[TEST PREVIEW] 🎥 MrBeast uploaded a new video!",
            description="A realistic preview of the YouTube upload notification.",
            color=discord.Color(0xFF0000),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name="YouTube",
            icon_url="https://www.youtube.com/s/desktop/e4d15d2c/img/favicon_144x144.png",
        )
        embed.add_field(name="Video Title",  value="**I Survived 7 Days In An Abandoned City**", inline=False)
        embed.add_field(name="Channel",      value="**MrBeast**", inline=True)
        embed.add_field(name="Duration",     value="24:18", inline=True)
        embed.add_field(name="Published",    value="Just now", inline=True)
        embed.set_image(url="https://img.youtube.com/vi/dQw4w9WgXcQ/hqdefault.jpg")
        embed.set_footer(
            text="YouTube Notification Bot • TEST PREVIEW",
            icon_url="https://www.youtube.com/s/desktop/e4d15d2c/img/favicon_144x144.png",
        )

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Watch on YouTube",
                style=discord.ButtonStyle.link,
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            )
        )

        content_parts = []
        if target_user:
            content_parts.append(target_user.mention)
        if role:
            content_parts.append("@everyone" if role.is_default() else role.mention)
        content = " ".join(content_parts)

        try:
            await target_channel.send(
                content=content or None,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(
                    everyone=True, roles=bool(role), users=bool(target_user), replied_user=True
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.followup.send(
                f"❌ Could not send the test notification: {exc}", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ Test notification sent to {target_channel.mention}"
            + (
                " with @everyone ping." if role and role.is_default()
                else f" with {role.mention} ping." if role
                else "."
            ),
            ephemeral=True,
        )

    # ── /ytinfo ───────────────────────────────────────────────────────
    @bot.tree.command(name="ytinfo", description="Resolve a YouTube URL to its channel ID and feed stats.")
    @app_commands.describe(url="YouTube channel URL or @handle URL")
    async def ytinfo(interaction: discord.Interaction, url: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            channel_id = await tracker.resolve_channel_id(url)
            videos     = await tracker.fetch_feed(channel_id)
            name       = next((v["channel_name"] for v in videos if v["channel_name"]), channel_id)
            webhook_url = getattr(config, "WEBHOOK_URL", None) or ""
            push_line   = (
                f"\nWebSub push: 🟢 configured (`{webhook_url[:60]}...`)"
                if webhook_url else "\nWebSub push: 🟡 not configured (RSS fallback only)"
            )
            await interaction.followup.send(
                f"**{name}**\nChannel ID: `{channel_id}`\nRSS entries: {len(videos)}{push_line}",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(f"Could not resolve channel: {exc}", ephemeral=True)
