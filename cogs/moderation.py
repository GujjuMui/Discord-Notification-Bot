"""Owner moderation, diagnostics, demo mode, and multi-channel broadcast cog.

Authorization tiers
───────────────────
  Owner-only   — BOT_OWNER_ID env var or Discord application owner.
                 Bypasses guild staff checks but still constrained by
                 Discord's platform rules (hierarchy, bot permissions).
  Staff        — Server owner, bot owner, or trusted user (is_trusted_or_owner).
                 Required for moderation commands.
  Read-only    — /userinfo, /serverinfo, /permissions available to all members.

Demo mode
─────────
  Set DEMO_MODE=true and DEMO_GUILD_ID=<guild_id> in the environment.
  In that guild all destructive moderation actions are intercepted and
  return a [SIMULATED DEMO MODE] embed instead of executing.
"""

from __future__ import annotations

import asyncio
import os
import platform
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
import psutil
from discord import app_commands
from discord.ext import commands

import config
from database import db

# ---------------------------------------------------------------------------
# Demo mode config
# ---------------------------------------------------------------------------

_DEMO_MODE: bool = os.getenv("DEMO_MODE", "false").strip().lower() in {"true", "1"}
_DEMO_GUILD_ID: Optional[int] = None
_raw_demo_guild = os.getenv("DEMO_GUILD_ID", "").strip()
if _raw_demo_guild:
    try:
        _DEMO_GUILD_ID = int(_raw_demo_guild)
    except ValueError:
        pass

_DEMO_AUDIT_CHANNEL_ID: Optional[int] = None
_raw_demo_audit = os.getenv("DEMO_AUDIT_LOG_CHANNEL", "").strip()
if _raw_demo_audit:
    try:
        _DEMO_AUDIT_CHANNEL_ID = int(_raw_demo_audit)
    except ValueError:
        pass

# TEST_CHANNEL_ID — /testmessage is restricted to this channel only.
_TEST_CHANNEL_ID: Optional[int] = None
_raw_test_ch = os.getenv("TEST_CHANNEL_ID", "").strip()
if _raw_test_ch:
    try:
        _TEST_CHANNEL_ID = int(_raw_test_ch)
    except ValueError:
        pass

# ---------------------------------------------------------------------------
# Authorization helpers
# ---------------------------------------------------------------------------

def _is_owner_check():
    """Restrict command to the bot owner only."""
    async def predicate(interaction: discord.Interaction) -> bool:
        owner_id = config.BOT_OWNER_ID
        if owner_id is None:
            try:
                app = await interaction.client.application_info()
                owner_id = app.owner.id if app.owner else None
            except (discord.HTTPException, discord.Forbidden):
                return False
        return interaction.user.id == owner_id
    return app_commands.check(predicate)


