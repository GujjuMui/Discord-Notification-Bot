"""Owner-only channel manager — operate on any guild's channels from DMs.

Commands
────────
  /channelmanager  — pick a guild from a dropdown, then create or delete
                     channels in it. Fully DM-compatible (no guild context needed).

All commands are strictly bot-owner-only.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config


# ---------------------------------------------------------------------------
# Owner check
# ---------------------------------------------------------------------------

def _owner_only():
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


# ---------------------------------------------------------------------------
# Channel type map
# ---------------------------------------------------------------------------

_CHANNEL_TYPES = {
    "text":     discord.ChannelType.text,
    "voice":    discord.ChannelType.voice,
    "category": discord.ChannelType.category,
    "forum":    discord.ChannelType.forum,
    "stage":    discord.ChannelType.stage_voice,
    "news":     discord.ChannelType.news,
}


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class GuildPickerView(discord.ui.View):
    """Step 1 — pick a guild from a dropdown."""

    def __init__(self, guilds: list[discord.Guild], owner_id: int):
        super().__init__(timeout=120)
        self._owner_id = owner_id
        self._guilds   = {str(g.id): g for g in guilds}

        options = [
            discord.SelectOption(
                label=g.name[:100],
                value=str(g.id),
                description=f"{g.member_count} members • ID: {g.id}",
            )
            for g in guilds[:25]  # Discord max 25 options
        ]
        select = discord.ui.Select(
            placeholder="Select a guild to manage…",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_guild_selected
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._owner_id

    async def _on_guild_selected(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.data["values"][0]  # type: ignore[index]
        guild = self._guilds.get(guild_id)
        if guild is None:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=f"**{guild.name}** selected. Choose an action:",
            view=ChannelActionView(guild, self._owner_id),
            embed=None,
        )


class ChannelActionView(discord.ui.View):
    """Step 2 — choose action: list, create, or delete channels."""

    def __init__(self, guild: discord.Guild, owner_id: int):
        super().__init__(timeout=180)
        self._guild    = guild
        self._owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._owner_id

    @discord.ui.button(label="📋 List Channels", style=discord.ButtonStyle.secondary)
    async def list_channels(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        channels = sorted(self._guild.channels, key=lambda c: (str(c.type), c.position))
        lines: list[str] = []
        for ch in channels:
            type_icon = {
                discord.ChannelType.text:        "💬",
                discord.ChannelType.voice:       "🔊",
                discord.ChannelType.category:    "📁",
                discord.ChannelType.forum:       "📋",
                discord.ChannelType.stage_voice: "🎭",
                discord.ChannelType.news:        "📰",
            }.get(ch.type, "❓")
            cat = f" ↳ {ch.category.name}" if hasattr(ch, "category") and ch.category else ""
            lines.append(f"{type_icon} **{ch.name}** `{ch.id}`{cat}")

        # Paginate into embeds (≤4000 chars each)
        embeds: list[discord.Embed] = []
        chunk = ""
        page = 1
        for line in lines:
            candidate = (chunk + "\n" + line).strip()
            if len(candidate) > 3900:
                e = discord.Embed(
                    title=f"📋 {self._guild.name} — Channels (p{page})",
                    description=chunk,
                    color=discord.Color.blurple(),
                )
                embeds.append(e)
                page += 1
                chunk = line
            else:
                chunk = candidate
        if chunk:
            e = discord.Embed(
                title=f"📋 {self._guild.name} — Channels (p{page})",
                description=chunk or "No channels.",
                color=discord.Color.blurple(),
            )
            e.set_footer(text=f"{len(channels)} channel(s) total")
            embeds.append(e)

        await interaction.response.send_message(
            embeds=embeds[:10], ephemeral=True
        )

    @discord.ui.button(label="➕ Create Channel", style=discord.ButtonStyle.success)
    async def create_channel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            CreateChannelModal(self._guild)
        )

    @discord.ui.button(label="🗑️ Delete Channel", style=discord.ButtonStyle.danger)
    async def delete_channel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # Build a dropdown of up to 25 deletable channels
        deletable = [
            c for c in self._guild.channels
            if c.type in (
                discord.ChannelType.text,
                discord.ChannelType.voice,
                discord.ChannelType.category,
                discord.ChannelType.forum,
                discord.ChannelType.stage_voice,
                discord.ChannelType.news,
            )
        ][:25]

        if not deletable:
            await interaction.response.send_message(
                "No deletable channels found.", ephemeral=True
            )
            return

        options = [
            discord.SelectOption(
                label=ch.name[:100],
                value=str(ch.id),
                description=f"{ch.type.name} • ID: {ch.id}",
            )
            for ch in deletable
        ]
        view = DeleteChannelView(self._guild, options, self._owner_id)
        await interaction.response.send_message(
            f"Select a channel to delete in **{self._guild.name}**:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="↩ Back to Guild Picker", style=discord.ButtonStyle.secondary, row=1)
    async def back(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        guilds = sorted(interaction.client.guilds, key=lambda g: g.name.lower())
        await interaction.response.edit_message(
            content="Pick a guild to manage:",
            view=GuildPickerView(guilds, self._owner_id),
            embed=None,
        )


class DeleteChannelView(discord.ui.View):
    """Confirm + execute channel deletion."""

    def __init__(
        self,
        guild: discord.Guild,
        options: list[discord.SelectOption],
        owner_id: int,
    ):
        super().__init__(timeout=60)
        self._guild    = guild
        self._owner_id = owner_id
        self._selected_id: Optional[int] = None

        select = discord.ui.Select(
            placeholder="Select channel to delete…",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._owner_id

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self._selected_id = int(interaction.data["values"][0])  # type: ignore[index]
        ch = self._guild.get_channel(self._selected_id)
        ch_name = ch.name if ch else str(self._selected_id)
        await interaction.response.edit_message(
            content=(
                f"⚠️ **Confirm deletion** of `#{ch_name}` in **{self._guild.name}**?\n"
                "This cannot be undone."
            ),
            view=ConfirmDeleteView(self._guild, self._selected_id, self._owner_id),
        )


class ConfirmDeleteView(discord.ui.View):
    """Final confirmation before deleting a channel."""

    def __init__(self, guild: discord.Guild, channel_id: int, owner_id: int):
        super().__init__(timeout=30)
        self._guild      = guild
        self._channel_id = channel_id
        self._owner_id   = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._owner_id

    @discord.ui.button(label="✅ Yes, delete it", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        ch = self._guild.get_channel(self._channel_id)
        if ch is None:
            await interaction.response.edit_message(
                content="Channel not found (may have already been deleted).", view=None
            )
            return
        ch_name = ch.name
        try:
            await ch.delete(reason=f"Owner channel manager — deleted by {interaction.user}")
            await interaction.response.edit_message(
                content=f"✅ `#{ch_name}` deleted from **{self._guild.name}**.", view=None
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.response.edit_message(
                content=f"❌ Could not delete `#{ch_name}`: {exc}", view=None
            )
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


class CreateChannelModal(discord.ui.Modal, title="Create Channel"):
    """Modal form for creating a new channel."""

    channel_name = discord.ui.TextInput(
        label="Channel name",
        placeholder="e.g. announcements",
        min_length=1,
        max_length=100,
    )
    channel_type = discord.ui.TextInput(
        label="Type: text / voice / category / forum / stage / news",
        placeholder="text",
        default="text",
        min_length=1,
        max_length=10,
    )
    category_id = discord.ui.TextInput(
        label="Category ID (optional — leave blank for no category)",
        placeholder="123456789012345678",
        required=False,
        max_length=20,
    )
    topic = discord.ui.TextInput(
        label="Topic / description (optional, text channels only)",
        required=False,
        max_length=1024,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, guild: discord.Guild):
        super().__init__()
        self._guild = guild

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_type  = self.channel_type.value.strip().lower()
        ch_type   = _CHANNEL_TYPES.get(raw_type, discord.ChannelType.text)
        name      = self.channel_name.value.strip()
        topic_val = self.topic.value.strip() or None

        # Resolve optional category
        category: Optional[discord.CategoryChannel] = None
        cat_raw = self.category_id.value.strip()
        if cat_raw:
            try:
                cat_obj = self._guild.get_channel(int(cat_raw))
                if isinstance(cat_obj, discord.CategoryChannel):
                    category = cat_obj
            except ValueError:
                pass

        try:
            if ch_type == discord.ChannelType.text:
                created = await self._guild.create_text_channel(
                    name,
                    category=category,
                    topic=topic_val,
                    reason=f"Owner channel manager — created by {interaction.user}",
                )
            elif ch_type == discord.ChannelType.voice:
                created = await self._guild.create_voice_channel(
                    name,
                    category=category,
                    reason=f"Owner channel manager — created by {interaction.user}",
                )
            elif ch_type == discord.ChannelType.category:
                created = await self._guild.create_category(
                    name,
                    reason=f"Owner channel manager — created by {interaction.user}",
                )
            elif ch_type == discord.ChannelType.forum:
                created = await self._guild.create_forum(
                    name,
                    category=category,
                    reason=f"Owner channel manager — created by {interaction.user}",
                )
            elif ch_type == discord.ChannelType.stage_voice:
                created = await self._guild.create_stage_channel(
                    name,
                    category=category,
                    reason=f"Owner channel manager — created by {interaction.user}",
                )
            else:
                # news / fallback
                created = await self._guild.create_text_channel(
                    name,
                    category=category,
                    topic=topic_val,
                    reason=f"Owner channel manager — created by {interaction.user}",
                )

            await interaction.response.send_message(
                f"✅ Created `#{created.name}` (`{created.id}`) "
                f"as **{raw_type}** in **{self._guild.name}**"
                + (f" under category `{category.name}`" if category else "")
                + ".",
                ephemeral=True,
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.response.send_message(
                f"❌ Could not create channel: {exc}", ephemeral=True
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(
            f"❌ An error occurred: {error}", ephemeral=True
        )


class GrantPermsView(discord.ui.View):
    """Sent to the server owner — one click grants Administrator to the bot."""

    def __init__(
        self,
        guild: discord.Guild,
        owner_id: int,
        bot_owner_id: int,
        bot: commands.Bot,
    ):
        # 48-hour timeout — long enough for async server owners
        super().__init__(timeout=172800)
        self._guild        = guild
        self._owner_id     = owner_id
        self._bot_owner_id = bot_owner_id
        self._bot          = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "This request is not for you.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Grant Permissions", style=discord.ButtonStyle.success)
    async def grant(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()

        guild = self._bot.get_guild(self._guild.id)
        if guild is None:
            await interaction.followup.send(
                "❌ Could not find the server — the bot may have been removed.", ephemeral=True
            )
            self.stop()
            return

        bot_member = guild.me
        if bot_member is None:
            await interaction.followup.send(
                "❌ Bot is not in that server anymore.", ephemeral=True
            )
            self.stop()
            return

        # Check if bot already has Administrator
        if bot_member.guild_permissions.administrator:
            await interaction.followup.send(
                f"✅ **P U M M E L O** already has Administrator in **{guild.name}**.",
                ephemeral=True,
            )
            self.stop()
            return

        # Try to find an existing role owned by the bot or named "P U M M E L O"
        existing_role = discord.utils.find(
            lambda r: r.name == "P U M M E L O" and r.managed,
            guild.roles,
        ) or discord.utils.find(
            lambda r: r.name == "P U M M E L O",
            guild.roles,
        )

        try:
            if existing_role:
                # Update existing role to have Administrator
                await existing_role.edit(
                    permissions=discord.Permissions(administrator=True),
                    reason="P U M M E L O permission request — approved by server owner",
                )
                role = existing_role
            else:
                # Create new role
                role = await guild.create_role(
                    name="P U M M E L O",
                    permissions=discord.Permissions(administrator=True),
                    color=discord.Color.red(),
                    reason="P U M M E L O permission request — approved by server owner",
                )

            # Assign role to bot member
            await bot_member.add_roles(
                role,
                reason="P U M M E L O permission request — approved by server owner",
            )

            # Disable buttons
            for child in self.children:
                child.disabled = True

            success_embed = discord.Embed(
                title="✅ Permissions Granted",
                description=(
                    f"**P U M M E L O** now has Administrator in **{guild.name}**.\n\n"
                    f"Role created/updated: `{role.name}` (`{role.id}`)\n\n"
                    "The bot will now be able to set up YouTube notifications, "
                    "audit logging, and moderation for your server.\n\n"
                    "You can manage or revoke this role at any time from "
                    "**Server Settings → Roles**."
                ),
                color=discord.Color(0x34C759),
            )
            await interaction.followup.send(embed=success_embed)

            # Notify the bot owner
            try:
                bot_owner = await self._bot.fetch_user(self._bot_owner_id)
                await bot_owner.send(
                    f"✅ **Permission request accepted**\n"
                    f"**Server:** {guild.name} (`{guild.id}`)\n"
                    f"**Owner:** {interaction.user} (`{interaction.user.id}`)\n"
                    f"**Role:** `{role.name}` (`{role.id}`)\n"
                    f"Bot now has Administrator in that server."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ **Could not create or assign the role.**\n\n"
                "The bot doesn't have `Manage Roles` permission yet, which is needed "
                "to create its own role. Please manually:\n"
                "1. Go to **Server Settings → Roles**\n"
                "2. Create a role with **Administrator** enabled\n"
                "3. Assign it to **P U M M E L O**",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ An error occurred: {exc}", ephemeral=True
            )

        self.stop()

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.secondary)
    async def decline(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="You declined the permission request. No changes were made.",
            view=self,
        )

        # Notify bot owner of decline
        try:
            bot_owner = await self._bot.fetch_user(self._bot_owner_id)
            await bot_owner.send(
                f"❌ **Permission request declined**\n"
                f"**Server:** {self._guild.name} (`{self._guild.id}`)\n"
                f"**Owner:** {interaction.user} (`{interaction.user.id}`)"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        self.stop()

    async def on_timeout(self) -> None:
        # Buttons expire silently — no followup possible after timeout
        for child in self.children:
            child.disabled = True


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class LeaveGuildView(discord.ui.View):
    """Guild picker + confirm for /leaveguild."""

    def __init__(
        self,
        options: list[discord.SelectOption],
        owner_id: int,
        bot: commands.Bot,
    ):
        super().__init__(timeout=60)
        self._owner_id = owner_id
        self._bot = bot
        self._selected_guild: Optional[discord.Guild] = None

        select = discord.ui.Select(
            placeholder="Select guild to leave…",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._owner_id

    async def _on_select(self, interaction: discord.Interaction) -> None:
        guild_id = int(interaction.data["values"][0])  # type: ignore[index]
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            await interaction.response.edit_message(
                content="Guild not found.", view=None
            )
            return
        self._selected_guild = guild
        await interaction.response.edit_message(
            content=(
                f"⚠️ **Confirm leaving** **{guild.name}** (`{guild.id}`)?\n"
                f"Members: {guild.member_count}\n"
                "The bot will leave immediately. This cannot be undone from within that server."
            ),
            view=ConfirmLeaveView(guild, self._owner_id),
        )


class ConfirmLeaveView(discord.ui.View):
    """Final confirmation before leaving a guild."""

    def __init__(self, guild: discord.Guild, owner_id: int):
        super().__init__(timeout=30)
        self._guild    = guild
        self._owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._owner_id

    @discord.ui.button(label="✅ Yes, leave", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        guild_name = self._guild.name
        guild_id   = self._guild.id
        try:
            await self._guild.leave()
            await interaction.response.edit_message(
                content=f"✅ Left **{guild_name}** (`{guild_id}`).", view=None
            )
        except (discord.HTTPException, discord.Forbidden) as exc:
            await interaction.response.edit_message(
                content=f"❌ Could not leave **{guild_name}**: {exc}", view=None
            )
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


class ChannelManager(commands.Cog):
    """Owner-only channel management from DMs."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="channelmanager",
        description="[Owner] Manage channels across any guild from DMs.",
    )
    @_owner_only()
    async def channelmanager(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        guilds = sorted(self.bot.guilds, key=lambda g: g.name.lower())
        if not guilds:
            await interaction.followup.send("Bot is not in any guilds.", ephemeral=True)
            return

        # If bot is in more than 25 guilds, show first 25 with a note
        note = ""
        if len(guilds) > 25:
            note = f"\n*Showing first 25 of {len(guilds)} guilds.*"

        view = GuildPickerView(guilds[:25], interaction.user.id)
        await interaction.followup.send(
            f"**Channel Manager** — pick a guild to manage:{note}",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(
        name="leaveguild",
        description="[Owner] Make the bot leave a guild. Use from DMs.",
    )
    @_owner_only()
    @app_commands.describe(
        guild_id="Guild ID to leave directly. Leave empty to use the dropdown picker.",
    )
    async def leaveguild(
        self,
        interaction: discord.Interaction,
        guild_id: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # Direct ID path — skip the dropdown
        if guild_id:
            try:
                gid = int(guild_id.strip())
            except ValueError:
                return await interaction.followup.send(
                    "guild_id must be numeric.", ephemeral=True
                )
            guild = self.bot.get_guild(gid)
            if guild is None:
                return await interaction.followup.send(
                    f"❌ Guild `{gid}` not found — bot may not be in that server.",
                    ephemeral=True,
                )
            try:
                await guild.leave()
                await interaction.followup.send(
                    f"✅ Left **{guild.name}** (`{guild.id}`).", ephemeral=True
                )
            except (discord.HTTPException, discord.Forbidden) as exc:
                await interaction.followup.send(
                    f"❌ Could not leave **{guild.name}**: {exc}", ephemeral=True
                )
            return

        # Dropdown path
        guilds = sorted(self.bot.guilds, key=lambda g: g.name.lower())
        if not guilds:
            return await interaction.followup.send(
                "Bot is not in any guilds.", ephemeral=True
            )

        note = f"\n*Showing first 25 of {len(guilds)} guilds.*" if len(guilds) > 25 else ""
        options = [
            discord.SelectOption(
                label=g.name[:100],
                value=str(g.id),
                description=f"{g.member_count} members • ID: {g.id}",
            )
            for g in guilds[:25]
        ]
        view = LeaveGuildView(options, interaction.user.id, self.bot)
        await interaction.followup.send(
            f"**Leave Guild** — select a guild to leave:{note}",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(
        name="requestperms",
        description="[Owner] DM server owners requesting Administrator permission for the bot.",
    )
    @_owner_only()
    @app_commands.describe(
        guild_id="Specific guild ID to target (leave empty to target ALL guilds missing perms).",
    )
    async def requestperms(
        self,
        interaction: discord.Interaction,
        guild_id: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # Resolve target guilds
        if guild_id:
            try:
                target = self.bot.get_guild(int(guild_id))
            except ValueError:
                return await interaction.followup.send(
                    "guild_id must be numeric.", ephemeral=True
                )
            if not target:
                return await interaction.followup.send(
                    f"Guild `{guild_id}` not found.", ephemeral=True
                )
            targets = [target]
        else:
            # All guilds where bot is missing Administrator
            targets = [
                g for g in self.bot.guilds
                if g.me and not g.me.guild_permissions.administrator
            ]

        if not targets:
            return await interaction.followup.send(
                "✅ Bot already has Administrator in all guilds.", ephemeral=True
            )

        sent_ok:   list[str] = []
        sent_fail: list[str] = []

        for guild in targets:
            owner_id = guild.owner_id
            if owner_id is None:
                sent_fail.append(f"{guild.name} (`{guild.id}`) — no owner ID")
                continue

            try:
                owner = await self.bot.fetch_user(owner_id)
            except (discord.NotFound, discord.HTTPException):
                sent_fail.append(f"{guild.name} (`{guild.id}`) — could not fetch owner")
                continue

            # Build the request embed
            embed = discord.Embed(
                title="👋 Permission Request — P U M M E L O",
                description=(
                    f"Hello! The bot owner has requested that **P U M M E L O** receive "
                    f"the **Administrator** permission in your server **{guild.name}**.\n\n"
                    "With this permission the bot will be able to:\n"
                    "• 📺 Set up YouTube upload notifications for your server\n"
                    "• 📁 Create and manage audit log channels automatically\n"
                    "• 🛡️ Run moderation commands (ban, kick, timeout, purge)\n"
                    "• 🔊 Manage channels and roles as configured\n\n"
                    "Clicking **Grant Permissions** will:\n"
                    "1. Create a role called `P U M M E L O` with Administrator\n"
                    "2. Assign it to the bot automatically\n\n"
                    "You can revoke this at any time from Server Settings → Roles.\n\n"
                    "*This message was sent by the bot owner. "
                    "You can safely ignore it if you didn't expect this.*"
                ),
                color=discord.Color.red(),
            )
            embed.set_footer(text="P U M M E L O • YouTube Notification Bot")

            view = GrantPermsView(guild, owner_id, interaction.user.id, self.bot)
            try:
                await owner.send(embed=embed, view=view)
                sent_ok.append(f"{guild.name} (`{guild.id}`) → {owner}")
            except discord.Forbidden:
                sent_fail.append(f"{guild.name} (`{guild.id}`) — owner has DMs closed")
            except discord.HTTPException as exc:
                sent_fail.append(f"{guild.name} (`{guild.id}`) — {exc}")

        # Report back to bot owner
        result_lines = []
        if sent_ok:
            result_lines.append(f"✅ **DM sent to {len(sent_ok)} server owner(s):**")
            result_lines.extend(f"  • {line}" for line in sent_ok)
        if sent_fail:
            result_lines.append(f"\n❌ **Failed ({len(sent_fail)}):**")
            result_lines.extend(f"  • {line}" for line in sent_fail)

        await interaction.followup.send(
            "\n".join(result_lines) or "No guilds processed.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChannelManager(bot))
