"""Staff utility commands for controlled bot-authored announcements."""

from __future__ import annotations

import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from cogs.server_logger import is_trusted_or_owner
from utils.helpers import format_mentions, extract_mention_ids


def _parse_color(value: Optional[str]) -> discord.Color:
    if not value:
        return discord.Color.blurple()
    raw = value.strip().lower().replace("#", "").replace("0x", "")
    if not re.fullmatch(r"[0-9a-f]{6}", raw):
        raise ValueError("Color must be a 6-digit hex value such as #007AFF or 0x34C759.")
    return discord.Color(int(raw, 16))


def _resolve_ping(guild: discord.Guild, value: Optional[str]) -> tuple[str, discord.AllowedMentions]:
    if not value:
        return "", discord.AllowedMentions.none()

    raw = value.strip()
    lowered = raw.lower()
    if lowered in {"@everyone", "everyone"}:
        return "@everyone", discord.AllowedMentions(everyone=True, roles=True, users=True)
    if lowered in {"@here", "here"}:
        return "@here", discord.AllowedMentions(everyone=True, roles=True, users=True)

    match = re.fullmatch(r"<@&?(\d{15,21})>", raw)
    role = None
    if match:
        role = guild.get_role(int(match.group(1)))
    else:
        role = discord.utils.find(lambda item: item.name.lower() == lowered.lstrip("@"), guild.roles)

    if role is None:
        raise ValueError("Ping role not found. Use a role mention like <@&ROLE_ID>, the role name, @everyone, or @here.")
    return role.mention, discord.AllowedMentions(everyone=True, roles=True, users=True)


class SayEmbedView(discord.ui.View):
    def __init__(self, label: str, url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label=label[:80] or "Open Link", url=url))


