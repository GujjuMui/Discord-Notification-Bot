"""Discord server audit logger with guild-scoped persistent configuration."""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

import discord
from discord.ext import commands

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
logger.addHandler(_handler)


class ServerLogger(commands.Cog):
    """Capture server events and dispatch them to each guild's configured channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._history_cache_loaded = False

    async def _warm_message_cache(self) -> None:
        """Cache recent channel history so later delete/purge events have context.

        Discord's raw delete events only provide message IDs after deletion.
        The bot therefore needs to have seen the messages beforehand. On startup
        we backfill a bounded recent history for channels the bot can read.
        """
        if self._history_cache_loaded:
            return

        self._history_cache_loaded = True
        total_cached = 0

        for guild in self.bot.guilds:
            me = guild.me
            if me is None:
                continue

            for channel in guild.text_channels:
                permissions = channel.permissions_for(me)
                if not (permissions.view_channel and permissions.read_message_history):
                    continue

                try:
                    async for message in channel.history(limit=100):
                        if not message.guild:
                            continue
                        record = self._message_record(message)
                        db.cache_message(
                            message_id=record["message_id"],
                            guild_id=record["guild_id"],
                            channel_id=record["channel_id"],
                            author_tag=record["author_tag"],
                            content=record["content"],
                            attachments=record["attachments"],
                            timestamp=record["timestamp"],
                        )
                        total_cached += 1
                except (discord.Forbidden, discord.HTTPException):
                    logger.warning(
                        "Could not warm message cache for #%s in guild %s",
                        channel.name,
                        guild.id,
                    )

        logger.info(
            "Message cache warm-up complete: cached %s recent messages.",
            total_cached,
        )

    async def _channel(
        self, guild_id: int
    ) -> Optional[discord.abc.Messageable]:
        settings = db.get_guild_settings(guild_id)
        channel_id = (
            settings.get("audit_log_channel_id")
            if settings
            else None
        )
        if not channel_id:
            return None

        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning(
                    "Could not access configured audit channel %s for guild %s",
                    channel_id,
                    guild_id,
                )
                return None

        return channel if hasattr(channel, "send") else None

    async def _executor(
        self,
        guild: discord.Guild,
        actions: tuple[discord.AuditLogAction, ...],
        target_id: Optional[int] = None,
    ) -> Optional[discord.abc.User]:
        if not guild.me or not guild.me.guild_permissions.view_audit_log:
            return None

        try:
            async for entry in guild.audit_logs(limit=10):
                if entry.action not in actions:
                    continue
                target = getattr(entry.target, "id", None)
                if target_id is not None and target not in (None, target_id):
                    continue
                if abs(
                    (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                ) <= 15:
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not read audit log for guild %s", guild.id)

        return None

    async def _send(
        self,
        guild_id: int,
        title: str,
        description: str,
        color: discord.Color,
        fields: Optional[list[tuple[str, str, bool]]] = None,
        file: Optional[discord.File] = None,
    ) -> None:
        channel = await self._channel(guild_id)
        if channel is None:
            return

        embed = discord.Embed(
            title=title,
            description=description[:4096],
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        for name, value, inline in fields or []:
            embed.add_field(
                name=name[:256],
                value=(value or "-")[:1024],
                inline=inline,
            )
        embed.set_footer(text="Server Audit Logger")

        try:
            if file is None:
                await channel.send(embed=embed)
            else:
                await channel.send(embed=embed, file=file)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Could not send audit event for guild %s", guild_id
            )

    def _event(self, name: str, data: dict[str, Any]) -> None:
        payload = {
            "event": name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        logger.info(
            "AUDIT_EVENT %s",
            json.dumps(payload, ensure_ascii=False, default=str),
        )

    @staticmethod
    def _clip(value: str, limit: int = 1024) -> str:
        value = value or "[no text content]"
        return value if len(value) <= limit else value[: limit - 3] + "..."

    @staticmethod
    def _message_record(message: discord.Message) -> dict[str, Any]:
        return {
            "message_id": message.id,
            "guild_id": message.guild.id,
            "channel_id": message.channel.id,
            "author_tag": str(message.author),
            "content": message.content or "",
            "attachments": [attachment.url for attachment in message.attachments],
            "timestamp": message.created_at.astimezone(timezone.utc).isoformat(),
        }

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._warm_message_cache()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return

        record = self._message_record(message)
        db.cache_message(
            message_id=record["message_id"],
            guild_id=record["guild_id"],
            channel_id=record["channel_id"],
            author_tag=record["author_tag"],
            content=record["content"],
            attachments=record["attachments"],
            timestamp=record["timestamp"],
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        if not payload.guild_id:
            return

        data = db.delete_cached_message(payload.message_id)
        guild = self.bot.get_guild(payload.guild_id)
        executor = (
            await self._executor(
                guild,
                (discord.AuditLogAction.message_delete,),
                payload.message_id,
            )
            if guild
            else None
        )
        who = str(executor) if executor else "Unknown / unavailable"

        if data:
            await self._send(
                payload.guild_id,
                "🗑️ Message Deleted",
                self._clip(data["content"]),
                discord.Color.orange(),
                [
                    ("Author", data["author_tag"], False),
                    ("Channel", f"<#{data['channel_id']}>", True),
                    ("Sent", data["timestamp"], True),
                    ("Deleted by", who, True),
                    (
                        "Attachments",
                        "\n".join(data["attachments"]) or "None",
                        False,
                    ),
                ],
            )
            self._event("message_delete", {**data, "executor": who})
        else:
            await self._send(
                payload.guild_id,
                "🗑️ Message Deleted",
                (
                    f"Message {payload.message_id} was deleted, but its "
                    "cached content was unavailable."
                ),
                discord.Color.orange(),
                [
                    ("Channel", f"<#{payload.channel_id}>", True),
                    ("Deleted by", who, True),
                ],
            )
            self._event(
                "message_delete_uncached",
                {
                    "message_id": payload.message_id,
                    "channel_id": payload.channel_id,
                    "guild_id": payload.guild_id,
                    "executor": who,
                },
            )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        if not payload.guild_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        executor = (
            await self._executor(
                guild,
                (
                    discord.AuditLogAction.message_bulk_delete,
                    discord.AuditLogAction.message_delete,
                ),
            )
            if guild
            else None
        )
        who = str(executor) if executor else "Unknown / unavailable"

        cached = db.get_cached_messages(
            list(payload.message_ids),
            guild_id=payload.guild_id,
        )
        cached_ids = {item["message_id"] for item in cached}

        for message_id in payload.message_ids:
            if message_id in cached_ids:
                db.delete_cached_message(message_id)

        missing = len(payload.message_ids) - len(cached)
        lines = []
        for item in cached:
            attachments = " | ".join(item["attachments"]) or "None"
            lines.append(
                f"[{item['timestamp']}] {item['author_tag']} "
                f"in channel {item['channel_id']} | "
                f"{item['content'] or '[no text content]'} | "
                f"Attachments: {attachments}"
            )

        file = None
        if len(payload.message_ids) > 10:
            body = "\n".join(lines) or "No cached message content."
            file = discord.File(
                io.BytesIO(body.encode("utf-8")),
                filename=(
                    f"purge_{payload.channel_id}_"
                    f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.txt"
                ),
            )
            preview = "Full cached purge contents are attached as a text log."
        else:
            preview = (
                "\n".join(lines)[:3500]
                or "No cached message content."
            )

        await self._send(
            payload.guild_id,
            "🧹 Bulk Message Purge",
            (
                f"{len(payload.message_ids)} messages purged in "
                f"<#{payload.channel_id}>.\n"
                f"Cached: {len(cached)} | Missing from cache: {missing}\n"
                f"Executor: {who}\n\n{preview}"
            ),
            discord.Color.red(),
            [("Audit Executor", who, True)],
            file,
        )
        self._event(
            "bulk_message_delete",
            {
                "guild_id": payload.guild_id,
                "channel_id": payload.channel_id,
                "message_ids": list(payload.message_ids),
                "cached_count": len(cached),
                "missing_count": missing,
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
        db.cache_message(
            message_id=record["message_id"],
            guild_id=record["guild_id"],
            channel_id=record["channel_id"],
            author_tag=record["author_tag"],
            content=record["content"],
            attachments=record["attachments"],
            timestamp=record["timestamp"],
        )

        await self._send(
            before.guild.id,
            "✏️ Message Edited",
            "A message was edited.",
            discord.Color.gold(),
            [
                ("Author", f"{before.author} ({before.author.id})", False),
                (
                    "Channel",
                    f"{before.channel.mention} ({before.channel.id})",
                    True,
                ),
                ("Before", self._clip(before.content), False),
                ("After", self._clip(after.content), False),
                (
                    "Message",
                    f"https://discord.com/channels/{before.guild.id}/"
                    f"{before.channel.id}/{before.id}",
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

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._send(
            member.guild.id,
            "📥 Member Joined",
            f"{member} ({member.id}) joined.",
            discord.Color.green(),
        )
        self._event(
            "member_join",
            {
                "guild_id": member.guild.id,
                "user_id": member.id,
                "user": str(member),
            },
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        executor = await self._executor(
            member.guild,
            (discord.AuditLogAction.kick, discord.AuditLogAction.ban),
            member.id,
        )
        who = str(executor) if executor else "Unknown / unavailable"
        await self._send(
            member.guild.id,
            "📤 Member Removed",
            f"{member} ({member.id}) left or was removed.",
            discord.Color.red(),
            [("Executor", who, False)],
        )
        self._event(
            "member_remove",
            {
                "guild_id": member.guild.id,
                "user_id": member.id,
                "user": str(member),
                "executor": who,
            },
        )

    @commands.Cog.listener()
    async def on_member_ban(
        self, guild: discord.Guild, user: discord.User
    ) -> None:
        executor = await self._executor(
            guild, (discord.AuditLogAction.ban,), user.id
        )
        who = str(executor) if executor else "Unknown / unavailable"
        await self._send(
            guild.id,
            "🔨 Member Banned",
            f"{user} ({user.id}) was banned.",
            discord.Color.dark_red(),
            [("Executor", who, False)],
        )
        self._event(
            "member_ban",
            {
                "guild_id": guild.id,
                "user_id": user.id,
                "user": str(user),
                "executor": who,
            },
        )

    @commands.Cog.listener()
    async def on_member_unban(
        self, guild: discord.Guild, user: discord.User
    ) -> None:
        executor = await self._executor(
            guild, (discord.AuditLogAction.unban,), user.id
        )
        who = str(executor) if executor else "Unknown / unavailable"
        await self._send(
            guild.id,
            "♻️ Member Unbanned",
            f"{user} ({user.id}) was unbanned.",
            discord.Color.green(),
            [("Executor", who, False)],
        )
        self._event(
            "member_unban",
            {
                "guild_id": guild.id,
                "user_id": user.id,
                "user": str(user),
                "executor": who,
            },
        )

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        if before.roles != after.roles:
            added = [
                role.name
                for role in after.roles
                if role not in before.roles and role != after.guild.default_role
            ]
            removed = [
                role.name
                for role in before.roles
                if role not in after.roles and role != before.guild.default_role
            ]
            if added or removed:
                await self._send(
                    after.guild.id,
                    "🎭 Member Roles Changed",
                    f"{after} ({after.id})",
                    discord.Color.blurple(),
                    [
                        ("Added", ", ".join(added) or "None", False),
                        ("Removed", ", ".join(removed) or "None", False),
                    ],
                )
                self._event(
                    "member_role_update",
                    {
                        "guild_id": after.guild.id,
                        "user_id": after.id,
                        "user": str(after),
                        "added_roles": added,
                        "removed_roles": removed,
                    },
                )

        before_timeout = getattr(before, "communication_disabled_until", None)
        after_timeout = getattr(after, "communication_disabled_until", None)

        if before_timeout != after_timeout:
            executor = await self._executor(
                after.guild,
                (discord.AuditLogAction.member_update,),
                after.id,
            )
            who = str(executor) if executor else "Unknown / unavailable"
            await self._send(
                after.guild.id,
                "⏱️ Member Timeout Updated",
                f"{after} ({after.id})",
                discord.Color.orange(),
                [
                    (
                        "Before",
                        str(
                            before_timeout
                            or "Not timed out"
                        ),
                        True,
                    ),
                    (
                        "After",
                        str(
                            after_timeout
                            or "Not timed out"
                        ),
                        True,
                    ),
                    ("Executor", who, False),
                ],
            )
            self._event(
                "member_timeout_update",
                {
                    "guild_id": after.guild.id,
                    "user_id": after.id,
                    "before": str(before_timeout),
                    "after": str(after_timeout),
                    "executor": who,
                },
            )

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self._role("created", role)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self._role("deleted", role)

    @commands.Cog.listener()
    async def on_guild_role_update(
        self, before: discord.Role, after: discord.Role
    ) -> None:
        if (
            before.name == after.name
            and before.permissions == after.permissions
        ):
            return

        await self._send(
            after.guild.id,
            "🎭 Role Updated",
            f"{before.name} → {after.name}",
            discord.Color.blurple(),
            [
                ("Role ID", str(after.id), True),
                (
                    "Before Permissions",
                    str(before.permissions.value),
                    False,
                ),
                (
                    "After Permissions",
                    str(after.permissions.value),
                    False,
                ),
            ],
        )
        self._event(
            "role_update",
            {
                "guild_id": after.guild.id,
                "role_id": after.id,
                "before_name": before.name,
                "after_name": after.name,
                "before_permissions": before.permissions.value,
                "after_permissions": after.permissions.value,
            },
        )

    async def _role(self, action: str, role: discord.Role) -> None:
        audit_action = (
            discord.AuditLogAction.role_create
            if action == "created"
            else discord.AuditLogAction.role_delete
        )
        executor = await self._executor(role.guild, (audit_action,))
        who = str(executor) if executor else "Unknown / unavailable"

        await self._send(
            role.guild.id,
            f"🎭 Role {action.title()}",
            f"{role.name} ({role.id})",
            discord.Color.blurple(),
            [("Executor", who, False)],
        )
        self._event(
            "role_" + action,
            {
                "guild_id": role.guild.id,
                "role_id": role.id,
                "role": role.name,
                "executor": who,
            },
        )

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

        await self._send(
            member.guild.id,
            "🔊 Voice State",
            f"{member} ({member.id}) {action}.",
            discord.Color.blue(),
            [
                (
                    "Before",
                    getattr(before.channel, "mention", "None"),
                    True,
                ),
                (
                    "After",
                    getattr(after.channel, "mention", "None"),
                    True,
                ),
                (
                    "Mute/Deaf",
                    f"{before.mute}/{before.deaf} → "
                    f"{after.mute}/{after.deaf}",
                    False,
                ),
            ],
        )
        self._event(
            "voice_state_update",
            {
                "guild_id": member.guild.id,
                "user_id": member.id,
                "user": str(member),
                "action": action,
                "before_channel_id": (
                    before.channel.id if before.channel else None
                ),
                "after_channel_id": (
                    after.channel.id if after.channel else None
                ),
                "before_mute": before.mute,
                "after_mute": after.mute,
                "before_deaf": before.deaf,
                "after_deaf": after.deaf,
            },
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerLogger(bot))
