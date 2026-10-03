# P U M M E L O — Discord Notification Bot

A production-grade Discord bot for **instant YouTube upload notifications** via Google's WebSub (PubSubHubbub) push engine, with a 60-second RSS fallback safety net and full categorized server audit logging across 8 dedicated channels.

**Version:** 3.1.0 | **Python:** 3.13 | **discord.py:** 2.7.1 | **Deployed on:** Railway

---

## Architecture

```
YouTube Upload
      │
      ▼
Google WebSub Hub  ──POST──▶  aiohttp.web /youtube/webhook
      │                              │
      │                    _process_push_payload()
      │                              │
      │              Guard: channel_id tracked?
      │              No → reject (connected accounts)
      │              Yes ↓
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

**Primary path:** Google pushes new uploads to your webhook within seconds.  
**Fallback path:** RSS poller runs every 60 seconds to catch any delayed pushes.  
**Deduplication:** `yt_content_route_cache` keyed on `(guild, channel, video_id)` — zero duplicates regardless of which path fired.

---

## Features

- **Instant WebSub push notifications** — Google delivers new uploads in seconds
- **YouTube Shorts / Live / Video / Community** detection via oEmbed + HEAD redirect
- **HD thumbnails** — `maxresdefault.jpg` → `hqdefault.jpg` fallback
- **Per-route configuration** — different Discord channels, roles, and content filters per YouTube source
- **8-channel categorized server audit logging** — chat, member, profile, role, channel, server, voice, moderation
- **Auto-restore** — log channel registrations survive Railway redeploys
- **WebSub lease auto-renewal** — subscriptions renew 24h before expiry (7-day lease)
- **SSRF protection** — outbound fetches restricted to allowlisted YouTube/Google hostnames
- **Mention injection prevention** — scraped YouTube content cannot inject `@everyone` or role pings
- **Railway volume persistence** — SQLite survives redeploys via persistent volume at `/app/data`

---

## Project Structure

```
Discord Notification Bot/
├── main.py                  # Bot entry point, event handlers, on_ready banner
├── config.py                # Environment variable loading and validation
├── database.py              # SQLite persistence, deduplication, WebSub lease tracking
├── requirements.txt         # Pinned dependencies
├── .env.example             # All environment variable documentation
├── .gitignore               # Excludes secrets, DB, venv, cache
├── cogs/
│   ├── youtube_tracker.py   # WebSub engine, RSS fallback, slash commands
│   ├── server_logger.py     # 8-channel categorized audit logging
│   └── utility.py           # /say, /announcement, /edit_say commands
└── utils/
    └── helpers.py           # Discord mention formatting utilities
```

---

## Quick Start (Local)

**Requirements:** Python 3.11+

```bash
# 1. Clone and enter the repo
git clone https://github.com/GujjuMui/Discord-Notification-Bot.git
cd Discord-Notification-Bot

# 2. Create virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
copy .env.example .env        # Windows
# cp .env.example .env        # Linux/macOS
# Edit .env and set DISCORD_BOT_TOKEN

# 5. Run
python main.py
```

> **Note:** WebSub push notifications require a publicly reachable HTTPS URL. For local testing, use [ngrok](https://ngrok.com): `ngrok http 8080` and set `WEBHOOK_URL=https://xxxx.ngrok-free.app/youtube/webhook` in `.env`.

---

## Railway Deployment

1. Push to GitHub
2. Create a new Railway project → **Deploy from GitHub repo**
3. Add a **Volume** mounted at `/app/data`
4. Set environment variables (see table below)
5. Railway auto-deploys on every push

### Required Railway Variables

| Variable | Example Value | Required |
|---|---|---|
| `DISCORD_BOT_TOKEN` | `MTU1...` | ✅ Yes |
| `DATABASE_PATH` | `/app/data/youtube_bot.db` | ✅ Yes |
| `WEBHOOK_URL` | `https://your-app.up.railway.app/youtube/webhook` | ✅ For WebSub |
| `BOT_OWNER_ID` | `123456789012345678` | Optional |
| `SYNC_COMMANDS` | `false` | Optional |
| `WEBHOOK_PORT` | `8080` | Optional (default: 8080) |
| `FALLBACK_POLL_INTERVAL` | `60` | Optional (default: 60s) |

---

## Slash Commands

### YouTube Tracking

| Command | Description | Access |
|---|---|---|
| `/add_yt` | Subscribe to a YouTube channel with optional role ping and content filter | Admin/Trusted |
| `/remove_yt` | Remove a subscription | Admin/Trusted |
| `/list_yt` | List all tracked channels and their push status (🟢/🟡) | Admin/Trusted |
| `/test_yt` | Send a test notification embed to any channel | Admin/Trusted |
| `/ytinfo` | Resolve a YouTube URL to channel ID and show push status | Public |

