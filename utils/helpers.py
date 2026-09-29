"""Shared Discord mention formatting helpers."""

from __future__ import annotations

import re
from typing import Optional

import discord


def format_mentions(text_content: str, guild: Optional[discord.Guild] = None) -> str:
    """Normalize Discord user/role/channel mention syntax and raw member IDs.

    Existing Discord mentions are preserved. Raw numeric IDs are converted only
    when they resolve to a guild member, preventing accidental channel/role IDs
    from becoming user mentions. Basic @name/display-name input is also resolved
    when a matching guild member is available.
    """
    text = str(text_content or "")
    if guild is None:
        return text

    def raw_id(match: re.Match[str]) -> str:
        value = int(match.group(1))
        member = guild.get_member(value)
        return member.mention if member else match.group(0)

    text = re.sub(r"(?<![\\d<@&])\\b(\\d{15,21})\\b(?!\\d)", raw_id, text)

    def handle(match: re.Match[str]) -> str:
        handle = match.group(1)
        lowered = handle.casefold()
        member = discord.utils.find(
            lambda m: m.name.casefold() == lowered
            or m.display_name.casefold() == lowered,
            guild.members,
        )
        return member.mention if member else match.group(0)

    text = re.sub(r"(?<![\\w<@])@([A-Za-z0-9_.-]{2,32})\\b", handle, text)
    return text


def extract_mention_ids(text_content: str) -> tuple[list[int], list[int], bool]:
    """Return user IDs, role IDs, and whether @everyone/@here occurs."""
    text = str(text_content or "")
    users = [int(value) for value in re.findall(r"<@!?(\\d{15,21})>", text)]
    roles = [int(value) for value in re.findall(r"<@&(\\d{15,21})>", text)]
    everyone = bool(re.search(r"(?<!\\w)@(everyone|here)(?!\\w)", text, re.I))
    return list(dict.fromkeys(users)), list(dict.fromkeys(roles)), everyone
