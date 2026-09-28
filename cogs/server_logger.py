"""Discord server audit logger."""

from __future__ import annotations

import io
import json
import logging
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

import discord
from discord.ext import commands

import config

logger = logging.getLogger(__name__)
AUDIT_LOG_PATH = config.PROJECT_ROOT / "logs" / "audit_history.log"
AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
_handler = RotatingFileHandler(AUDIT_LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s"))
logger.addHandler(_handler)


class ServerLogger(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.message_cache: deque[dict[str, Any]] = deque(maxlen=10000)
        self._messages: dict[int, dict[str, Any]] = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.id in self._messages:
            return
        record = {
            "message_id": message.id,
            "author_id": message.author.id,
            "author_tag": str(message.author),
            "channel_id": message.channel.id,
            "channel_name": getattr(message.channel, "name", str(message.channel)),
            "guild_id": message.guild.id,
            "content": message.content or "",
            "attachments": [a.url for a in message.attachments],
            "timestamp": message.created_at.astimezone(timezone.utc).isoformat(),
        }
        self.message_cache.append(record)
        self._messages[message.id] = record
        if len(self._messages) > self.message_cache.maxlen:
            live = {x["message_id"] for x in self.message_cache}
            for mid in list(self._messages):
                if mid not in live:
                    self._messages.pop(mid, None)

    async def _channel(self) -> Optional[discord.abc.Messageable]:
        if not config.AUDIT_LOG_CHANNEL_ID:
            return None
        channel = self.bot.get_channel(config.AUDIT_LOG_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(config.AUDIT_LOG_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.exception("Could not access audit log channel")
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
                if abs((datetime.now(timezone.utc) - entry.created_at).total_seconds()) <= 15:
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not read audit log for guild %s", guild.id)
        return None

    async def _send(
        self,
        title: str,
        description: str,
        color: discord.Color,
        fields: Optional[list[tuple[str, str, bool]]] = None,
        file: Optional[discord.File] = None,
    ) -> None:
        channel = await self._channel()
        if channel is None:
            return
        embed = discord.Embed(title=title, description=description[:4096], color=color, timestamp=datetime.now(timezone.utc))
        for name, value, inline in fields or []:
            embed.add_field(name=name[:256], value=(value or "-")[:1024], inline=inline)
        embed.set_footer(text="Server Audit Logger")
        try:
            await channel.send(embed=embed, file=file)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not send audit event")

    def _event(self, name: str, data: dict[str, Any]) -> None:
        logger.info("AUDIT_EVENT %s", json.dumps({"event": name, "timestamp": datetime.now(timezone.utc).isoformat(), **data}, ensure_ascii=False, default=str))

    @staticmethod
    def _clip(value: str, limit: int = 1024) -> str:
        value = value or "[no text content]"
        return value if len(value) <= limit else value[:limit - 3] + "..."

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        data = self._messages.pop(payload.message_id, None)
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        executor = await self._executor(guild, (discord.AuditLogAction.message_delete,), payload.message_id) if guild else None
        who = str(executor) if executor else "Unknown / unavailable"
        if data:
            await self._send(
                "🗑️ Message Deleted",
                self._clip(data["content"]),
                discord.Color.orange(),
                [
                    ("Author", f"{data['author_tag']} ({data['author_id']})", False),
                    ("Channel", f"<#{data['channel_id']}>", True),
                    ("Sent", data["timestamp"], True),
                    ("Deleted by", who, True),
                    ("Attachments", "\n".join(data["attachments"]) or "None", False),
                ],
            )
            self._event("message_delete", {**data, "executor": who})
        else:
            await self._send(
                "🗑️ Message Deleted",
                f"Message {payload.message_id} was deleted, but its cached content was unavailable.",
                discord.Color.orange(),
                [("Channel", f"<#{payload.channel_id}>", True), ("Deleted by", who, True)],
            )
            self._event("message_delete_uncached", {"message_id": payload.message_id, "channel_id": payload.channel_id, "guild_id": payload.guild_id, "executor": who})

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        executor = await self._executor(
            guild,
            (discord.AuditLogAction.message_bulk_delete, discord.AuditLogAction.message_delete),
        ) if guild else None
        who = str(executor) if executor else "Unknown / unavailable"
        cached = []
        for mid in payload.message_ids:
            item = self._messages.pop(mid, None)
            if item:
                cached.append(item)
        cached.sort(key=lambda x: x["timestamp"])
        missing = len(payload.message_ids) - len(cached)
        lines = []
        for item in cached:
            attachments = " | ".join(item["attachments"]) or "None"
            lines.append(
                f"[{item['timestamp']}] {item['author_tag']} ({item['author_id']}) "
                f"in #{item['channel_name']} | {item['content'] or '[no text content]'} | Attachments: {attachments}"
            )
        file = None
        if len(payload.message_ids) > 10:
            body = "\n".join(lines) or "No cached message content."
            channel_name = cached[0]["channel_name"] if cached else str(payload.channel_id)
            file = discord.File(
                io.BytesIO(body.encode("utf-8")),
                filename=f"purge_{channel_name}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.txt",
            )
            preview = "Full cached purge contents are attached as a text log."
        else:
            preview = "\n".join(lines)[:3500] or "No cached message content."
        await self._send(
            "🧹 Bulk Message Purge",
            f"{len(payload.message_ids)} messages purged in <#{payload.channel_id}>.\nCached: {len(cached)} | Missing from cache: {missing}\nExecutor: {who}\n\n{preview}",
            discord.Color.red(),
            [("Audit Executor", who, True)],
            file,
        )
        self._event("bulk_message_delete", {
            "guild_id": payload.guild_id,
            "channel_id": payload.channel_id,
            "message_ids": list(payload.message_ids),
            "cached_count": len(cached),
            "missing_count": missing,
            "executor": who,
            "messages": cached,
        })

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not before.guild or before.content == after.content:
            return
        self._cache_edited(after)
        await self._send(
            "✏️ Message Edited",
            "A message was edited.",
            discord.Color.gold(),
            [
                ("Author", f"{before.author} ({before.author.id})", False),
                ("Channel", f"{before.channel.mention} ({before.channel.id})", True),
                ("Before", self._clip(before.content), False),
                ("After", self._clip(after.content), False),
                ("Message", f"https://discord.com/channels/{before.guild.id}/{before.channel.id}/{before.id}", False),
            ],
        )
        self._event("message_edit", {
            "guild_id": before.guild.id,
            "channel_id": before.channel.id,
            "message_id": before.id,
            "author_id": before.author.id,
            "author_tag": str(before.author),
            "before": before.content,
            "after": after.content,
        })

    def _cache_edited(self, message: discord.Message) -> None:
        self._messages[message.id] = {
            "message_id": message.id,
            "author_id": message.author.id,
            "author_tag": str(message.author),
            "channel_id": message.channel.id,
            "channel_name": getattr(message.channel, "name", str(message.channel)),
            "guild_id": message.guild.id if message.guild else None,
            "content": message.content or "",
            "attachments": [a.url for a in message.attachments],
            "timestamp": message.created_at.astimezone(timezone.utc).isoformat(),
        }

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._send("📥 Member Joined", f"{member} ({member.id}) joined.", discord.Color.green())
        self._event("member_join", {"guild_id": member.guild.id, "user_id": member.id, "user": str(member)})

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        executor = await self._executor(member.guild, (discord.AuditLogAction.kick, discord.AuditLogAction.ban), member.id)
        who = str(executor) if executor else "Unknown / unavailable"
        await self._send("📤 Member Removed", f"{member} ({member.id}) left or was removed.", discord.Color.red(), [("Executor", who, False)])
        self._event("member_remove", {"guild_id": member.guild.id, "user_id": member.id, "user": str(member), "executor": who})

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        executor = await self._executor(guild, (discord.AuditLogAction.ban,), user.id)
        who = str(executor) if executor else "Unknown / unavailable"
        await self._send("🔨 Member Banned", f"{user} ({user.id}) was banned.", discord.Color.dark_red(), [("Executor", who, False)])
        self._event("member_ban", {"guild_id": guild.id, "user_id": user.id, "user": str(user), "executor": who})

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        executor = await self._executor(guild, (discord.AuditLogAction.unban,), user.id)
        who = str(executor) if executor else "Unknown / unavailable"
        await self._send("♻️ Member Unbanned", f"{user} ({user.id}) was unbanned.", discord.Color.green(), [("Executor", who, False)])
        self._event("member_unban", {"guild_id": guild.id, "user_id": user.id, "user": str(user), "executor": who})

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles != after.roles:
            added = [r.name for r in after.roles if r not in before.roles and r != after.guild.default_role]
            removed = [r.name for r in before.roles if r not in after.roles and r != before.guild.default_role]
            if added or removed:
                await self._send("🎭 Member Roles Changed", f"{after} ({after.id})", discord.Color.blurple(), [("Added", ", ".join(added) or "None", False), ("Removed", ", ".join(removed) or "None", False)])
                self._event("member_role_update", {"guild_id": after.guild.id, "user_id": after.id, "user": str(after), "added_roles": added, "removed_roles": removed})
        if before.communication_disabled_until != after.communication_disabled_until:
            executor = await self._executor(after.guild, (discord.AuditLogAction.member_update,), after.id)
            who = str(executor) if executor else "Unknown / unavailable"
            await self._send("⏱️ Member Timeout Updated", f"{after} ({after.id})", discord.Color.orange(), [("Before", str(before.communication_disabled_until or "Not timed out"), True), ("After", str(after.communication_disabled_until or "Not timed out"), True), ("Executor", who, False)])
            self._event("member_timeout_update", {"guild_id": after.guild.id, "user_id": after.id, "before": str(before.communication_disabled_until), "after": str(after.communication_disabled_until), "executor": who})

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self._role("created", role)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self._role("deleted", role)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        if before.name == after.name and before.permissions == after.permissions:
            return
        await self._send("🎭 Role Updated", f"{before.name} → {after.name}", discord.Color.blurple(), [("Role ID", str(after.id), True), ("Before Permissions", str(before.permissions.value), False), ("After Permissions", str(after.permissions.value), False)])
        self._event("role_update", {"guild_id": after.guild.id, "role_id": after.id, "before_name": before.name, "after_name": after.name, "before_permissions": before.permissions.value, "after_permissions": after.permissions.value})

    async def _role(self, action: str, role: discord.Role) -> None:
        audit_action = discord.AuditLogAction.role_create if action == "created" else discord.AuditLogAction.role_delete
        executor = await self._executor(role.guild, (audit_action,))
        who = str(executor) if executor else "Unknown / unavailable"
        await self._send(f"🎭 Role {action.title()}", f"{role.name} ({role.id})", discord.Color.blurple(), [("Executor", who, False)])
        self._event("role_" + action, {"guild_id": role.guild.id, "role_id": role.id, "role": role.name, "executor": who})

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if before.channel == after.channel and before.mute == after.mute and before.deaf == after.deaf:
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
            "🔊 Voice State",
            f"{member} ({member.id}) {action}.",
            discord.Color.blue(),
            [("Before", getattr(before.channel, "mention", "None"), True), ("After", getattr(after.channel, "mention", "None"), True), ("Mute/Deaf", f"{before.mute}/{before.deaf} → {after.mute}/{after.deaf}", False)],
        )
        self._event("voice_state_update", {
            "guild_id": member.guild.id,
            "user_id": member.id,
            "user": str(member),
            "action": action,
            "before_channel_id": before.channel.id if before.channel else None,
            "after_channel_id": after.channel.id if after.channel else None,
            "before_mute": before.mute,
            "after_mute": after.mute,
            "before_deaf": before.deaf,
            "after_deaf": after.deaf,
        })


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerLogger(bot))