class Utility(commands.Cog):
    """Authorized staff announcement and message-management commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _error(self, interaction: discord.Interaction, message: str) -> None:
        embed = discord.Embed(
            title="🚫 /say Error",
            description=message,
            color=discord.Color.red(),
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _get_target(
        self,
        interaction: discord.Interaction,
        target_channel: Optional[discord.TextChannel],
    ) -> discord.TextChannel:
        channel = target_channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Target must be a normal text channel.")
        return channel

    async def _send_say(
        self,
        interaction: discord.Interaction,
        *,
        message: str,
        target_channel: discord.TextChannel,
        attachment: Optional[discord.Attachment],
        media_url: Optional[str],
        embed_enabled: bool,
        embed_title: Optional[str],
        embed_color: Optional[str],
        ping_role: Optional[str],
        reply_to_message_id: Optional[str],
        anonymous: bool,
        command_name: str,
        target_user: Optional[discord.User] = None,
        footer: Optional[str] = None,
        button_label: Optional[str] = None,
        button_url: Optional[str] = None,
    ) -> discord.Message:
        message = format_mentions(message or "", interaction.guild)
        ping_text, allowed_mentions = _resolve_ping(interaction.guild, ping_role)
        content = message or ""

        # Build the direct-user mention explicitly from the Discord ID. This
        # avoids relying on cached member formatting and makes the mention
        # target unambiguous to Discord's mention parser.
        direct_user_mention = f"<@{target_user.id}>" if target_user else ""
        if direct_user_mention:
            content = f"{direct_user_mention} {content}".strip()
        if ping_text:
            content = f"{ping_text} {content}".strip()

        mention_users, mention_roles, mention_everyone = extract_mention_ids(content)
        allowed_mentions = discord.AllowedMentions(
            everyone=mention_everyone,
            roles=bool(mention_roles) or bool(ping_role),
            users=True if mention_users or target_user else False,
            replied_user=True,
        )

        if media_url and not embed_enabled:
            content = f"{content}\n{media_url}".strip()

        if not embed_enabled and not content and not attachment:
            raise ValueError("Provide message text, media_url, or an attachment.")

        if embed_enabled:
            if len(message) > 4096:
                raise ValueError("Embed message text is limited to 4096 characters.")
            embed = discord.Embed(
                title=embed_title or None,
                description=message or None,
                color=_parse_color(embed_color),
            )
            if media_url:
                embed.set_image(url=media_url)
            if footer:
                embed.set_footer(text=footer)
            elif not anonymous:
                embed.set_footer(text=f"📢 Announcement sent by {interaction.user.display_name}")

            view = None
            if button_url:
                if not re.match(r"^https?://", button_url, re.I):
                    raise ValueError("Button URL must start with http:// or https://.")
                view = SayEmbedView(button_label or "Open Link", button_url)
        else:
            if len(content) > 2000:
                raise ValueError("Discord message content is limited to 2000 characters.")
            embed = None
            view = None

        file = await attachment.to_file() if attachment else None

        reference = None
        if reply_to_message_id:
            try:
                message_id = int(reply_to_message_id.strip())
            except ValueError as exc:
                raise ValueError("reply_to_message_id must be a Discord message ID.") from exc
            try:
                referenced = await target_channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                raise ValueError("Could not find or access that message in the target channel.") from exc
            reference = referenced.to_reference(fail_if_not_exists=False)

        sent = await target_channel.send(
            content=content or None,
            embed=embed,
            file=file,
            view=view,
            reference=reference,
            mention_author=True if reference else None,
            allowed_mentions=allowed_mentions,
        )
        db.save_say_message(
            sent.id,
            interaction.guild.id,
            target_channel.id,
            interaction.user.id,
            command_name,
        )

        server_logger = getattr(self.bot, "server_logger", None)
        if server_logger:
            try:
                await server_logger.log_say_event(
                    interaction.guild.id,
                    interaction.user,
                    target_channel.id,
                    message,
                    bool(attachment or media_url),
                )
            except Exception:
                # Sending the announcement must not fail because audit logging failed.
                pass
        return sent

    @app_commands.command(name="say", description="Send an authorized bot announcement with media, embeds, mentions, or replies.")
    @is_trusted_or_owner()
    @app_commands.describe(
        message="Message text / Markdown / custom emoji / mention content.",
        target_channel="Destination channel. Defaults to the current channel.",
        attachment="Optional uploaded file, image, GIF, PDF, or document.",
        media_url="Optional image/GIF URL. Embedded when embed=True, otherwise appended.",
        embed="Send the message as a Discord Embed.",
        embed_title="Optional embed title.",
        embed_color="Hex color such as #007AFF or 0xFF0000.",
        ping_role="Role mention, @everyone, or @here.",
        target_user="Optional user to directly ping.",
        reply_to_message_id="Optional message ID to reply to.",
        anonymous="If false, adds a small staff-credit footer to embeds.",
    )
    async def say(
        self,
        interaction: discord.Interaction,
        message: str,
        target_channel: Optional[discord.TextChannel] = None,
        attachment: Optional[discord.Attachment] = None,
        media_url: Optional[str] = None,
        embed: bool = False,
        embed_title: Optional[str] = None,
        embed_color: Optional[str] = None,
        ping_role: Optional[str] = None,
        target_user: Optional[discord.User] = None,
        reply_to_message_id: Optional[str] = None,
        anonymous: bool = True,
    ) -> None:
        try:
            target = await self._get_target(interaction, target_channel)
            await self._send_say(
                interaction,
                message=message,
                target_channel=target,
                attachment=attachment,
                media_url=media_url,
                embed_enabled=embed,
                embed_title=embed_title,
                embed_color=embed_color,
                ping_role=ping_role,
                target_user=target_user,
                reply_to_message_id=reply_to_message_id,
                anonymous=anonymous,
                command_name="say",
            )
            await interaction.response.send_message("✅ Announcement sent.", ephemeral=True)
        except (ValueError, discord.Forbidden, discord.HTTPException) as exc:
            await self._error(interaction, str(exc))

    @app_commands.command(name="say_embed", description="Create a rich multi-field announcement embed.")
    @is_trusted_or_owner()
    @app_commands.describe(
        title="Embed title.",
        description="Embed description.",
        color="Hex color.",
        image_url="Optional image/GIF URL.",
        footer="Optional footer text.",
        button_label="Optional button label.",
        button_url="Optional button URL.",
        target_channel="Destination channel. Defaults to current channel.",
        target_user="Optional user to directly ping.",
    )
    async def say_embed(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        color: Optional[str] = None,
        image_url: Optional[str] = None,
        footer: Optional[str] = None,
        button_label: Optional[str] = None,
        button_url: Optional[str] = None,
        target_channel: Optional[discord.TextChannel] = None,
        target_user: Optional[discord.User] = None,
    ) -> None:
        try:
            target = await self._get_target(interaction, target_channel)
            await self._send_say(
                interaction,
                message=description,
                target_channel=target,
                attachment=None,
                media_url=image_url,
                embed_enabled=True,
                embed_title=title,
                embed_color=color,
                ping_role=None,
                target_user=target_user,
                reply_to_message_id=None,
                anonymous=True,
                command_name="say_embed",
                footer=footer,
                button_label=button_label,
                button_url=button_url,
            )
            await interaction.response.send_message("✅ Embed announcement sent.", ephemeral=True)
        except (ValueError, discord.Forbidden, discord.HTTPException) as exc:
            await self._error(interaction, str(exc))

    @app_commands.command(name="edit_say", description="Edit a message previously sent by /say or /say_embed.")
    @is_trusted_or_owner()
    @app_commands.describe(
        message_id="Message ID of the bot message to edit.",
        new_content="New message content.",
    )
    async def edit_say(
        self,
        interaction: discord.Interaction,
        message_id: str,
        new_content: str,
    ) -> None:
        try:
            message_id_int = int(message_id.strip())
        except ValueError:
            await self._error(interaction, "message_id must be a Discord message ID.")
            return

        record = db.get_say_message(message_id_int)
        if not record or int(record["guild_id"]) != interaction.guild.id:
            await self._error(interaction, "That message is not registered as a bot announcement in this server.")
            return
        if int(record["author_id"]) != interaction.user.id and not await self._authorized_override(interaction):
            await self._error(interaction, "Only the staff member who created the announcement, the server owner, or bot owner can edit it.")
            return

        channel = self.bot.get_channel(int(record["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched = await self.bot.fetch_channel(int(record["channel_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                await self._error(interaction, "The original target channel is unavailable.")
                return
            if not isinstance(fetched, discord.TextChannel):
                await self._error(interaction, "The original target is not a text channel.")
                return
            channel = fetched

        try:
            target = await channel.fetch_message(message_id_int)
            if target.author.id != self.bot.user.id:
                await self._error(interaction, "That message is no longer authored by this bot.")
                return
            await target.edit(content=new_content[:2000])
            await interaction.response.send_message("✅ Announcement edited.", ephemeral=True)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            await self._error(interaction, f"Could not edit the message: {exc}")

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
