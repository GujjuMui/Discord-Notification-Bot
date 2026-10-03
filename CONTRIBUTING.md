# Contributing to Discord Notification Bot

Thanks for your interest in contributing. Here's how to get set up.

## Local Setup

```bash
git clone https://github.com/GujjuMui/Discord-Notification-Bot.git
cd Discord-Notification-Bot
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
cp .env.example .env        # Fill in DISCORD_BOT_TOKEN
python main.py
```

> For WebSub testing locally, use [ngrok](https://ngrok.com): `ngrok http 8080` and set `WEBHOOK_URL=https://xxxx.ngrok-free.app/youtube/webhook` in `.env`.

## Code Style

- Python 3.11+ with `from __future__ import annotations`
- Type hints on all function signatures
- All slash commands must call `await interaction.response.defer(ephemeral=True)` as the first `await` — before any async I/O or DB calls
- No bare `except:` — always catch specific exceptions or `Exception` with logging
- All outbound HTTP must go through `_fetch()` or the session directly — never subprocess/curl
- Discord notification sends must pass explicit `allowed_mentions`

## Channel Resolver Rules

- `CHANNEL_ID_PATTERNS` order is load-bearing — `externalId` and `itemprop=channelId` must come before the generic `"channelId":"UC..."` JSON key
- Never add a new pattern that matches connected/brand account IDs before the page-owner patterns
- The WebSub push handler must validate `channel_id` from the payload against `db.get_yt_monitored_channels()` before calling `_classify_and_build_item` — this is the connected-account guard

## Branch Naming

- `feat/description` — new features
- `fix/description` — bug fixes
- `chore/description` — maintenance, docs, refactoring

## Pull Request Checklist

- [ ] No secrets or `.env` files committed
- [ ] `requirements.txt` updated if new dependencies added
- [ ] All slash commands defer before async I/O
- [ ] No HTML scrapers — WebSub push + RSS fallback only
- [ ] `CHANNEL_ID_PATTERNS` order preserved (page-owner patterns first)
- [ ] WebSub push handler guard check intact (reject untracked channel IDs before classification)
- [ ] Tested locally with a real Discord bot token

## After Deploying

Run `/sync` once after any deploy that adds or changes slash commands. `SYNC_COMMANDS=false` is the default to avoid Discord 429 rate limits on every restart.

## Reporting Bugs

Open an issue at https://github.com/GujjuMui/Discord-Notification-Bot/issues with:
- Railway deploy logs (redact your bot token)
- The exact slash command or action that triggered the bug
- Expected vs actual behaviour
