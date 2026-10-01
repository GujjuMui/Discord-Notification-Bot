"""Staff utility commands: /say (direct send) and /announcement (preview + confirm)."""

from __future__ import annotations

import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from cogs.server_logger import is_trusted_or_owner


# ---------------------------------------------------------------------------
# Announcement preview UI
# ---------------------------------------------------------------------------

class AnnouncementConfirmView(discord.ui.View):
    """Ephemeral preview view with Confirm / Cancel buttons."""

    def __init__(
        self,
        embed: discord.Embed,
        content: str,
        target_channel: discord.TextChannel,
        allowed_mentions: discord.AllowedMentions,
        author_id: int,
    ):
        super().__init__(timeout=120)
        self._embed = embed
        self._content = content
        self._target = target_channel
        self._allowed_mentions = allowed_mentions
        self._author_id = author_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                "This preview belongs to someone else.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Confirm & Post", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await self._target.send(
                content=self._content or None,
                embed=self._embed,
                allowed_mentions=self._allowed_mentions,
            )
            self.confirmed = True
            for child in self.children:
                child.disabled = True
            await interaction.followup.send(
                f"✅ Announcement posted to {self._target.mention}.", ephemeral=True
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.followup.send(
                f"❌ Could not post announcement: {exc}", ephemeral=True
            )
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Announcement cancelled.", view=self
        )
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        # Note: we cannot edit the original message here because we don't have
        # access to the interaction object after timeout. Discord will show the
        # buttons as non-interactive once the view expires on the client side.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_color(value: Optional[str]) -> discord.Color:
    if not value:
        return discord.Color.blurple()
    raw = value.strip().lower().replace("#", "").replace("0x", "")
    if not re.fullmatch(r"[0-9a-f]{6}", raw):
        raise ValueError("Color must be a 6-digit hex value such as #FF0000 or 0x007AFF.")
    return discord.Color(int(raw, 16))


