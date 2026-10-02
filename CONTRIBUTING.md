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

## Code Style

- Python 3.11+ with `from __future__ import annotations`
- Type hints on all function signatures
- All slash commands must call `await interaction.response.defer(ephemeral=True)` before any async I/O
- No bare `except:` — always catch specific exceptions or `Exception` with logging
- All outbound HTTP must go through `_fetch()` or the session directly — never subprocess/curl
- Discord notification sends must pass explicit `allowed_mentions`

## Branch Naming

- `feat/description` — new features
- `fix/description` — bug fixes
- `chore/description` — maintenance, docs, refactoring

## Pull Request Checklist

- [ ] No secrets or `.env` files committed
- [ ] `requirements.txt` updated if new dependencies added
- [ ] All slash commands defer before async I/O
- [ ] No HTML scrapers — WebSub push + RSS fallback only
- [ ] Tested locally with a real Discord bot token

## Reporting Bugs

Open an issue at https://github.com/GujjuMui/Discord-Notification-Bot/issues with:
- Railway deploy logs (redact your bot token)
- The exact slash command or action that triggered the bug
- Expected vs actual behaviour