def _is_staff_check():
    """Server owner, bot owner, or trusted user."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        owner_id = config.BOT_OWNER_ID
        if owner_id is None:
            try:
                app = await interaction.client.application_info()
                owner_id = app.owner.id if app.owner else None
            except (discord.HTTPException, discord.Forbidden):
                owner_id = None
        return (
            interaction.user.id == owner_id
            or interaction.user.id == interaction.guild.owner_id
            or db.is_trusted_user(interaction.guild.id, interaction.user.id)
        )
    return app_commands.check(predicate)


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

class PreflightResult:
    def __init__(self, ok: bool, reason: str = ""):
        self.ok = ok
        self.reason = reason

    def __bool__(self) -> bool:
        return self.ok


def _preflight(
    guild: discord.Guild,
    bot_member: discord.Member,
    target: discord.Member,
    required_bot_perm: str,
) -> PreflightResult:
    """Run all pre-flight checks before a destructive moderation action."""
    # Never target the bot itself
    if target.id == bot_member.id:
        return PreflightResult(False, "Cannot target the bot itself.")

    # Never target the guild owner
    if target.id == guild.owner_id:
        return PreflightResult(False, "Cannot moderate the server owner.")

    # Bot must have the required permission
    bot_perms = bot_member.guild_permissions
    if not getattr(bot_perms, required_bot_perm, False):
        return PreflightResult(
            False,
            f"Bot is missing the `{required_bot_perm}` permission.",
        )

    # Bot's highest role must be above the target's highest role
    if bot_member.top_role <= target.top_role:
        return PreflightResult(
            False,
            f"Bot's highest role ({bot_member.top_role.mention}) is not above "
            f"target's highest role ({target.top_role.mention}). "
            "Move the bot's role higher in Server Settings → Roles.",
        )

    return PreflightResult(True)


# ---------------------------------------------------------------------------
# Demo-mode interception
# ---------------------------------------------------------------------------

def _is_demo(guild: discord.Guild) -> bool:
    return _DEMO_MODE and _DEMO_GUILD_ID is not None and guild.id == _DEMO_GUILD_ID


def _demo_embed(action: str, target: discord.Member, reason: str) -> discord.Embed:
    embed = discord.Embed(
        title="🧪 [SIMULATED DEMO MODE — NO REAL ACTION TAKEN]",
        description=(
            f"**Action:** `{action}`\n"
            f"**Target:** {target.mention} (`{target}`)\n"
            f"**Reason:** {reason or 'No reason provided'}"
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Demo mode is active — set DEMO_MODE=false to enable real actions")
    return embed


# ---------------------------------------------------------------------------
# Delete-test-message button view
# ---------------------------------------------------------------------------

class DeleteTestMessageView(discord.ui.View):
    def __init__(self, message: discord.Message):
        super().__init__(timeout=300)
        self._message = message

    @discord.ui.button(label="🗑️ Delete Test Message", style=discord.ButtonStyle.danger)
    async def delete_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await self._message.delete()
            await interaction.followup.send("Test message deleted.", ephemeral=True)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            await interaction.followup.send(f"Could not delete: {exc}", ephemeral=True)
        self.stop()


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Moderation(commands.Cog):
    """Owner moderation, diagnostics, demo mode, and broadcast commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Cooldown: /testmessage — 1 use per 30s per channel
        self._testmessage_cooldowns: dict[int, datetime] = {}

    # ------------------------------------------------------------------
    # Shared error helper
    # ------------------------------------------------------------------

    async def _err(self, interaction: discord.Interaction, msg: str) -> None:
        embed = discord.Embed(
            title="🚫 Error",
            description=msg,
            color=discord.Color.red(),
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _ok(self, interaction: discord.Interaction, msg: str, color: int = 0x34C759) -> None:
        embed = discord.Embed(description=msg, color=discord.Color(color))
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    # ==================================================================
    # SECTION 1 — MODERATION SUITE
    # ==================================================================

    @app_commands.command(name="ban", description="Ban a member from the server.")
    @_is_staff_check()
    @app_commands.describe(
        target="Member to ban.",
        reason="Reason for the ban (shown in audit log).",
        delete_message_days="Days of message history to delete (0–7).",
    )
    async def ban(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        reason: str = "No reason provided",
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return await self._err(interaction, "Server-only command.")

        bot_member = guild.me
        check = _preflight(guild, bot_member, target, "ban_members")
        if not check:
            return await self._err(interaction, check.reason)

        if _is_demo(guild):
            return await interaction.followup.send(
                embed=_demo_embed("BAN", target, reason), ephemeral=True
            )

        await guild.ban(target, reason=f"{interaction.user} — {reason}", delete_message_days=delete_message_days)
        await self._ok(interaction, f"✅ **{target}** has been banned.\nReason: {reason}")

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @_is_staff_check()
    @app_commands.describe(target="Member to kick.", reason="Reason for the kick.")
    async def kick(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        reason: str = "No reason provided",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return await self._err(interaction, "Server-only command.")

        check = _preflight(guild, guild.me, target, "kick_members")
        if not check:
            return await self._err(interaction, check.reason)

        if _is_demo(guild):
            return await interaction.followup.send(
                embed=_demo_embed("KICK", target, reason), ephemeral=True
            )

        await guild.kick(target, reason=f"{interaction.user} — {reason}")
        await self._ok(interaction, f"✅ **{target}** has been kicked.\nReason: {reason}")

    @app_commands.command(name="timeout", description="Timeout a member (mute them temporarily).")
    @_is_staff_check()
    @app_commands.describe(
        target="Member to timeout.",
        duration="Duration in minutes (1–40320 / max 28 days).",
        reason="Reason for the timeout.",
    )
    async def timeout(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        duration: app_commands.Range[int, 1, 40320],
        reason: str = "No reason provided",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return await self._err(interaction, "Server-only command.")

        check = _preflight(guild, guild.me, target, "moderate_members")
        if not check:
            return await self._err(interaction, check.reason)

        if _is_demo(guild):
            return await interaction.followup.send(
                embed=_demo_embed(f"TIMEOUT ({duration}m)", target, reason), ephemeral=True
            )

        until = discord.utils.utcnow() + timedelta(minutes=duration)
        await target.timeout(until, reason=f"{interaction.user} — {reason}")
        await self._ok(
            interaction,
            f"✅ **{target}** timed out for **{duration} minute(s)**.\nReason: {reason}",
        )

    @app_commands.command(name="untimeout", description="Remove an active timeout from a member.")
    @_is_staff_check()
    @app_commands.describe(target="Member to un-timeout.", reason="Reason.")
    async def untimeout(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        reason: str = "No reason provided",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return await self._err(interaction, "Server-only command.")

        check = _preflight(guild, guild.me, target, "moderate_members")
        if not check:
            return await self._err(interaction, check.reason)

        if target.timed_out_until is None:
            return await self._err(interaction, f"**{target}** is not currently timed out.")

        await target.timeout(None, reason=f"{interaction.user} — {reason}")
        await self._ok(interaction, f"✅ Timeout removed from **{target}**.\nReason: {reason}")

    @app_commands.command(name="purge", description="Bulk-delete messages in the current channel.")
    @_is_staff_check()
    @app_commands.describe(
        amount="Number of messages to delete (1–100).",
        filter_user="Only delete messages from this user (optional).",
    )
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100],
        filter_user: Optional[discord.Member] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        channel = interaction.channel
        if not guild or not isinstance(channel, discord.TextChannel):
            return await self._err(interaction, "Must be used in a server text channel.")

        if not guild.me.guild_permissions.manage_messages:
            return await self._err(interaction, "Bot is missing `manage_messages` permission.")

        if _is_demo(guild):
            embed = discord.Embed(
                title="🧪 [SIMULATED DEMO MODE — NO REAL ACTION TAKEN]",
                description=f"**Action:** PURGE {amount} messages"
                + (f" from {filter_user.mention}" if filter_user else ""),
                color=discord.Color.orange(),
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)

        # Discord bulk-delete only works on messages < 14 days old
        cutoff = discord.utils.utcnow() - timedelta(days=14)

        def check_msg(m: discord.Message) -> bool:
            if m.created_at < cutoff:
                return False
            if filter_user and m.author.id != filter_user.id:
                return False
            return True

        deleted = await channel.purge(limit=amount, check=check_msg)
        await self._ok(
            interaction,
            f"✅ Deleted **{len(deleted)}** message(s)"
            + (f" from {filter_user.mention}" if filter_user else "")
            + ".\n*Messages older than 14 days were skipped (Discord API limit).*",
        )

    # ------------------------------------------------------------------
    # Read-only info commands (no access restriction)
    # ------------------------------------------------------------------

    @app_commands.command(name="userinfo", description="Show information about a member.")
    @app_commands.describe(target="Member to look up (defaults to yourself).")
    async def userinfo(
        self,
        interaction: discord.Interaction,
        target: Optional[discord.Member] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return await self._err(interaction, "Server-only command.")

        member = target or interaction.user
        if not isinstance(member, discord.Member):
            return await self._err(interaction, "Could not resolve member in this server.")

        created = discord.utils.format_dt(member.created_at, "F")
        joined = discord.utils.format_dt(member.joined_at, "F") if member.joined_at else "Unknown"
        roles = [r.mention for r in reversed(member.roles) if r.name != "@everyone"]
        top_role = member.top_role.mention if member.top_role.name != "@everyone" else "None"

        # Hierarchy rank (1 = highest non-everyone role)
        sorted_roles = sorted(
            [r for r in guild.roles if r.name != "@everyone"],
            key=lambda r: r.position,
            reverse=True,
        )
        rank = next(
            (i + 1 for i, r in enumerate(sorted_roles) if r in member.roles),
            len(sorted_roles),
        )

        # User flags
        flags = [f.name.replace("_", " ").title() for f, val in member.public_flags if val]

        embed = discord.Embed(
            title=str(member),
            color=member.color if member.color.value else discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Bot", value="Yes" if member.bot else "No", inline=True)
        embed.add_field(name="Hierarchy Rank", value=f"#{rank} of {len(sorted_roles)}", inline=True)
        embed.add_field(name="Account Created", value=created, inline=False)
        embed.add_field(name="Joined Server", value=joined, inline=False)
        embed.add_field(name="Top Role", value=top_role, inline=True)
        embed.add_field(
            name=f"Roles ({len(roles)})",
            value=" ".join(roles[:20]) or "None",
            inline=False,
        )
        if flags:
            embed.add_field(name="Badges", value=", ".join(flags), inline=False)
        if member.timed_out_until and member.timed_out_until > discord.utils.utcnow():
            embed.add_field(
                name="⏱️ Timed Out Until",
                value=discord.utils.format_dt(member.timed_out_until, "F"),
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="serverinfo", description="Show information about this server.")
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return await self._err(interaction, "Server-only command.")

        owner = guild.owner or await self.bot.fetch_user(guild.owner_id)
        created = discord.utils.format_dt(guild.created_at, "F")
        verification = str(guild.verification_level).replace("_", " ").title()

        embed = discord.Embed(
            title=guild.name,
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="ID", value=f"`{guild.id}`", inline=True)
        embed.add_field(name="Owner", value=str(owner), inline=True)
        embed.add_field(name="Created", value=created, inline=False)
        embed.add_field(name="Members", value=str(guild.member_count), inline=True)
        embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(name="Channels", value=str(len(guild.channels)), inline=True)
        embed.add_field(name="Boost Level", value=f"Level {guild.premium_tier}", inline=True)
        embed.add_field(name="Boosts", value=str(guild.premium_subscription_count or 0), inline=True)
        embed.add_field(name="Verification", value=verification, inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="permissions",
        description="Show the bot's effective permissions in this guild/channel.",
    )
    async def permissions(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        channel = interaction.channel
        if not guild:
            return await self._err(interaction, "Server-only command.")

        bot_member = guild.me
        guild_perms = bot_member.guild_permissions
        channel_perms = (
            channel.permissions_for(bot_member)
            if isinstance(channel, discord.abc.GuildChannel)
            else None
        )

        # Core permissions we care about
        core_flags = [
            "send_messages", "embed_links", "attach_files", "read_message_history",
            "manage_messages", "ban_members", "kick_members", "moderate_members",
            "manage_channels", "view_channel", "view_audit_log",
        ]

        lines = []
        for flag in core_flags:
            guild_val = getattr(guild_perms, flag, False)
            ch_val = getattr(channel_perms, flag, None) if channel_perms else None
            status = "✅" if guild_val else "❌"
            ch_note = ""
            if ch_val is not None and ch_val != guild_val:
                ch_note = f" (channel: {'✅' if ch_val else '❌'})"
            lines.append(f"{status} `{flag}`{ch_note}")

        embed = discord.Embed(
            title=f"Bot Permissions in {guild.name}",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Channel: #{getattr(channel, 'name', 'unknown')}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="checkaccess",
        description="Pre-flight check: would this moderation action succeed?",
    )
    @_is_staff_check()
    @app_commands.describe(
        action="Action to simulate (ban/kick/timeout/untimeout/purge).",
        target="Member to check against.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="ban", value="ban"),
        app_commands.Choice(name="kick", value="kick"),
        app_commands.Choice(name="timeout", value="timeout"),
        app_commands.Choice(name="untimeout", value="untimeout"),
        app_commands.Choice(name="purge", value="purge"),
    ])
    async def checkaccess(
        self,
        interaction: discord.Interaction,
        action: str,
        target: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return await self._err(interaction, "Server-only command.")

        perm_map = {
            "ban": "ban_members",
            "kick": "kick_members",
            "timeout": "moderate_members",
            "untimeout": "moderate_members",
            "purge": "manage_messages",
        }
        required_perm = perm_map.get(action, "ban_members")
        check = _preflight(guild, guild.me, target, required_perm)

        color = 0x34C759 if check.ok else 0xFF3B30
        status = "✅ Would succeed" if check.ok else f"❌ Would fail: {check.reason}"

        embed = discord.Embed(
            title=f"Pre-flight Check: {action.upper()} → {target}",
            description=status,
            color=discord.Color(color),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Action", value=action, inline=True)
        embed.add_field(name="Target", value=target.mention, inline=True)
        embed.add_field(name="Required Permission", value=f"`{required_perm}`", inline=True)
        bot_has = getattr(guild.me.guild_permissions, required_perm, False)
        embed.add_field(name="Bot Has Permission", value="✅ Yes" if bot_has else "❌ No", inline=True)

        hier_ok = guild.me.top_role > target.top_role
        embed.add_field(
            name="Hierarchy OK",
            value=f"✅ ({guild.me.top_role.name} > {target.top_role.name})"
            if hier_ok else f"❌ ({guild.me.top_role.name} ≤ {target.top_role.name})",
            inline=True,
        )
        embed.add_field(name="Is Guild Owner", value="⚠️ Yes — blocked" if target.id == guild.owner_id else "✅ No", inline=True)

        demo_note = " (Demo mode active — real action would still be intercepted)" if _is_demo(guild) else ""
        embed.set_footer(text=f"Non-destructive check — no action taken{demo_note}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ==================================================================
    # SECTION 2 — DIAGNOSTICS (Owner-only)
    # ==================================================================

    @app_commands.command(
        name="checkpermissions",
        description="[Owner] Show bot permission matrix across all channels in a guild.",
    )
    @_is_owner_check()
    @app_commands.describe(guild_id="Guild ID to inspect (defaults to current guild).")
    async def checkpermissions(
        self,
        interaction: discord.Interaction,
        guild_id: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        target_guild: Optional[discord.Guild] = None
        if guild_id:
            try:
                target_guild = self.bot.get_guild(int(guild_id))
            except ValueError:
                return await self._err(interaction, "guild_id must be numeric.")
        if not target_guild:
            target_guild = interaction.guild
        if not target_guild:
            return await self._err(interaction, "Could not resolve guild.")

        bot_member = target_guild.me
        key_perms = ["send_messages", "embed_links", "manage_messages", "view_channel"]

        lines = []
        for ch in sorted(target_guild.text_channels, key=lambda c: c.position)[:25]:
            p = ch.permissions_for(bot_member)
            flags = "".join(
                ("✅" if getattr(p, f, False) else "❌") for f in key_perms
            )
            lines.append(f"{flags} `#{ch.name}`")

        embed = discord.Embed(
            title=f"Permission Matrix — {target_guild.name}",
            description=(
                f"Legend: send | embed | manage_msg | view\n\n"
                + "\n".join(lines)
                + ("\n*…(showing first 25 channels)*" if len(target_guild.text_channels) > 25 else "")
            ),
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="checkcommands",
        description="[Owner] List all registered slash commands with access tiers.",
    )
    @_is_owner_check()
    async def checkcommands(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        lines = []
        for cmd in sorted(self.bot.tree.get_commands(), key=lambda c: c.name):
            checks = getattr(cmd, "checks", [])
            tier = "Public"
            for chk in checks:
                name = getattr(chk, "__name__", "") or getattr(chk, "__qualname__", "")
                if "owner" in name.lower():
                    tier = "Owner Only"
                    break
                if "staff" in name.lower() or "trusted" in name.lower():
                    tier = "Staff / Trusted"
                    break
            lines.append(f"`/{cmd.name}` — {tier}")

        embed = discord.Embed(
            title="Registered Slash Commands",
            description="\n".join(lines) or "No commands registered.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(lines)} command(s) total")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="diagnose",
        description="[Owner] Run automated system health check.",
    )
    @_is_owner_check()
    async def diagnose(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        results: list[str] = []

        # 1. Gateway intent check
        intents = self.bot.intents
        required_intents = {
            "guilds": intents.guilds,
            "members": intents.members,
            "message_content": intents.message_content,
        }
        for name, enabled in required_intents.items():
            results.append(f"{'✅' if enabled else '❌'} Intent: `{name}`")

        # 2. Guild permission audit
        missing_guilds: list[str] = []
        core_perms = ["send_messages", "embed_links", "read_message_history", "view_channel"]
        for guild in self.bot.guilds:
            bot_m = guild.me
            if bot_m is None:
                continue
            missing = [p for p in core_perms if not getattr(bot_m.guild_permissions, p, False)]
            if missing:
                missing_guilds.append(f"`{guild.name}`: missing {', '.join(missing)}")

        if missing_guilds:
            results.append(f"⚠️ Guilds with missing permissions ({len(missing_guilds)}):")
            results.extend(f"  • {g}" for g in missing_guilds[:10])
        else:
            results.append(f"✅ All {len(self.bot.guilds)} guild(s) have core permissions")

        # 3. WebSub status
        tracker = getattr(self.bot, "cogs", {})
        websub_active = bool(getattr(config, "WEBHOOK_URL", ""))
        results.append(f"{'✅' if websub_active else '🟡'} WebSub: {'Active' if websub_active else 'RSS fallback only'}")

        # 4. Demo mode
        results.append(
            f"{'🧪' if _DEMO_MODE else '✅'} Demo mode: {'ACTIVE (guild ' + str(_DEMO_GUILD_ID) + ')' if _DEMO_MODE else 'Disabled'}"
        )

        # 5. DB connectivity
        try:
            counts = db.table_counts()
            results.append(f"✅ Database: {sum(counts.values())} total rows across {len(counts)} tables")
        except Exception as exc:
            results.append(f"❌ Database error: {exc}")

        # 6. Command sync check
        registered = self.bot.tree.get_commands()
        results.append(f"✅ {len(registered)} command(s) in local tree")

        embed = discord.Embed(
            title="🔬 System Diagnostics",
            description="\n".join(results),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Python {sys.version.split()[0]} | discord.py {discord.__version__}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="testmoderation",
        description="[Owner] Dry-run all validation stages for a moderation action (no real action).",
    )
    @_is_owner_check()
    @app_commands.describe(
        action="Action to dry-run.",
        target="Target member.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="ban", value="ban"),
        app_commands.Choice(name="kick", value="kick"),
        app_commands.Choice(name="timeout", value="timeout"),
        app_commands.Choice(name="untimeout", value="untimeout"),
    ])
    async def testmoderation(
        self,
        interaction: discord.Interaction,
        action: str,
        target: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return await self._err(interaction, "Server-only command.")

        perm_map = {
            "ban": "ban_members",
            "kick": "kick_members",
            "timeout": "moderate_members",
            "untimeout": "moderate_members",
        }
        perm = perm_map.get(action, "ban_members")

        stages: list[str] = []

        # Stage 1: input validation
        stages.append("✅ Stage 1 — Input Validation: target resolved")

        # Stage 2: self-target check
        if target.id == self.bot.user.id:
            stages.append("❌ Stage 2 — Self-target: cannot target bot itself")
        else:
            stages.append("✅ Stage 2 — Self-target: OK")

        # Stage 3: guild owner check
        if target.id == guild.owner_id:
            stages.append("❌ Stage 3 — Guild owner protection: target is guild owner")
        else:
            stages.append("✅ Stage 3 — Guild owner protection: OK")

        # Stage 4: bot permission
        has_perm = getattr(guild.me.guild_permissions, perm, False)
        stages.append(f"{'✅' if has_perm else '❌'} Stage 4 — Bot permission `{perm}`: {'granted' if has_perm else 'MISSING'}")

        # Stage 5: hierarchy
        hier_ok = guild.me.top_role > target.top_role
        stages.append(
            f"{'✅' if hier_ok else '❌'} Stage 5 — Role hierarchy: "
            f"bot={guild.me.top_role.name} (pos {guild.me.top_role.position}) "
            f"vs target={target.top_role.name} (pos {target.top_role.position})"
        )

        all_ok = has_perm and hier_ok and target.id != guild.owner_id and target.id != self.bot.user.id
        embed = discord.Embed(
            title=f"🔬 Dry-Run: {action.upper()} → {target}",
            description="\n".join(stages),
            color=discord.Color(0x34C759 if all_ok else 0xFF3B30),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="DRY-RUN — No action was taken")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ==================================================================
    # SECTION 3 — BROADCAST (up to 10 channels)
    # ==================================================================

    @app_commands.command(
        name="broadcast",
        description="Send an announcement to up to 10 channels at once. Supports images and GIFs.",
    )
    @_is_staff_check()
    @app_commands.describe(
        message="Announcement text. Supports markdown and @mentions.",
        channel1="First target channel (required).",
        channel2="Second target channel.",
        channel3="Third target channel.",
        channel4="Fourth target channel.",
        channel5="Fifth target channel.",
        channel6="Sixth target channel.",
        channel7="Seventh target channel.",
        channel8="Eighth target channel.",
        channel9="Ninth target channel.",
        channel10="Tenth target channel.",
        image_url="Image or GIF URL (https:// only) to embed in the announcement.",
        ping_role="Role or @everyone/@here to ping.",
        embed="Send as a rich Discord embed (default: True).",
        title="Embed title (used when embed=True).",
    )
    async def broadcast(
        self,
        interaction: discord.Interaction,
        message: str,
        channel1: discord.TextChannel,
        channel2: Optional[discord.TextChannel] = None,
        channel3: Optional[discord.TextChannel] = None,
        channel4: Optional[discord.TextChannel] = None,
        channel5: Optional[discord.TextChannel] = None,
        channel6: Optional[discord.TextChannel] = None,
        channel7: Optional[discord.TextChannel] = None,
        channel8: Optional[discord.TextChannel] = None,
        channel9: Optional[discord.TextChannel] = None,
        channel10: Optional[discord.TextChannel] = None,
        image_url: Optional[str] = None,
        ping_role: Optional[str] = None,
        embed: bool = True,
        title: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return await self._err(interaction, "Server-only command.")

        # Validate image URL
        if image_url and not image_url.strip().lower().startswith("https://"):
            return await self._err(interaction, "Image URL must start with `https://`.")

        # Deduplicate channels, preserve order
        seen: set[int] = set()
        targets: list[discord.TextChannel] = []
        for ch in [channel1, channel2, channel3, channel4, channel5,
                   channel6, channel7, channel8, channel9, channel10]:
            if ch is not None and ch.id not in seen:
                seen.add(ch.id)
                targets.append(ch)

        # Build ping content
        ping_content = ""
        if ping_role:
            import re as _re
            raw = ping_role.strip().lower()
            if raw in {"@everyone", "everyone"}:
                ping_content = "@everyone"
            elif raw in {"@here", "here"}:
                ping_content = "@here"
            else:
                m = _re.fullmatch(r"<@&?(\d{15,21})>", ping_role.strip())
                if m:
                    ping_content = f"<@&{m.group(1)}>"
                else:
                    role_obj = discord.utils.find(
                        lambda r: r.name.lower() == raw.lstrip("@"),
                        guild.roles,
                    )
                    ping_content = role_obj.mention if role_obj else ""

        allowed_mentions = discord.AllowedMentions(
            everyone=ping_content in {"@everyone", "@here"},
            roles=bool(ping_content and ping_content.startswith("<@&")),
            users=False,
        )

        # Build embed or plain content
        embed_obj: Optional[discord.Embed] = None
        plain_content: str = ""

        if embed:
            embed_obj = discord.Embed(
                description=message[:4096],
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc),
            )
            if title:
                embed_obj.title = title[:256]
            if image_url:
                embed_obj.set_image(url=image_url)
            embed_obj.set_footer(text=f"Broadcast by {interaction.user.display_name}")
            plain_content = ping_content
        else:
            plain_content = f"{ping_content}\n{message}".strip() if ping_content else message
            if image_url:
                plain_content = f"{plain_content}\n{image_url}"

        # Send to each channel
        sent: list[str] = []
        failed: list[str] = []

        for ch in targets:
            try:
                kwargs: dict = {
                    "content": plain_content or None,
                    "allowed_mentions": allowed_mentions,
                }
                if embed_obj is not None:
                    kwargs["embed"] = embed_obj
                await ch.send(**kwargs)
                sent.append(ch.mention)
            except (discord.Forbidden, discord.HTTPException) as exc:
                failed.append(f"{ch.mention} — {exc}")
            # Small delay between sends to avoid hitting Discord rate limits
            await asyncio.sleep(0.5)

        result_lines = []
        if sent:
            result_lines.append(f"✅ Sent to {len(sent)} channel(s): {' '.join(sent)}")
        if failed:
            result_lines.append(f"❌ Failed ({len(failed)}):\n" + "\n".join(failed))

        await interaction.followup.send("\n".join(result_lines) or "Nothing sent.", ephemeral=True)

    # ==================================================================
    # SECTION 4 — TEST MESSAGE
    # ==================================================================

    @app_commands.command(
        name="testmessage",
        description="Send a test embed to the configured TEST_CHANNEL_ID.",
    )
    @_is_owner_check()
    @app_commands.describe(
        layout_type="Embed layout variant to render.",
        sample_image="Optional image/GIF URL to include in the test embed.",
    )
    @app_commands.choices(layout_type=[
        app_commands.Choice(name="standard", value="standard"),
        app_commands.Choice(name="announcement", value="announcement"),
        app_commands.Choice(name="alert", value="alert"),
        app_commands.Choice(name="youtube_notification", value="youtube_notification"),
    ])
    async def testmessage(
        self,
        interaction: discord.Interaction,
        layout_type: str = "standard",
        sample_image: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # Channel scoping
        if _TEST_CHANNEL_ID is None:
            return await self._err(
                interaction,
                "TEST_CHANNEL_ID is not set. Add it to your Railway environment variables.",
            )

        channel = self.bot.get_channel(_TEST_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(_TEST_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return await self._err(interaction, f"Could not access TEST_CHANNEL_ID `{_TEST_CHANNEL_ID}`.")

        if not isinstance(channel, discord.TextChannel):
            return await self._err(interaction, "TEST_CHANNEL_ID must point to a text channel.")

        # Cooldown: 1 per 30s per channel
        now = datetime.now(timezone.utc)
        last = self._testmessage_cooldowns.get(channel.id)
        if last and (now - last).total_seconds() < 30:
            remaining = 30 - int((now - last).total_seconds())
            return await self._err(interaction, f"Cooldown: wait **{remaining}s** before sending another test message.")

        self._testmessage_cooldowns[channel.id] = now

        # Build layout
        image = sample_image or "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"
        if sample_image and not sample_image.strip().lower().startswith("https://"):
            return await self._err(interaction, "Image URL must start with `https://`.")

        layouts = {
            "standard": discord.Embed(
                title="📋 Standard Layout Test",
                description="This is a **standard embed** with markdown.\n\n"
                            "> Block quote example\n"
                            "- Bullet item 1\n- Bullet item 2\n\n"
                            "Inline `code` and **bold** and *italic*.",
                color=discord.Color.blurple(),
            ),
            "announcement": discord.Embed(
                title="📢 Announcement Layout Test",
                description="This is how an **announcement** embed looks.\n\n"
                            "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
                            "Phasellus imperdiet, nulla et dictum interdum.",
                color=discord.Color(0x34C759),
            ),
            "alert": discord.Embed(
                title="⚠️ Alert Layout Test",
                description="This is an **alert** style embed.\n\nAction required!",
                color=discord.Color(0xFF3B30),
            ),
            "youtube_notification": discord.Embed(
                title="Test Channel",
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                description="**Test Video Title — Layout Preview**",
                color=discord.Color(0xFF0000),
            ),
        }

        embed_obj = layouts.get(layout_type, layouts["standard"])
        embed_obj.set_image(url=image)
        embed_obj.add_field(name="Field One", value="Value one — inline", inline=True)
        embed_obj.add_field(name="Field Two", value="Value two — inline", inline=True)
        embed_obj.add_field(name="Field Three", value="Value three — full width", inline=False)
        embed_obj.set_footer(
            text=f"Test message | Layout: {layout_type} | Requested by {interaction.user}",
        )
        embed_obj.timestamp = datetime.now(timezone.utc)

        view_placeholder = discord.ui.View(timeout=None)
        view_placeholder.add_item(
            discord.ui.Button(
                label="Example Link Button",
                style=discord.ButtonStyle.link,
                url="https://discord.com",
            )
        )

        try:
            sent_msg = await channel.send(
                content=f"🧪 **Test message** — layout: `{layout_type}`",
                embed=embed_obj,
                view=view_placeholder,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await self._err(interaction, f"Could not send test message: {exc}")

        # Attach delete button
        delete_view = DeleteTestMessageView(sent_msg)
        await interaction.followup.send(
            f"✅ Test message sent to {channel.mention}.",
            view=delete_view,
            ephemeral=True,
        )

    # ==================================================================
    # SECTION 5 — PREFIX COMMANDS (!ban, !kick, etc.)
    # All share the same pre-flight logic as the slash commands above.
    # Usage examples:
    #   !ban @user spamming
    #   !kick @user rule violation
    #   !timeout @user 30 being rude
    #   !untimeout @user
    #   !purge 20
    #   !purge 10 @user
    #   !userinfo @user
    #   !serverinfo
    # ==================================================================

    async def _prefix_is_staff(self, ctx: commands.Context) -> bool:
        """Return True if the ctx author has staff/owner access."""
        if not ctx.guild:
            return False
        owner_id = config.BOT_OWNER_ID
        if owner_id is None:
            try:
                app = await self.bot.application_info()
                owner_id = app.owner.id if app.owner else None
            except (discord.HTTPException, discord.Forbidden):
                owner_id = None
        return (
            ctx.author.id == owner_id
            or ctx.author.id == ctx.guild.owner_id
            or db.is_trusted_user(ctx.guild.id, ctx.author.id)
        )

    async def _prefix_err(self, ctx: commands.Context, msg: str) -> None:
        embed = discord.Embed(description=f"🚫 {msg}", color=discord.Color.red())
        await ctx.send(embed=embed)

    async def _prefix_ok(self, ctx: commands.Context, msg: str) -> None:
        embed = discord.Embed(description=msg, color=discord.Color(0x34C759))
        await ctx.send(embed=embed)

    @commands.command(name="ban", help="Ban a member. Usage: !ban @user [reason]")
    async def prefix_ban(
        self,
        ctx: commands.Context,
        target: discord.Member,
        *,
        reason: str = "No reason provided",
    ) -> None:
        if not await self._prefix_is_staff(ctx):
            return await self._prefix_err(ctx, "You need staff/owner access to use this command.")
        if not ctx.guild:
            return
        check = _preflight(ctx.guild, ctx.guild.me, target, "ban_members")
        if not check:
            return await self._prefix_err(ctx, check.reason)
        if _is_demo(ctx.guild):
            return await ctx.send(embed=_demo_embed("BAN", target, reason))
        await ctx.guild.ban(target, reason=f"{ctx.author} — {reason}", delete_message_days=0)
        await self._prefix_ok(ctx, f"✅ **{target}** has been banned.\nReason: {reason}")

    @commands.command(name="kick", help="Kick a member. Usage: !kick @user [reason]")
    async def prefix_kick(
        self,
        ctx: commands.Context,
        target: discord.Member,
        *,
        reason: str = "No reason provided",
    ) -> None:
        if not await self._prefix_is_staff(ctx):
            return await self._prefix_err(ctx, "You need staff/owner access to use this command.")
        if not ctx.guild:
            return
        check = _preflight(ctx.guild, ctx.guild.me, target, "kick_members")
        if not check:
            return await self._prefix_err(ctx, check.reason)
        if _is_demo(ctx.guild):
            return await ctx.send(embed=_demo_embed("KICK", target, reason))
        await ctx.guild.kick(target, reason=f"{ctx.author} — {reason}")
        await self._prefix_ok(ctx, f"✅ **{target}** has been kicked.\nReason: {reason}")

    @commands.command(name="timeout", help="Timeout a member. Usage: !timeout @user <minutes> [reason]")
    async def prefix_timeout(
        self,
        ctx: commands.Context,
        target: discord.Member,
        duration: int,
        *,
        reason: str = "No reason provided",
    ) -> None:
        if not await self._prefix_is_staff(ctx):
            return await self._prefix_err(ctx, "You need staff/owner access to use this command.")
        if not ctx.guild:
            return
        if not 1 <= duration <= 40320:
            return await self._prefix_err(ctx, "Duration must be between 1 and 40320 minutes (28 days).")
        check = _preflight(ctx.guild, ctx.guild.me, target, "moderate_members")
        if not check:
            return await self._prefix_err(ctx, check.reason)
        if _is_demo(ctx.guild):
            return await ctx.send(embed=_demo_embed(f"TIMEOUT ({duration}m)", target, reason))
        until = discord.utils.utcnow() + timedelta(minutes=duration)
        await target.timeout(until, reason=f"{ctx.author} — {reason}")
        await self._prefix_ok(ctx, f"✅ **{target}** timed out for **{duration} minute(s)**.\nReason: {reason}")

    @commands.command(name="untimeout", help="Remove timeout. Usage: !untimeout @user [reason]")
    async def prefix_untimeout(
        self,
        ctx: commands.Context,
        target: discord.Member,
        *,
        reason: str = "No reason provided",
    ) -> None:
        if not await self._prefix_is_staff(ctx):
            return await self._prefix_err(ctx, "You need staff/owner access to use this command.")
        if not ctx.guild:
            return
        check = _preflight(ctx.guild, ctx.guild.me, target, "moderate_members")
        if not check:
            return await self._prefix_err(ctx, check.reason)
        if target.timed_out_until is None:
            return await self._prefix_err(ctx, f"**{target}** is not currently timed out.")
        await target.timeout(None, reason=f"{ctx.author} — {reason}")
        await self._prefix_ok(ctx, f"✅ Timeout removed from **{target}**.\nReason: {reason}")

    @commands.command(name="purge", help="Bulk delete messages. Usage: !purge <amount> [@user]")
    async def prefix_purge(
        self,
        ctx: commands.Context,
        amount: int,
        filter_user: Optional[discord.Member] = None,
    ) -> None:
        if not await self._prefix_is_staff(ctx):
            return await self._prefix_err(ctx, "You need staff/owner access to use this command.")
        if not ctx.guild or not isinstance(ctx.channel, discord.TextChannel):
            return await self._prefix_err(ctx, "Must be used in a server text channel.")
        if not 1 <= amount <= 100:
            return await self._prefix_err(ctx, "Amount must be between 1 and 100.")
        if not ctx.guild.me.guild_permissions.manage_messages:
            return await self._prefix_err(ctx, "Bot is missing `manage_messages` permission.")
        if _is_demo(ctx.guild):
            embed = discord.Embed(
                title="🧪 [SIMULATED DEMO MODE — NO REAL ACTION TAKEN]",
                description=f"**Action:** PURGE {amount} messages"
                + (f" from {filter_user.mention}" if filter_user else ""),
                color=discord.Color.orange(),
            )
            return await ctx.send(embed=embed)

        # Delete the invoking command message too
        await ctx.message.delete()

        cutoff = discord.utils.utcnow() - timedelta(days=14)

        def check_msg(m: discord.Message) -> bool:
            if m.created_at < cutoff:
                return False
            if filter_user and m.author.id != filter_user.id:
                return False
            return True

        deleted = await ctx.channel.purge(limit=amount, check=check_msg)
        confirm = await ctx.send(
            embed=discord.Embed(
                description=f"✅ Deleted **{len(deleted)}** message(s)"
                + (f" from {filter_user.mention}" if filter_user else "")
                + ".\n*Messages older than 14 days were skipped.*",
                color=discord.Color(0x34C759),
            )
        )
        await asyncio.sleep(5)
        await confirm.delete()

    @commands.command(name="userinfo", help="Show member info. Usage: !userinfo [@user]")
    async def prefix_userinfo(
        self,
        ctx: commands.Context,
        target: Optional[discord.Member] = None,
    ) -> None:
        if not ctx.guild:
            return
        member = target or ctx.author
        if not isinstance(member, discord.Member):
            return await self._prefix_err(ctx, "Could not resolve member.")

        created = discord.utils.format_dt(member.created_at, "F")
        joined = discord.utils.format_dt(member.joined_at, "F") if member.joined_at else "Unknown"
        roles = [r.mention for r in reversed(member.roles) if r.name != "@everyone"]
        sorted_roles = sorted(
            [r for r in ctx.guild.roles if r.name != "@everyone"],
            key=lambda r: r.position, reverse=True,
        )
        rank = next(
            (i + 1 for i, r in enumerate(sorted_roles) if r in member.roles),
            len(sorted_roles),
        )
        flags = [f.name.replace("_", " ").title() for f, val in member.public_flags if val]

        embed = discord.Embed(
            title=str(member),
            color=member.color if member.color.value else discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Bot", value="Yes" if member.bot else "No", inline=True)
        embed.add_field(name="Hierarchy Rank", value=f"#{rank} of {len(sorted_roles)}", inline=True)
        embed.add_field(name="Account Created", value=created, inline=False)
        embed.add_field(name="Joined Server", value=joined, inline=False)
        embed.add_field(name="Roles", value=" ".join(roles[:20]) or "None", inline=False)
        if flags:
            embed.add_field(name="Badges", value=", ".join(flags), inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="serverinfo", help="Show server info. Usage: !serverinfo")
    async def prefix_serverinfo(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        guild = ctx.guild
        owner = guild.owner or await self.bot.fetch_user(guild.owner_id)
        embed = discord.Embed(
            title=guild.name,
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="ID", value=f"`{guild.id}`", inline=True)
        embed.add_field(name="Owner", value=str(owner), inline=True)
        embed.add_field(name="Members", value=str(guild.member_count), inline=True)
        embed.add_field(name="Boost Level", value=f"Level {guild.premium_tier}", inline=True)
        embed.add_field(name="Verification", value=str(guild.verification_level).title(), inline=True)
        await ctx.send(embed=embed)

    # Error handler for prefix command argument failures
    @prefix_ban.error
    @prefix_kick.error
    @prefix_timeout.error
    @prefix_untimeout.error
    @prefix_purge.error
    @prefix_userinfo.error
    async def prefix_mod_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.MemberNotFound):
            await self._prefix_err(ctx, f"Member not found: `{error.argument}`. Use a @mention or user ID.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await self._prefix_err(ctx, f"Missing argument: `{error.param.name}`. Check `!help {ctx.command.name}`.")
        elif isinstance(error, commands.BadArgument):
            await self._prefix_err(ctx, f"Invalid argument: {error}")
        else:
            await self._prefix_err(ctx, f"An error occurred: {error}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
