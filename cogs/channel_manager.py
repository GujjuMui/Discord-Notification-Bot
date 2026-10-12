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
    async def leaveguild(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        guilds = sorted(self.bot.guilds, key=lambda g: g.name.lower())
        if not guilds:
            await interaction.followup.send("Bot is not in any guilds.", ephemeral=True)
            return

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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChannelManager(bot))
