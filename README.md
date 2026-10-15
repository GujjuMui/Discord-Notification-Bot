# P U M M E L O — Discord Notification Bot

A production-grade Discord bot delivering **instant YouTube upload notifications** via Google's WebSub (PubSubHubbub) push engine, full **8-channel categorized server audit logging**, a **moderation suite** with pre-flight safety checks, and multi-channel **broadcast** announcements.

**Version:** 3.3.1 | **Python:** 3.11+ | **discord.py:** 2.7.1 | **Deployed on:** Railway

---

## Feature Highlights

- **Instant YouTube notifications** — Google WebSub push delivers new uploads within seconds; 60-second RSS fallback catches any missed pushes
- **Content-type detection** — classifies Videos, Shorts, Live Streams, and Community Posts via oEmbed + HEAD redirect
- **Per-route configuration** — each YouTube channel can post to a different Discord channel, with its own role ping and content filter
- **8-channel audit logging** — chat, member, profile, role, channel, server, voice, and moderation events
- **Moderation suite** — `/ban`, `/kick`, `/timeout`, `/untimeout`, `/purge` with full pre-flight hierarchy and permission checks; also available as `!` prefix commands
- **Broadcast** — send announcements to up to 10 channels simultaneously with image/GIF support
- **RBAC trust system** — bot owner → server owner → trusted users; all admin commands are gated
- **Demo mode** — intercepts destructive actions and returns simulated embeds (no real execution)
- **WebSub lease auto-renewal** — subscriptions renew 24h before 7-day expiry
- **SQLite + WAL mode** — concurrent-safe, Railway volume-persistent

---

## Architecture

```
YouTube Upload
      │
      ▼
Google WebSub Hub ──POST──▶  aiohttp.web /youtube/webhook
      │                              │
      │                    Guard: channel_id tracked?
      │                    No → reject (connected accounts)
      │                    Yes ↓
      │                    oEmbed classification
      │                    (video / short / live)
      │                              │
      ▼                              ▼
RSS Fallback (60s)         Discord embed + role ping
      │                              │
      └──────────────────────────────▘
                    SQLite dedup
             (never sends same video twice)
```

---

## Project Structure

```
Discord Notification Bot/
├── main.py                  # Bot entry point, cog loading, on_ready banner
├── config.py                # Environment variable loading and validation
├── database.py              # SQLite persistence, dedup, WebSub lease tracking
├── requirements.txt         # Pinned dependencies
├── .env.example             # All environment variable documentation
├── .gitignore               # Excludes secrets, DB, venv, cache
├── railway.toml             # Railway build/deploy config with healthcheck
├── Procfile                 # Fallback start command
├── cogs/
│   ├── youtube_tracker.py   # WebSub engine, RSS fallback, slash commands
│   ├── server_logger.py     # 8-channel categorized audit logging
│   ├── channel_manager.py   # Owner-only /channelmanager, /leaveguild, permission requests
│   ├── utility.py           # /say, /announcement, /edit_say
│   └── moderation.py        # Moderation suite, diagnostics, broadcast
└── utils/
    └── helpers.py           # Discord mention formatting utilities
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in the values.

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | — | Bot token from Discord Developer Portal |
| `BOT_OWNER_ID` | Optional | Auto-detected | Discord user ID of the bot owner |
| `DATABASE_PATH` | Optional | `youtube_bot.db` | SQLite file path. On Railway: `/app/data/youtube_bot.db` |
| `SYNC_COMMANDS` | Optional | `false` | Set `true` to sync slash commands on startup. Use `/sync` instead in production. |
| `WEBHOOK_URL` | Optional | — | Public HTTPS URL for WebSub push. e.g. `https://your-app.up.railway.app/youtube/webhook` |
| `WEBHOOK_PORT` | Optional | `8080` | Port the embedded aiohttp webhook server listens on |
| `FALLBACK_POLL_INTERVAL` | Optional | `60` | RSS fallback poll interval in seconds (min 60) |
| `ALLOWED_GUILD_IDS` | Optional | `""` (open) | Comma-separated guild IDs to restrict bot operation |
| `ENABLE_MODERATION` | Optional | `false` | Enable moderation cog |
| `ENABLE_CHANNEL_MANAGER` | Optional | `false` | Enable channel manager cog |
| `BOT_NAME` | Optional | `P U M M E L O` | Bot display name for permission requests and roles |
| `DEMO_MODE` | Optional | `false` | Set `true` to enable demo/simulation mode |
| `DEMO_GUILD_ID` | Optional | — | Guild ID where demo mode intercepts destructive actions |
| `DEMO_AUDIT_LOG_CHANNEL` | Optional | — | Channel ID for demo mode audit log events |
| `TEST_CHANNEL_ID` | Optional | — | Channel ID where `/testmessage` is allowed to post |

---

## Quick Start (Local)

