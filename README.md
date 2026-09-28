# YouTube Notification Bot

A Python Discord bot that polls YouTube RSS feeds and sends Discord embeds for new uploads.

## Stack
- Python 3.9+
- discord.py 2.x
- aiohttp
- feedparser
- SQLite
- python-dotenv

No YouTube API key is required. The tracker uses YouTube channel RSS feeds.

## Project structure

main.py - bot entry point
config.py - environment configuration
database.py - SQLite persistence and deduplication
cogs/youtube_tracker.py - RSS polling, notifications, and slash commands
requirements.txt - dependencies
.env.example - configuration template
.gitignore - excludes secrets and runtime files

## Windows setup

Open CMD in the repository root:

python -m venv .venv
.venv\\Scripts\\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env

Edit .env and set DISCORD_BOT_TOKEN. DISCORD_CHANNEL_ID is optional; a per-channel target can be set with /settarget.

Run with:

python main.py

## Discord setup

Create a Discord application and bot, then invite it with the bot and applications.commands scopes. The bot needs Send Messages and Embed Links in the notification channel. Message Content Intent should be enabled if you want the legacy !ping command.

## Commands

/addchannel <url> - track a YouTube channel
/removechannel <channel_id> - pause tracking without deleting video history
/listchannels - list active channels
/settarget <channel_id> <channel> - set notification destination
/pausechannel <channel_id> - pause a channel
/resumechannel <channel_id> - resume a channel
/ytinfo <url> - resolve a YouTube channel URL
!ping - check latency

## Deduplication

Each YouTube video ID is stored with a unique constraint in SQLite. Existing RSS entries are seeded when a channel is first added, so old videos are not announced. New videos are inserted as pending before delivery and marked notified only after Discord accepts the message. If delivery fails, the pending record can be retried. Pausing/removing a channel keeps its video history so re-adding it does not replay old uploads.

## Security

Never commit .env or bot tokens. The repository .gitignore excludes .env, SQLite databases, virtual environments, and Python cache files.