### Server Logging

| Command | Description | Access |
|---|---|---|
| `/setup_logs` | Auto-create 8-channel logging category or manually map log types | Admin/Trusted |

### Utility

| Command | Description | Access |
|---|---|---|
| `/say` | Send a message as the bot in the current channel | Admin/Trusted |
| `/announcement` | Create an announcement with live preview, embed toggle, and role ping | Admin/Trusted |
| `/edit_say` | Edit a previously sent bot announcement | Admin/Trusted |

### Admin / System

| Command | Description | Access |
|---|---|---|
| `/sync` | Manually sync the global slash command tree | Admin/Trusted |
| `/botstatus` | Live health dashboard — RAM, CPU, gateway, RSS, WebSub status | Admin/Trusted |
| `/about` | Public bot profile with live stats | Public |
| `/help` | Interactive categorized help menu | Public |
| `/trust add/remove/list` | Manage trusted users for admin commands | Owner |

### Content Type Filters

When running `/add_yt`, the `types` parameter accepts:

| Value | What it tracks |
|---|---|
| `all` | Everything (default) |
| `videos` | Standard uploads only |
| `shorts` | YouTube Shorts only |
| `live` | Live streams only |
| `community` | Community posts only |

---

## Server Audit Logging

Run `/setup_logs auto_create:True` to automatically create the **📁 SERVER LOGS** category with 8 channels:

| Channel | Events logged |
|---|---|
| `#chat-logs` | Message edits, deletes, bulk deletes |
| `#member-logs` | Joins, leaves, bans, kicks, timeouts |
| `#profile-logs` | Username, avatar, nickname changes |
| `#role-logs` | Role creates, deletes, permission edits |
| `#channel-logs` | Channel creates, deletes, edits |
| `#server-logs` | Server setting changes, emoji/sticker events |
| `#voice-logs` | Voice join, leave, move, mute, deaff events |
| `#mod-logs` | Moderation actions from audit log |

Log channel registrations **auto-restore** after Railway redeploys — the bot scans Discord for the existing `📁 SERVER LOGS` category and re-registers all channel IDs automatically.

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

---

## Discord Application Setup

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a new application → **Bot** tab → Enable:
   - **Server Members Intent**
   - **Message Content Intent**
   - **Presence Intent** (optional)
3. Invite with scopes: `bot` + `applications.commands`
4. Permissions needed: `Send Messages`, `Embed Links`, `Read Message History`, `View Channels`, `Manage Channels` (for `/setup_logs`), `View Audit Log` (for mod logging)

---

## Security

- `.env` and `*.db` files are excluded by `.gitignore` — never commit secrets
- SSRF protection: all outbound HTTP fetches are restricted to allowlisted YouTube/Google hostnames
- Mention injection prevention: YouTube content cannot inject `@everyone`, `<@role>`, or `<@user>` into notification messages
- Audit log channels use `AllowedMentions.none()` — zero accidental pings in log channels
- RBAC: admin commands restricted to bot owner, server owner, and trusted users via `/trust`
- `SYNC_COMMANDS=false` by default — prevents Discord HTTP 429 rate limits on every restart

---

## Changelog

### v3.1.0
- **Channel resolver fix** — `CHANNEL_ID_PATTERNS` reordered so `externalId` and `itemprop=channelId` are checked before the generic `channelId` JSON key, preventing connected brand accounts from hijacking the resolved channel ID
- **WebSub push guard** — push handler now rejects untracked `<yt:channelId>` values before classification, blocking connected account notifications entirely
- `resolve_channel_id` returns `(channel_id, name)` tuple with pattern-index logging for easier future diagnosis
- `DESIGN.md` fully rewritten to reflect v3.x architecture

### v3.0.0
- **Full WebSub (PubSubHubbub) push engine** — replaced all HTML scrapers with Google's official push notification system
- Embedded `aiohttp.web` HTTP server for webhook handling (GET challenge + POST Atom XML parser)
- oEmbed content classification (video / short / live)
- HEAD redirect detection for YouTube Shorts
- WebSub lease auto-renewal (every 6h, renews at T-24h)
- 60-second RSS fallback safety net alongside WebSub
- `lease_expires_at` tracking in SQLite
- Per-entry exception handling in push payload processor
- `asyncio.ensure_future` done-callback for silent error detection

### v2.3.0
- Railway volume persistence for SQLite
- Auto-restore of log channel registrations after redeploys
- Exponential backoff for RSS 404 errors
- Warmup seeding to prevent historical video spam on startup
- Cross-type deduplication (same video_id not re-sent as both video and short)

### v2.0.0
- Multi-guild, multi-route YouTube tracking
- 8-channel categorized server audit logging
- `/announcement` with interactive preview and embed toggle
- Trusted user RBAC system

---

## Credits

Developed and maintained by **GujjuMui**
