"""Comprehensive, guild-scoped Discord server audit logging."""

from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import db

logger = logging.getLogger(__name__)

AUDIT_LOG_PATH = config.PROJECT_ROOT / "logs" / "audit_history.log"
AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
_handler = RotatingFileHandler(
    AUDIT_LOG_PATH,
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
)
if not any(isinstance(item, RotatingFileHandler) for item in logger.handlers):
    logger.addHandler(_handler)


LOG_CHANNEL_NAMES = {
    "chat": "chat-logs",
    "member": "member-logs",
    "profile": "profile-logs",
    "role": "role-logs",
    "channel": "channel-logs",
    "server": "server-logs",
    "voice": "voice-logs",
    "mod": "moderation-logs",
}

LOG_COLUMNS = {
    "chat": "chat_log_id",
    "member": "member_log_id",
    "profile": "profile_log_id",
    "role": "role_log_id",
    "channel": "channel_log_id",
    "server": "server_log_id",
    "voice": "voice_log_id",
    "mod": "mod_log_id",
}


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


class ServerLogger(commands.Cog):
    """Capture server events, persist message history, and route events by type."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._history_cache_loaded: set[int] = set()
        self._missing_log_alerted: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------
    # Setup / routing
    # ------------------------------------------------------------------
    async def cog_load(self) -> None:
        self.message_retention_cleanup.start()

    async def cog_unload(self) -> None:
        self.message_retention_cleanup.cancel()

    @tasks.loop(hours=24)
    async def message_retention_cleanup(self) -> None:
        try:
            removed = db.cleanup_old_messages(days=30)
            logger.info("Message retention cleanup removed %d cached messages older than 30 days.", removed)
        except Exception:
            logger.exception("Daily message retention cleanup failed.")

    @message_retention_cleanup.before_loop
    async def before_message_retention_cleanup(self) -> None:
        await self.bot.wait_until_ready()



    async def configure_logs(
        self,
        guild: discord.Guild,
        *,
        auto_create: bool = True,
        log_type: Optional[str] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> dict[str, discord.TextChannel]:
        """Create/map the final categorized logging channels and persist their IDs."""
        me = guild.me
        if me is None:
            raise RuntimeError("The bot is not ready in this server.")

        category = discord.utils.find(
            lambda c: c.name == "📁 SERVER LOGS",
            guild.categories,
        )

        if auto_create:
            if category is None:
                category = await guild.create_category(
                    "📁 SERVER LOGS",
                    reason="Server audit logging setup",
                )

            result: dict[str, discord.TextChannel] = {}
            for key, name in LOG_CHANNEL_NAMES.items():
                existing = discord.utils.find(
                    lambda c, n=name: c.name == n and c.category_id == category.id,
                    guild.text_channels,
                )
                if existing is None:
                    existing = await guild.create_text_channel(
                        name,
                        category=category,
                        reason="Server audit logging setup",
                    )
                result[key] = existing

            db.set_guild_log_channels(
                guild.id,
                category_id=category.id,
                chat_log_id=result["chat"].id,
                member_log_id=result["member"].id,
                profile_log_id=result["profile"].id,
                role_log_id=result["role"].id,
                channel_log_id=result["channel"].id,
                server_log_id=result["server"].id,
                voice_log_id=result["voice"].id,
                mod_log_id=result["mod"].id,
                enabled=True,
            )
            return result

        if log_type is None or channel is None:
            raise ValueError(
                "Manual mapping requires both type and channel, for example "
                "type:chat channel:#chat-logs."
            )

        if log_type not in LOG_COLUMNS:
            raise ValueError(
                "Invalid log type. Use chat, member, profile, role, channel, "
                "server, voice, or mod."
            )

        current = db.get_guild_log_channels(guild.id) or {}
        values = {
            key: current.get(key)
            for key in (
                "category_id",
                "chat_log_id",
                "member_log_id",
                "profile_log_id",
                "role_log_id",
                "channel_log_id",
                "server_log_id",
                "voice_log_id",
                "mod_log_id",
            )
        }
        values[LOG_COLUMNS[log_type]] = channel.id
        db.set_guild_log_channels(guild.id, enabled=True, **values)

        return {log_type: channel}

    async def register_commands(self) -> None:
        """Register the setup slash command once the cog is loaded."""
        # Kept as a method so main.py can explicitly register this cog's command.
        return None

    async def _resolve_log_channel(
        self, guild_id: int, log_type: str
    ) -> Optional[discord.abc.Messageable]:
        settings = db.get_guild_log_channels(guild_id)
        if settings and not settings.get("enabled", 1):
            return None

        candidate_ids: list[int] = []
        if settings:
            specific = settings.get(LOG_COLUMNS.get(log_type, ""))
            if specific:
                candidate_ids.append(int(specific))

            # General fallback: moderation log, then another configured log channel.
            for key in (
                "mod_log_id",
                "chat_log_id",
                "member_log_id",
                "profile_log_id",
                "role_log_id",
                "channel_log_id",
                "server_log_id",
                "voice_log_id",
            ):
                value = settings.get(key)
                if value and int(value) not in candidate_ids:
                    candidate_ids.append(int(value))

        for channel_id in candidate_ids:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    logger.error("Configured audit log channel %s is unavailable: %s", channel_id, exc)
                    await self._notify_missing_log_channel(guild_id, channel_id)
                    continue
            if hasattr(channel, "send"):
                self._missing_log_alerted.discard((guild_id, channel_id))
                return channel

        await self._notify_missing_log_channel(guild_id, 0)
        return None

    async def _notify_missing_log_channel(self, guild_id: int, channel_id: int) -> None:
        key = (guild_id, channel_id)
        if key in self._missing_log_alerted:
            return
        self._missing_log_alerted.add(key)

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        target = f"<#{channel_id}>" if channel_id else "the configured audit log channels"
        message = (
            f"⚠️ I could not access {target}. Server audit logging will use another "
            "configured log channel when possible. Run /setup_logs if the logging "
            "channels need to be recreated or remapped."
        )
        try:
            owner = guild.owner or await self.bot.fetch_user(guild.owner_id)
            if owner:
                await owner.send(message)
                return
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Could not DM guild owner about missing audit log channel %s.", channel_id)

        fallback = guild.system_channel
        if fallback and hasattr(fallback, "send"):
            try:
                await fallback.send(message)
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Could not send missing audit log alert in guild %s.", guild_id)


    @staticmethod
    def _visual_color(title: str, log_type: str, fallback: Optional[discord.Color]) -> discord.Color:
        value = f"{title} {log_type}".lower()
        if any(term in value for term in ("deleted", "delet", "left", "removed", "banned", "purge")):
            return discord.Color(0xFF3B30)
        if any(term in value for term in ("joined", "created", "unbanned", "unban")):
            return discord.Color(0x34C759)
        if any(term in value for term in ("boost", "avatar", "pfp", "server icon", "banner")):
            return discord.Color(0xAF52DE)
        if any(term in value for term in ("edited", "edit", "moved", "nickname", "renamed")):
            return discord.Color(0x007AFF)
        if any(term in value for term in ("role", "permission", "timeout", "override", "updated")):
            return discord.Color(0xFF9500)
        return fallback or discord.Color(0x007AFF)

    @staticmethod
    def _extract_id(value: Any) -> Optional[int]:
        match = re.search(r"(?<!\d)(\d{15,21})(?!\d)", str(value or ""))
        return int(match.group(1)) if match else None

    @classmethod
    def _compact_identity(cls, label: str, value: Any) -> str:
        raw = str(value or "Unknown / unavailable").strip()
        if raw == "Unknown / unavailable":
            return f"{label}: Unknown"
        if raw.startswith("<@") or raw.startswith("<#"):
            return f"{label}: {raw}"
        target_id = cls._extract_id(raw)
        if target_id is None:
            return f"{label}: {raw}"
        if label in {"Author", "Executor", "By"}:
            return f"{label}: <@{target_id}> (" + chr(96) + str(target_id) + chr(96) + ")"
        return f"{label}: {raw} (" + chr(96) + str(target_id) + chr(96) + ")"

    @staticmethod
    def _compact_value(value: Any, limit: int = 900) -> str:
        text = str(value or "-").replace("\r", " ").replace("\n", " ↵ ")
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def _compact_change(before: Any, after: Any) -> str:
        old = str(before or "None").replace("\n", " ")
        new = str(after or "None").replace("\n", " ")
        tick = chr(96)
        return f"• Old: " + tick + old + tick + " ➔ New: " + tick + new + tick

    async def _send(
        self,
        guild_id: int,
        log_type: str,
        title: str,
        description: str = "",
        color: Optional[discord.Color] = None,
        fields: Optional[list[tuple[str, str, bool]]] = None,
        file: Optional[discord.File] = None,
        thumbnail: Optional[str] = None,
        image: Optional[str] = None,
    ) -> bool:
        channel = await self._resolve_log_channel(guild_id, log_type)
        if channel is None:
            return False

        guild = self.bot.get_guild(guild_id)
        source_fields = list(fields or [])

        metadata: list[str] = []
        retained: list[tuple[str, str, bool]] = []
        consumed: set[int] = set()

        author_value = None
        target_value = None
        channel_value = None
        executor_value = None

        for index, (name, value, _inline) in enumerate(source_fields):
            key = str(name).strip().lower()
            if key == "author" and author_value is None:
                author_value = value
                consumed.add(index)
            elif key in {"user", "target"} and target_value is None:
                target_value = value
                consumed.add(index)
            elif key == "target" and target_value is None:
                target_value = value
                consumed.add(index)
            elif key == "channel" and channel_value is None:
                channel_value = value
                consumed.add(index)
            elif key in {"executor", "deleted by", "purged by"} and executor_value is None:
                executor_value = value
                consumed.add(index)

        if author_value is not None:
            metadata.append(self._compact_identity("Author", author_value))
        if target_value is not None:
            metadata.append(self._compact_identity("Target", target_value))
        if channel_value is not None:
            metadata.append(self._compact_identity("Channel", channel_value))
        if executor_value is not None:
            metadata.append(self._compact_identity("By", executor_value))

        paired_before: Optional[tuple[int, str]] = None
        paired_after: Optional[tuple[int, str]] = None
        for index, (name, value, _inline) in enumerate(source_fields):
            key = str(name).strip().lower()
            if index in consumed:
                continue
            if key in {"before", "old"} and paired_before is None:
                paired_before = (index, str(value))
            elif key in {"after", "new"} and paired_after is None:
                paired_after = (index, str(value))

        if paired_before and paired_after:
            retained.append(("Change", self._compact_change(paired_before[1], paired_after[1]), False))
            consumed.update({paired_before[0], paired_after[0]})

        for index, (name, value, inline) in enumerate(source_fields):
            if index in consumed:
                continue
            compact = self._compact_value(value)
            retained.append((str(name)[:256], compact, bool(inline or len(compact) <= 320)))

        embed = discord.Embed(
            title=title,
            description="",
            color=self._visual_color(title, log_type, color),
            timestamp=datetime.now(timezone.utc),
        )

        if guild:
            guild_icon = self._avatar_url(guild.icon)
            if guild_icon:
                embed.set_author(name=guild.name, icon_url=guild_icon)
            else:
                embed.set_author(name=guild.name)

        if metadata:
            embed.description = "  |  ".join(metadata)[:4096]

        if description:
            content = description.replace(chr(96) * 3, "").strip()
            if len(content) > 1000:
                content = content[:997] + "…"
            quoted = "\n".join(f"> {line}" if line else ">" for line in content.splitlines())
            embed.add_field(name="Content", value=quoted[:1024], inline=False)

        for name, value, inline in retained:
            embed.add_field(
                name=str(name)[:256],
                value=str(value)[:1024] if value else "-",
                inline=inline,
            )

        target_id = self._extract_id(target_value or author_value or description)
        action_ts = int(datetime.now(timezone.utc).timestamp())
        footer_parts = []
        if target_id:
            footer_parts.append(f"ID: {target_id}")
        footer_parts.extend((log_type.upper(), f"<t:{action_ts}:t>"))
        embed.set_footer(text=" • ".join(footer_parts))

        if target_id and guild:
            member = guild.get_member(target_id)
            if member:
                embed.set_thumbnail(url=self._avatar_url(member.display_avatar))
            else:
                try:
                    user = await self.bot.fetch_user(target_id)
                    avatar = self._avatar_url(user.display_avatar)
                    if avatar:
                        embed.set_thumbnail(url=avatar)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        if thumbnail and not getattr(embed.thumbnail, "url", None):
            embed.set_thumbnail(url=thumbnail)
        if image:
            embed.set_image(url=image)

        try:
            if file is None:
                await channel.send(embed=embed)
            else:
                await channel.send(embed=embed, file=file)
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not send %s audit event for guild %s", log_type, guild_id)
            return False


    async def _executor(
        self,
        guild: Optional[discord.Guild],
        actions: tuple[Any, ...],
        target_id: Optional[int] = None,
        limit: int = 20,
    ) -> Optional[discord.abc.User]:
        if guild is None or not guild.me or not guild.me.guild_permissions.view_audit_log:
            return None

        valid_actions = tuple(action for action in actions if action is not None)
        if not valid_actions:
            return None

        try:
            async for entry in guild.audit_logs(limit=limit):
                if entry.action not in valid_actions:
                    continue
                entry_target = getattr(entry.target, "id", None)
                if target_id is not None and entry_target not in (None, target_id):
                    continue
                if abs(
                    (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                ) <= 20:
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Could not read audit log for guild %s", guild.id)
        return None

    @staticmethod
    def _audit_action(name: str) -> Any:
        return getattr(discord.AuditLogAction, name, None)

    async def _executor_tag(
        self,
        guild: Optional[discord.Guild],
        action_names: tuple[str, ...],
        target_id: Optional[int] = None,
    ) -> str:
        actions = tuple(self._audit_action(name) for name in action_names)
        executor = await self._executor(guild, actions, target_id)
        return str(executor) if executor else "Unknown / unavailable"

    def _event(self, name: str, data: dict[str, Any]) -> None:
        payload = {
            "event": name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        logger.info("AUDIT_EVENT %s", json.dumps(payload, ensure_ascii=False, default=str))

    @staticmethod
    def _clip(value: Optional[str], limit: int = 1024) -> str:
        value = value or "[no text content]"
        return value if len(value) <= limit else value[: limit - 3] + "..."

    @staticmethod
    def _avatar_url(value: Any) -> Optional[str]:
        try:
            return str(value.url) if value else None
        except AttributeError:
            return None

    @staticmethod
    def _message_record(message: discord.Message) -> dict[str, Any]:
        return {
            "message_id": message.id,
            "guild_id": message.guild.id,
            "channel_id": message.channel.id,
            "author_id": message.author.id,
            "author_tag": str(message.author),
            "content": message.content or "",
            "attachments": [attachment.url for attachment in message.attachments],
            "timestamp": message.created_at.astimezone(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Message persistence / deletion
    # ------------------------------------------------------------------

    async def _warm_message_cache(self, guild: discord.Guild) -> None:
        if guild.id in self._history_cache_loaded:
            return

        self._history_cache_loaded.add(guild.id)
        total = 0
        for channel in guild.text_channels:
            me = guild.me
            if me is None:
                continue
            permissions = channel.permissions_for(me)
            if not (permissions.view_channel and permissions.read_message_history):
                continue
            try:
                async for message in channel.history(limit=100):
                    record = self._message_record(message)
                    db.cache_message(**record)
                    total += 1
            except (discord.Forbidden, discord.HTTPException):
                logger.warning(
                    "Could not warm message cache for #%s in guild %s",
                    channel.name,
                    guild.id,
                )
        logger.debug("Message cache warm-up for %s: %s messages", guild.name, total)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self._warm_message_cache(guild)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return
        db.cache_message(**self._message_record(message))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if not payload.guild_id:
            return

        data = db.mark_message_deleted(payload.message_id)
        guild = self.bot.get_guild(payload.guild_id)
        who = await self._executor_tag(
            guild, ("message_delete",), payload.message_id
        )

        if data:
            attachments = "\n".join(data["attachments"]) or "None"
            await self._send(
                payload.guild_id,
                "chat",
                "🗑️ Message Deleted",
                self._clip(data["content"]),
                discord.Color.orange(),
                [
                    ("Author", f"{data['author_tag']} ({data.get('author_id') or 'unknown'})", False),
                    ("Channel", f"<#{data['channel_id']}>", True),
                    ("Sent", data["timestamp"], True),
                    ("Deleted by", who, True),
                    ("Attachments", attachments, False),
                ],
            )
            self._event("message_delete", {**data, "executor": who})
        else:
            await self._send(
                payload.guild_id,
                "chat",
                "🗑️ Message Deleted",
                "Message content was not present in the persistent SQLite cache.",
                discord.Color.orange(),
                [
                    ("Message ID", str(payload.message_id), True),
                    ("Channel", f"<#{payload.channel_id}>", True),
                    ("Deleted by", who, True),
                ],
            )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        if not payload.guild_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        who = await self._executor_tag(
            guild, ("message_bulk_delete", "message_delete")
        )
        cached = db.get_cached_messages(
            list(payload.message_ids), guild_id=payload.guild_id
        )

        lines = []
        for item in cached:
            attachments = " | ".join(item["attachments"]) or "None"
            lines.append(
                f"[{item['timestamp']}] {item['author_tag']} "
                f"({item.get('author_id') or 'unknown'}) | "
                f"#{item['channel_id']} | {item['content'] or '[no text content]'} | "
                f"Attachments: {attachments}"
            )

        if len(payload.message_ids) > 10:
            body = "\n".join(lines) or "No cached message content."
            filename = (
                f"purge_{payload.channel_id}_"
                f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.txt"
            )
            file = discord.File(io.BytesIO(body.encode("utf-8")), filename=filename)
            preview = (
                f"{len(payload.message_ids)} messages purged. "
                f"{len(cached)} messages were recovered from SQLite. "
                f"Full content is attached."
            )
        else:
            file = None
            preview = "\n".join(lines)[:3500] or "No cached message content."

        await self._send(
            payload.guild_id,
            "chat",
            "🧹 Bulk Message Purge",
            preview,
            discord.Color.red(),
            [
                ("Messages purged", str(len(payload.message_ids)), True),
                ("Recovered from SQLite", str(len(cached)), True),
                ("Missing from SQLite", str(len(payload.message_ids) - len(cached)), True),
                ("Purged by", who, False),
                ("Source channel", f"<#{payload.channel_id}>", True),
            ],
            file=file,
        )

        # Keep the records permanently; marking them deleted preserves the evidence.
        for item in cached:
            db.mark_message_deleted(item["message_id"])

        self._event(
            "bulk_message_delete",
            {
                "guild_id": payload.guild_id,
                "channel_id": payload.channel_id,
                "message_ids": list(payload.message_ids),
                "cached_count": len(cached),
                "missing_count": len(payload.message_ids) - len(cached),
                "executor": who,
                "messages": cached,
            },
        )

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        if not before.guild or before.content == after.content:
            return

        record = self._message_record(after)
        db.cache_message(**record)
        await self._send(
            before.guild.id,
            "chat",
            "✏️ Message Edited",
            "A message was edited.",
            discord.Color.gold(),
            [
                ("Author", f"{before.author} ({before.author.id})", False),
                ("Channel", before.channel.mention, True),
                ("Before", self._clip(before.content), False),
                ("After", self._clip(after.content), False),
                (
                    "Message",
                    f"https://discord.com/channels/{before.guild.id}/{before.channel.id}/{before.id}",
                    False,
                ),
            ],
        )
        self._event(
            "message_edit",
            {
                "guild_id": before.guild.id,
                "channel_id": before.channel.id,
                "message_id": before.id,
                "author_id": before.author.id,
                "author_tag": str(before.author),
                "before": before.content,
                "after": after.content,
            },
        )

    # ------------------------------------------------------------------
    # Members / profile / moderation
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        account_age = datetime.now(timezone.utc) - member.created_at
        await self._send(
            member.guild.id,
            "member",
            "📥 Member Joined",
            f"{member.mention} joined the server.",
            discord.Color.green(),
            [
                ("User", f"{member} ({member.id})", False),
                ("Account created", member.created_at.isoformat(), True),
                ("Account age", str(account_age).split(".")[0], True),
            ],
        )
        self._event("member_join", {"guild_id": member.guild.id, "user_id": member.id, "user": str(member)})

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        who = await self._executor_tag(
            member.guild, ("kick", "ban"), member.id
        )
        await self._send(
            member.guild.id,
            "member",
            "📤 Member Left / Removed",
            f"{member} ({member.id}) left or was removed.",
            discord.Color.red(),
            [("Executor", who, False)],
        )
        self._event("member_remove", {"guild_id": member.guild.id, "user_id": member.id, "user": str(member), "executor": who})

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        who = await self._executor_tag(guild, ("ban",), user.id)
        await self._send(
            guild.id,
            "member",
            "🔨 Member Banned",
            f"{user} ({user.id}) was banned.",
            discord.Color.dark_red(),
            [("Executor", who, False)],
        )
        self._event("member_ban", {"guild_id": guild.id, "user_id": user.id, "user": str(user), "executor": who})

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        who = await self._executor_tag(guild, ("unban",), user.id)
        await self._send(
            guild.id,
            "member",
            "♻️ Member Unbanned",
            f"{user} ({user.id}) was unbanned.",
            discord.Color.green(),
            [("Executor", who, False)],
        )
        self._event("member_unban", {"guild_id": guild.id, "user_id": user.id, "user": str(user), "executor": who})

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        if before.roles != after.roles:
            added = [r for r in after.roles if r not in before.roles and not r.is_default()]
            removed = [r for r in before.roles if r not in after.roles and not r.is_default()]
            if added or removed:
                who = await self._executor_tag(
                    after.guild, ("member_role_update", "member_update"), after.id
                )
                await self._send(
                    after.guild.id,
                    "role",
                    "🎭 Member Roles Changed",
                    f"{after} ({after.id})",
                    discord.Color.blurple(),
                    [
                        ("Added", ", ".join(r.name for r in added) or "None", False),
                        ("Removed", ", ".join(r.name for r in removed) or "None", False),
                        ("Executor", who, False),
                    ],
                )

        before_timeout = getattr(
            before,
            "timed_out_until",
            getattr(before, "communication_disabled_until", None),
        )
        after_timeout = getattr(
            after,
            "timed_out_until",
            getattr(after, "communication_disabled_until", None),
        )
        if before_timeout != after_timeout:
            who = await self._executor_tag(after.guild, ("member_update",), after.id)
            await self._send(
                after.guild.id,
                "member",
                "⏱️ Member Timeout Updated",
                f"{after} ({after.id})",
                discord.Color.orange(),
                [
                    ("Before", str(before_timeout or "Not timed out"), True),
                    ("After", str(after_timeout or "Not timed out"), True),
                    ("Executor", who, False),
                ],
            )

        if before.nick != after.nick:
            who = await self._executor_tag(after.guild, ("member_update",), after.id)
            await self._send(
                after.guild.id,
                "profile",
                "🏷️ Server Nickname Changed",
                f"{after} ({after.id})",
                discord.Color.blurple(),
                [
                    ("Before", before.nick or "None", True),
                    ("After", after.nick or "None", True),
                    ("Executor", who, False),
                ],
            )

        before_avatar = self._avatar_url(before.avatar)
        after_avatar = self._avatar_url(after.avatar)
        if before_avatar != after_avatar:
            who = await self._executor_tag(after.guild, ("member_update",), after.id)
            await self._send(
                after.guild.id,
                "profile",
                "🖼️ Avatar / PFP Updated",
                f"{after} ({after.id})",
                discord.Color.blurple(),
                [
                    ("Old Avatar", before_avatar or "Default / unavailable", False),
                    ("New Avatar", after_avatar or "Default / unavailable", False),
                    ("Executor", who, False),
                ],
                thumbnail=after_avatar,
                image=before_avatar,
            )

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        if before.name == after.name and self._avatar_url(before.avatar) == self._avatar_url(after.avatar):
            return
        # User update is global; only log it for guilds where the user is currently a member.
        for guild in self.bot.guilds:
            member = guild.get_member(after.id)
            if member is None:
                continue
            fields = [
                ("Before username", before.name, True),
                ("After username", after.name, True),
                ("Old Avatar", self._avatar_url(before.avatar) or "Default / unavailable", False),
                ("New Avatar", self._avatar_url(after.avatar) or "Default / unavailable", False),
            ]
            await self._send(
                guild.id,
                "profile",
                "👤 Global Profile Updated",
                f"{after} ({after.id})",
                discord.Color.blurple(),
                fields,
                thumbnail=self._avatar_url(after.avatar),
                image=self._avatar_url(before.avatar),
            )

    # ------------------------------------------------------------------
    # Roles
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        who = await self._executor_tag(role.guild, ("role_create",), role.id)
        await self._send(role.guild.id, "role", "➕ Role Created", f"{role.name} ({role.id})", discord.Color.green(), [("Executor", who, False)])

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        who = await self._executor_tag(role.guild, ("role_delete",), role.id)
        await self._send(role.guild.id, "role", "➖ Role Deleted", f"{role.name} ({role.id})", discord.Color.red(), [("Executor", who, False)])

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        if (
            before.name == after.name
            and before.colour == after.colour
            and before.permissions == after.permissions
            and before.hoist == after.hoist
            and before.mentionable == after.mentionable
        ):
            return
        who = await self._executor_tag(after.guild, ("role_update",), after.id)
        await self._send(
            after.guild.id,
            "role",
            "🎨 Role Updated",
            f"{before.name} → {after.name}",
            discord.Color.blurple(),
            [
                ("Name", f"{before.name} → {after.name}", False),
                ("Color", f"{before.colour} → {after.colour}", True),
                ("Color HEX", f"{before.colour.value:#08x} → {after.colour.value:#08x}", True),
                ("Permissions", f"{before.permissions.value} → {after.permissions.value}", False),
                ("Executor", who, False),
            ],
        )

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        who = await self._executor_tag(channel.guild, ("channel_create",), channel.id)
        await self._send(
            channel.guild.id,
            "channel",
            "📁 Channel Created",
            f"{channel.mention if hasattr(channel, 'mention') else channel.name}",
            discord.Color.green(),
            [
                ("Name", channel.name, True),
                ("Type", str(channel.type), True),
                ("Category", getattr(channel.category, "name", "None"), True),
                ("Executor", who, False),
            ],
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        who = await self._executor_tag(channel.guild, ("channel_delete",), channel.id)
        await self._send(
            channel.guild.id,
            "channel",
            "🗑️ Channel Deleted",
            f"#{channel.name}",
            discord.Color.red(),
            [
                ("Channel ID", str(channel.id), True),
                ("Category", getattr(channel.category, "name", "None"), True),
                ("Executor", who, False),
            ],
        )

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        changes: list[str] = []
        if before.name != after.name:
            changes.append(f"Name: {before.name} → {after.name}")
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes.append("Topic changed")
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            changes.append(
                f"Slowmode: {getattr(before, 'slowmode_delay', 0)}s → "
                f"{getattr(after, 'slowmode_delay', 0)}s"
            )
        if getattr(before, "nsfw", None) != getattr(after, "nsfw", None):
            changes.append(f"NSFW: {getattr(before, 'nsfw', False)} → {getattr(after, 'nsfw', False)}")
        if getattr(before, "category_id", None) != getattr(after, "category_id", None):
            changes.append(
                f"Category: {getattr(before.category, 'name', 'None')} → "
                f"{getattr(after.category, 'name', 'None')}"
            )
        if getattr(before, "overwrites", None) != getattr(after, "overwrites", None):
            changes.append("Permission overwrites changed")

        if not changes:
            return

        who = await self._executor_tag(
            after.guild, ("channel_update",), after.id
        )
        await self._send(
            after.guild.id,
            "channel",
            "⚙️ Channel Updated",
            f"#{after.name}",
            discord.Color.gold(),
            [
                ("Changes", "\n".join(changes), False),
                ("Channel ID", str(after.id), True),
                ("Executor", who, False),
            ],
        )

    # ------------------------------------------------------------------
    # Server / governance
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        changes: list[str] = []
        if before.name != after.name:
            changes.append(f"Name: {before.name} → {after.name}")
        if self._avatar_url(before.icon) != self._avatar_url(after.icon):
            changes.append("Server icon updated")
        if self._avatar_url(before.banner) != self._avatar_url(after.banner):
            changes.append("Server banner updated")
        if before.premium_tier != after.premium_tier:
            changes.append(f"Boost tier: {before.premium_tier} → {after.premium_tier}")
        if before.premium_subscription_count != after.premium_subscription_count:
            changes.append(
                f"Boost count: {before.premium_subscription_count} → "
                f"{after.premium_subscription_count}"
            )
        if not changes:
            return

        who = await self._executor_tag(after, ("guild_update",), after.id)
        await self._send(
            after.id,
            "server",
            "🏠 Server Updated",
            "\n".join(changes),
            discord.Color.gold(),
            [("Executor", who, False)],
            thumbnail=self._avatar_url(after.icon),
            image=self._avatar_url(after.banner),
        )

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if guild is None:
            return
        who = await self._executor_tag(guild, ("invite_create",), invite.channel.id if invite.channel else None)
        await self._send(
            guild.id,
            "server",
            "🔗 Invite Created",
            f"Invite: {invite.url}",
            discord.Color.green(),
            [
                ("Creator", str(invite.inviter) if invite.inviter else "Unknown", True),
                ("Channel", invite.channel.mention if invite.channel else "Unknown", True),
                ("Executor", who, False),
            ],
        )

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if guild is None:
            return
        who = await self._executor_tag(guild, ("invite_delete",), invite.channel.id if invite.channel else None)
        await self._send(
            guild.id,
            "server",
            "🔗 Invite Deleted",
            f"Invite: {invite.url}",
            discord.Color.red(),
            [("Executor", who, False)],
        )

    @commands.Cog.listener()
    async def on_guild_emojis_update(
        self,
        guild: discord.Guild,
        before: tuple[discord.Emoji, ...],
        after: tuple[discord.Emoji, ...],
    ) -> None:
        old = {emoji.id: emoji for emoji in before}
        new = {emoji.id: emoji for emoji in after}
        added = [emoji.name for emoji_id, emoji in new.items() if emoji_id not in old]
        removed = [emoji.name for emoji_id, emoji in old.items() if emoji_id not in new]
        renamed = [
            f"{old[eid].name} → {new[eid].name}"
            for eid in old.keys() & new.keys()
            if old[eid].name != new[eid].name
        ]
        if not (added or removed or renamed):
            return
        who = await self._executor_tag(guild, ("emoji_create", "emoji_update", "emoji_delete"))
        await self._send(
            guild.id,
            "server",
            "😀 Server Emoji Update",
            "",
            discord.Color.blurple(),
            [
                ("Added", ", ".join(added) or "None", False),
                ("Removed", ", ".join(removed) or "None", False),
                ("Renamed", ", ".join(renamed) or "None", False),
                ("Executor", who, False),
            ],
        )

    @commands.Cog.listener()
    async def on_guild_stickers_update(
        self,
        guild: discord.Guild,
        before: tuple[discord.GuildSticker, ...],
        after: tuple[discord.GuildSticker, ...],
    ) -> None:
        old = {sticker.id: sticker for sticker in before}
        new = {sticker.id: sticker for sticker in after}
        added = [sticker.name for sid, sticker in new.items() if sid not in old]
        removed = [sticker.name for sid, sticker in old.items() if sid not in new]
        if not (added or removed):
            return
        who = await self._executor_tag(guild, ("sticker_create", "sticker_delete"))
        await self._send(
            guild.id,
            "server",
            "🏷️ Server Sticker Update",
            "",
            discord.Color.blurple(),
            [
                ("Added", ", ".join(added) or "None", False),
                ("Removed", ", ".join(removed) or "None", False),
                ("Executor", who, False),
            ],
        )

    # ------------------------------------------------------------------
    # Voice / commands / governance
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if (
            before.channel == after.channel
            and before.mute == after.mute
            and before.deaf == after.deaf
        ):
            return

        if before.channel is None and after.channel is not None:
            action = "joined"
        elif before.channel is not None and after.channel is None:
            action = "left"
        elif before.channel != after.channel:
            action = "moved"
        else:
            action = "voice state changed"

        who = await self._executor_tag(member.guild, ("member_move", "member_update"), member.id)
        await self._send(
            member.guild.id,
            "voice",
            "🔊 Voice State",
            f"{member} ({member.id}) {action}.",
            discord.Color.blue(),
            [
                ("Before", getattr(before.channel, "mention", "None"), True),
                ("After", getattr(after.channel, "mention", "None"), True),
                ("Mute/Deaf", f"{before.mute}/{before.deaf} → {after.mute}/{after.deaf}", False),
                ("Executor", who, False),
            ],
        )

    @commands.Cog.listener()
    async def on_audit_log_entry_create(
        self, entry: discord.AuditLogEntry
    ) -> None:
        guild = entry.guild
        if guild is None:
            return
        target = getattr(entry.target, "id", None)
        target_text = str(entry.target) if entry.target is not None else "Unknown"
        await self._send(
            guild.id,
            "mod",
            "🛡️ Audit Log Action",
            str(entry.action),
            discord.Color.dark_gold(),
            [
                ("Executor", f"{entry.user} ({entry.user.id})" if entry.user else "Unknown / unavailable", False),
                ("Target", f"{target_text} ({target})" if target else target_text, False),
                ("Reason", entry.reason or "No reason supplied", False),
            ],
        )
        self._event(
            "audit_log_action",
            {
                "guild_id": guild.id,
                "action": str(entry.action),
                "executor_id": getattr(entry.user, "id", None),
                "executor": str(entry.user) if entry.user else None,
                "target_id": target,
                "target": target_text,
                "reason": entry.reason,
            },
        )

    @commands.Cog.listener()
    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command,
    ) -> None:
        if not interaction.guild:
            return
        await self._send(
            interaction.guild.id,
            "mod",
            "🛡️ Slash Command Executed",
            f"/{command.qualified_name}",
            discord.Color.blurple(),
            [
                ("Executor", f"{interaction.user} ({interaction.user.id})", False),
                ("Channel", interaction.channel.mention if interaction.channel else "Unknown", True),
            ],
        )

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        await self._send(
            ctx.guild.id,
            "mod",
            "🛡️ Prefix Command Executed",
            f"{ctx.command.qualified_name if ctx.command else 'unknown'}",
            discord.Color.blurple(),
            [
                ("Executor", f"{ctx.author} ({ctx.author.id})", False),
                ("Channel", ctx.channel.mention, True),
            ],
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerLogger(bot))