def _build_allowed_mentions(content: str, ping_role: Optional[str]) -> discord.AllowedMentions:
    """Allow mentions that are actually present in content."""
    has_everyone = bool(re.search(r"(?<!\w)@(everyone|here)(?!\w)", content, re.I))
    has_roles = bool(re.search(r"<@&\d+>", content))
    has_users = bool(re.search(r"<@!?\d+>", content))
    return discord.AllowedMentions(
        everyone=has_everyone or bool(ping_role and ping_role.lower() in {"@everyone", "everyone", "@here", "here"}),
        roles=has_roles or bool(ping_role),
        users=has_users,
        replied_user=True,
    )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Utility(commands.Cog):
    """Authorized staff announcement and message commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _error(self, interaction: discord.Interaction, message: str) -> None:
        embed = discord.Embed(
            title="🚫 Error",
            description=message,
            color=discord.Color.red(),
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /say — simple, direct, current channel
    # ------------------------------------------------------------------

    @app_commands.command(
        name="say",
        description="Send a message as the bot in the current channel. Supports mentions, links, and attachments.",
    )
    @is_trusted_or_owner()
    @app_commands.describe(
        message="Message content. Raw mentions like <@user_id> and <@&role_id> work natively.",
        attachment="Optional file, image, or GIF to attach.",
    )
    async def say(
        self,
        interaction: discord.Interaction,
        message: str,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send(
                "This command can only be used in a server text channel.", ephemeral=True
            )
            return

        content = message.strip()
        if not content and not attachment:
            await interaction.followup.send(
                "Provide a message or attachment.", ephemeral=True
            )
            return

        if len(content) > 2000:
            await interaction.followup.send(
                "Message is over Discord's 2000-character limit.", ephemeral=True
            )
            return

        allowed_mentions = _build_allowed_mentions(content, None)
        file = await attachment.to_file() if attachment else None

        try:
            sent = await interaction.channel.send(
                content=content or None,
                file=file,
                allowed_mentions=allowed_mentions,
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.followup.send(f"❌ Could not send: {exc}", ephemeral=True)
            return

        # Audit log
        db.save_say_message(
            sent.id,
            interaction.guild.id,
            interaction.channel.id,
            interaction.user.id,
            "say",
        )
        server_logger = getattr(self.bot, "server_logger", None)
        if server_logger:
            try:
                await server_logger.log_say_event(
                    interaction.guild.id,
                    interaction.user,
                    interaction.channel.id,
                    content,
                    bool(attachment),
                )
            except Exception:
                pass

        await interaction.followup.send("✅ Message sent.", ephemeral=True)

    # ------------------------------------------------------------------
    # /announcement — advanced with interactive preview
    # ------------------------------------------------------------------

    @app_commands.command(
        name="announcement",
        description="Create a rich announcement with a live preview before posting.",
    )
    @is_trusted_or_owner()
    @app_commands.describe(
        target_channel="Channel to post the announcement in.",
        title="Embed title.",
        description="Main announcement body. Supports markdown and raw mentions.",
        color="Hex color code, e.g. #FF0000 or 0x007AFF.",
        ping_role="Role or @everyone/@here to ping alongside the announcement.",
        image_url="Optional image or GIF URL to display in the embed.",
        footer="Optional footer text.",
        button_label="Label for an optional link button.",
        button_url="URL for the optional link button (must start with https://).",
        anonymous="Hide the 'sent by' attribution in the footer. Default: true.",
    )
    async def announcement(
        self,
        interaction: discord.Interaction,
        target_channel: discord.TextChannel,
        title: str,
        description: str,
        color: Optional[str] = None,
        ping_role: Optional[str] = None,
        image_url: Optional[str] = None,
        footer: Optional[str] = None,
        button_label: Optional[str] = None,
        button_url: Optional[str] = None,
        anonymous: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild:
            await interaction.followup.send(
                "This command can only be used inside a server.", ephemeral=True
            )
            return

        # Validate inputs
        try:
            embed_color = _parse_color(color)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if button_url and not re.match(r"^https://", button_url, re.I):
            await interaction.followup.send(
                "Button URL must start with `https://` (plain http:// is not accepted by Discord).",
                ephemeral=True,
            )
            return

        if image_url and not re.match(r"^https://", image_url, re.I):
            await interaction.followup.send(
                "Image URL must start with `https://`.",
                ephemeral=True,
            )
            return

        if len(description) > 4096:
            await interaction.followup.send(
                "Description is over Discord's 4096-character embed limit.", ephemeral=True
            )
            return

        # Build ping content
        ping_content = ""
        if ping_role:
            raw = ping_role.strip().lower()
            if raw in {"@everyone", "everyone"}:
                ping_content = "@everyone"
            elif raw in {"@here", "here"}:
                ping_content = "@here"
            else:
                # Try to resolve as a role mention or name
                match = re.fullmatch(r"<@&?(\d{15,21})>", ping_role.strip())
                if match:
                    ping_content = f"<@&{match.group(1)}>"
                else:
                    role_obj = discord.utils.find(
                        lambda r: r.name.lower() == raw.lstrip("@"),
                        interaction.guild.roles,
                    )
                    if role_obj:
                        ping_content = role_obj.mention
                    else:
                        await interaction.followup.send(
                            f"Could not find role `{ping_role}`. Use a role mention, role name, @everyone, or @here.",
                            ephemeral=True,
                        )
                        return

        # Build embed
        embed = discord.Embed(
            title=title[:256],
            description=description,
            color=embed_color,
        )
        if image_url:
            embed.set_image(url=image_url)
        if footer:
            embed.set_footer(text=footer[:2048])
        elif not anonymous:
            embed.set_footer(
                text=f"📢 Announcement by {interaction.user.display_name}"
            )

        # Optional link button
        view = None
        if button_url:
            view = discord.ui.View(timeout=None)
            view.add_item(
                discord.ui.Button(
                    label=(button_label or "Open Link")[:80],
                    style=discord.ButtonStyle.link,
                    url=button_url,
                )
            )

        allowed_mentions = _build_allowed_mentions(ping_content, ping_role)

        # Build the preview confirm view
        confirm_view = AnnouncementConfirmView(
            embed=embed,
            content=ping_content,
            target_channel=target_channel,
            allowed_mentions=allowed_mentions,
            author_id=interaction.user.id,
        )

        preview_text = (
            f"**Preview of your announcement for {target_channel.mention}**\n"
            f"{'Pings: ' + ping_content if ping_content else 'No ping.'}\n\n"
            "Click **Confirm & Post** to broadcast, or **Cancel** to discard."
        )

        await interaction.followup.send(
            content=preview_text,
            embed=embed,
            view=confirm_view,
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /edit_say — edit a previously sent bot message
    # ------------------------------------------------------------------

    @app_commands.command(
        name="edit_say",
        description="Edit a message previously sent by /say or /announcement.",
    )
    @is_trusted_or_owner()
    @app_commands.describe(
        message_id="Message ID of the bot message to edit.",
        new_content="Replacement content.",
    )
    async def edit_say(
        self,
        interaction: discord.Interaction,
        message_id: str,
        new_content: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            message_id_int = int(message_id.strip())
        except ValueError:
            await interaction.followup.send(
                "message_id must be a Discord message ID (numeric).", ephemeral=True
            )
            return

        if not interaction.guild:
            await interaction.followup.send(
                "This command can only be used inside a server.", ephemeral=True
            )
            return

        record = db.get_say_message(message_id_int)
        if not record or int(record["guild_id"]) != interaction.guild.id:
            await interaction.followup.send(
                "That message is not registered as a bot announcement in this server.",
                ephemeral=True,
            )
            return

        if int(record["author_id"]) != interaction.user.id and not await self._authorized_override(interaction):
            await interaction.followup.send(
                "Only the staff member who created the announcement, the server owner, or bot owner can edit it.",
                ephemeral=True,
            )
            return

        channel = self.bot.get_channel(int(record["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched = await self.bot.fetch_channel(int(record["channel_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await interaction.followup.send(
                    "The original target channel is unavailable.", ephemeral=True
                )
                return
            if not isinstance(fetched, discord.TextChannel):
                await interaction.followup.send(
                    "The original target is not a text channel.", ephemeral=True
                )
                return
            channel = fetched

        try:
            target = await channel.fetch_message(message_id_int)
            if target.author.id != self.bot.user.id:
                await interaction.followup.send(
                    "That message is no longer authored by this bot.", ephemeral=True
                )
                return
            await target.edit(content=new_content[:2000])
            await interaction.followup.send("✅ Announcement edited.", ephemeral=True)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            await interaction.followup.send(
                f"Could not edit the message: {exc}", ephemeral=True
            )

    async def _authorized_override(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        if interaction.user.id == interaction.guild.owner_id:
            return True
        owner_id = getattr(self.bot, "bot_owner_id", None)
        if owner_id is not None and interaction.user.id == owner_id:
            return True
        return db.is_trusted_user(interaction.guild.id, interaction.user.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Utility(bot))