**Requirements:** Python 3.11+

```bash
# 1. Clone
git clone https://github.com/GujjuMui/Discord-Notification-Bot.git
cd Discord-Notification-Bot

# 2. Virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

# 3. Dependencies
pip install -r requirements.txt

# 4. Configure
copy .env.example .env        # Windows
# cp .env.example .env        # Linux/macOS
# Edit .env — set DISCORD_BOT_TOKEN at minimum

# 5. Run
python main.py
```

For WebSub testing locally, use [ngrok](https://ngrok.com):
```bash
ngrok http 8080
# Then set WEBHOOK_URL=https://xxxx.ngrok-free.app/youtube/webhook in .env
```

---

## Railway Deployment

1. Push repo to GitHub
2. Create Railway project → **Deploy from GitHub repo**
3. Add a **Volume** mounted at `/app/data`
4. Set environment variables (see table above)
5. Railway auto-deploys on every `git push`

After each deploy that changes the command tree, run `/sync` once in Discord.

### Railway healthcheck
The bot exposes `GET /health` → `200 OK` used by Railway's healthcheck (`railway.toml`).

---

## Command Reference

### YouTube Tracking

| Command | Parameters | Permission | Description |
|---|---|---|---|
| `/add_yt` | `url` `#channel` `[role]` `[types]` `[name]` | Staff | Subscribe to a YouTube channel |
| `/remove_yt` | `url_or_id` `[#channel]` | Staff | Remove a subscription |
| `/list_yt` | — | Staff | List all routes with push status |
| `/test_yt` | `url` `[#channel]` | Staff | Send a test notification embed |
| `/ytinfo` | `url` | Public | Resolve a YouTube URL to channel ID + RSS info |

**Content type filters:** `all` · `videos` · `shorts` · `live` · `community`

### Moderation (Slash + Prefix)

| Command | Parameters | Permission | Description |
|---|---|---|---|
| `/ban` / `!ban` | `@user` `[reason]` `[days]` | Staff | Ban a member (pre-flight checked) |
| `/kick` / `!kick` | `@user` `[reason]` | Staff | Kick a member |
| `/timeout` / `!timeout` | `@user` `<minutes>` `[reason]` | Staff | Timeout a member (1–40320 min) |
| `/untimeout` / `!untimeout` | `@user` `[reason]` | Staff | Remove an active timeout |
| `/purge` / `!purge` | `<1-100>` `[@user]` | Staff | Bulk-delete messages (max 14 days) |
| `/userinfo` / `!userinfo` | `[@user]` | Public | Show member info, roles, hierarchy rank |
| `/serverinfo` / `!serverinfo` | — | Public | Show server stats, boost level, verification |
| `/permissions` | — | Public | Show bot's effective permissions in this channel |
| `/checkaccess` | `<action>` `@user` | Staff | Pre-flight check — would this action succeed? |

### Channel Manager (Owner-Only, DM-compatible)

| Command | Parameters | Permission | Description |
|---|---|---|---|
| `/channelmanager` | — | Owner | Guild picker → list / create / delete channels across any guild from DMs. Requires `Manage Channels` in target guild. |
| `/leaveguild` | — | Owner | Guild picker dropdown → confirm → bot leaves the selected server immediately |

### Diagnostics (Owner-Only)

| Command | Parameters | Description |
|---|---|---|
| `/permaudit` | — | Check all guilds for missing permissions — shows ✅ fully authorized vs ⚠️ missing with per-guild breakdown |
| `/listyt_all` | — | Cross-guild YouTube tracking audit grouped by guild |
| `/guildlist` | — | All servers with ✅/⚠️ authorization status (also `!guildlist`) |
| `/checkpermissions` | `[guild_id]` | Per-channel permission matrix |
| `/checkcommands` | — | List all commands with access tiers |
| `/diagnose` | — | Full system health check |
| `/testmoderation` | `<action>` `@user` | Dry-run all 5 validation stages |
| `/testmessage` | `[layout]` `[image_url]` | Send test embed to TEST_CHANNEL_ID |

### Broadcast & Utility

| Command | Parameters | Permission | Description |
|---|---|---|---|
| `/broadcast` | `<message>` `<#ch1>…[#ch10]` `[image_url]` `[ping]` | Staff | Send announcement to up to 10 channels |
| `/say` | `<message>` `[attachment]` | Staff | Send a bot message in current channel |
| `/announcement` | `<#channel>` `<message>` `[embed]` `[title]` `[ping_role]` `[image_url]` | Staff | Announcement with live preview |
| `/edit_say` | `<message_id>` `<new_content>` | Staff | Edit a previously sent bot message |

### Server Logging

| Command | Parameters | Permission | Description |
|---|---|---|---|
| `/setup_logs` | `[auto_create]` `[type]` `[#channel]` | Staff | Create or map audit log channels |

### Admin / System

| Command | Parameters | Permission | Description |
|---|---|---|---|
| `/trust add` | `@user` | Staff | Grant trusted access in this server |
| `/trust remove` | `@user` | Staff | Revoke trusted access |
| `/trust list` | — | Staff | List trusted users |
| `/sync` | — | Staff | Sync global slash command tree |
| `/botstatus` | — | Staff | Live health dashboard |
| `/about` | — | Public | Bot profile with live stats |
| `/help` | — | Public | Interactive categorized help menu (9 categories) |

---

## Server Audit Logging

Run `/setup_logs auto_create:True` to create the **📁 SERVER LOGS** category with 8 channels:

| Channel | Events |
|---|---|
| `#chat-logs` | Message edits, deletes, bulk deletes |
| `#member-logs` | Joins, leaves, bans, kicks, timeouts |
| `#profile-logs` | Username, avatar, nickname changes |
| `#role-logs` | Role creates, deletes, permission edits |
| `#channel-logs` | Channel creates, deletes, edits |
| `#server-logs` | Server settings, emoji/sticker events |
| `#voice-logs` | Voice join, leave, move, mute, deafen |
| `#moderation-logs` | Moderation actions from audit log |

Log channel registrations **auto-restore** after Railway redeploys.

---

## WebSub Subscription Lifecycle

```
/add_yt ──▶ subscribe_channel() ──POST──▶ pubsubhubbub.appspot.com
                                                    │
                                          (HTTP 202 Accepted)
                                                    │
                                   Google sends GET challenge to /youtube/webhook
                                                    │
                                          Bot echoes hub.challenge
                                                    │
                                   Subscription verified — lease = 7 days
                                                    │
                                   resubscribe_loop checks every 6h
                                   Re-subscribes at T-24h before expiry
```

First push after a new subscription has 5–20 min Google hub latency (normal). The 60-second RSS fallback covers this window.

---

## Discord Application Setup

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create application → **Bot** tab → Enable:
   - **Server Members Intent**
   - **Message Content Intent**
3. Invite with scopes: `bot` + `applications.commands`
4. Permissions: `Send Messages`, `Embed Links`, `Read Message History`, `View Channels`, `Manage Channels`, `Manage Messages`, `Ban Members`, `Kick Members`, `Moderate Members`, `View Audit Log`

---

## Security

- `.env` and `*.db` excluded by `.gitignore`
- SSRF protection: all outbound HTTP restricted to YouTube/Google allowlist
- Mention injection prevention: YouTube content cannot inject `@everyone` or role pings
- WebSub push guard: rejects pushes from untracked channel IDs before classification
- RBAC: admin commands gated by bot owner / server owner / trusted users
- Demo mode: `DEMO_MODE=true` + `DEMO_GUILD_ID` intercepts all destructive actions

---

## Changelog

### v3.3.1
- Centralized all hardcoded configuration constants into `config.py` with environment variable overrides (`BOT_NAME`, `BOT_VERSION`, `WEBSUB_HUB_URL`, `WEBSUB_LEASE_SECONDS`, `WEBSUB_MAX_BODY_BYTES`, `LOG_CATEGORY_NAME`, `LOG_CHANNEL_NAMES`, `MESSAGE_RETENTION_DAYS`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`)
- Added `*.db-journal` to `.gitignore`
- Added comprehensive documentation for all new environment variables in `.env.example` and `README.md`

### v3.2.0
- New `cogs/moderation.py`: full moderation suite (`/ban`, `/kick`, `/timeout`, `/untimeout`, `/purge`), prefix command equivalents (`!ban`, etc.), `/userinfo`, `/serverinfo`, `/permissions`, `/checkaccess`, `/broadcast` (up to 10 channels, GIF/image support), owner diagnostics (`/diagnose`, `/checkpermissions`, `/checkcommands`, `/testmoderation`, `/testmessage`), demo mode engine
- `/help` updated: 7 categories covering all commands including new moderation and utility sections
- `/about` footer now tracks live bot version

### v3.1.2
- Security hardening: WebSub challenge validation, push body size cap (512KB), safe lease_seconds parse
- `poll_loop` and `resubscribe_loop` now have `on_error` handlers — unhandled exceptions no longer kill the loop

### v3.1.1
- `_send_notification` retries up to 3× on Discord 429 with exponential backoff

### v3.1.0
- Channel resolver fix: `CHANNEL_ID_PATTERNS` reordered — `externalId` first, generic `channelId` last
- WebSub push guard: rejects untracked channel IDs before oEmbed classification

### v3.0.0
- Full WebSub push engine replacing all HTML scrapers
- Embedded aiohttp.web server, oEmbed classification, lease auto-renewal
- 60-second RSS fallback, per-entry push exception handling

---

## Credits

Developed and maintained by **GujjuMui**
