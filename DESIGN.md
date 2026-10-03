# P U M M E L O — Architecture & Design Document

**Version:** 3.1.0 | **Last Updated:** 2026-10-03 | **Status:** Production

---

## Overview

P U M M E L O is a production Discord bot that delivers instant YouTube upload notifications via Google's WebSub (PubSubHubbub) push system, with a 60-second RSS fallback safety net and 8-channel categorized server audit logging.

It runs on Railway with a persistent SQLite volume and serves 5 guilds.

---

## High-Level Architecture

```
YouTube Upload
      │
      ▼
Google WebSub Hub ──POST──▶  aiohttp.web /youtube/webhook
      │                              │
      │                    _process_push_payload()
      │                              │
      │              Guard: is channel_id tracked?
      │              (rejects connected brand accounts)
      │                              │
      │                    oEmbed classification
      │                    (video / short / live)
      │                              │
      ▼                              ▼
RSS Fallback (60s)         _dispatch_activity()
      │                              │
      │                    SQLite dedup check
      │                    (guild, channel, video_id)
      │                              │
      └──────────────────────────────▶ Discord embed + role/user ping
```

**Primary path:** Google WebSub pushes new uploads within seconds of publish.  
**Fallback path:** RSS poller runs every 60s to catch any push Google failed to deliver.  
**Dedup:** `yt_content_route_cache` keyed on `(guild_id, yt_channel_id, content_id, content_type, target_channel_id)` — zero duplicates regardless of which path fires first.

---

## File Structure

```
Discord Notification Bot/
├── main.py                  # Bot entry point, cog loading, on_ready banner
├── config.py                # Environment variable loading (dotenv + os.environ)
├── database.py              # SQLite persistence, all DB operations
├── requirements.txt         # Pinned dependencies
├── .env.example             # All environment variable documentation
├── .gitignore               # Excludes secrets, DB, venv, cache, .cursor/
├── railway.toml             # Railway build/deploy config with healthcheck
├── Procfile                 # Fallback start command for Railway
├── cogs/
│   ├── youtube_tracker.py   # WebSub engine, RSS fallback, channel resolver, slash commands
│   ├── server_logger.py     # 8-channel categorized audit logging
│   └── utility.py           # /say, /announcement, /edit_say
└── utils/
    └── helpers.py           # Discord mention formatting, allowed_mentions helpers
```

---

## Component Design

### `main.py`
- Loads cogs on startup: `YouTubeTracker`, `ServerLogger`, `Utility`
- Logs table row counts on `on_ready` for quick health check
- `SYNC_COMMANDS` env gate prevents Discord 429 rate limit on every restart
- Bot version: `BOT_VERSION = "3.1.0"`

### `config.py`
- Single source of truth for all environment variables
- Validates required variables at startup (raises `ValueError` if missing)
- Key variables: `DISCORD_BOT_TOKEN`, `DATABASE_PATH`, `WEBHOOK_URL`, `WEBHOOK_PORT`, `FALLBACK_POLL_INTERVAL`, `BOT_OWNER_ID`, `SYNC_COMMANDS`

### `database.py`
Key tables:

| Table | Purpose |
|---|---|
| `yt_monitored_channels` | Tracked YouTube channels per guild/route, WebSub lease state |
| `yt_content_route_cache` | Per-route dedup: `(guild, yt_channel, video_id, type, target_channel)` |
| `yt_content_cache` | Legacy global dedup (kept for cross-type dedup) |
| `videos` | Video metadata archive |
| `guild_log_channels` | Discord channel IDs for 8-category audit logging |
| `trusted_users` | RBAC trusted user list |
| `say_messages` | `/say` message ID tracking for `/edit_say` |
| `message_cache` | 30-day message cache for edit/delete audit logs |

SQLite runs in WAL journal mode for concurrent write safety.

### `cogs/youtube_tracker.py`

#### Channel Resolver (`resolve_channel_id`)
Converts any YouTube URL or handle to a `(channel_id, channel_name)` tuple by fetching the page HTML and matching `CHANNEL_ID_PATTERNS` in priority order:

1. `"externalId":"UC..."` — JSON key that always refers to the page owner (most reliable)
2. `<meta itemprop="channelId" content="UC...">` — HTML canonical meta (equally reliable)
3. `<meta content="UC..." itemprop="channelId">` — alternate attribute order
4. `"canonicalBaseUrl":"/@..."..."externalId":"UC..."` — co-located JSON pair
5. `"https://www.youtube.com/channel/UC..."` — URL string in page JSON
6. `"channelId":"UC..."` — generic JSON key (last resort; can match connected accounts)

Priority order prevents connected brand accounts from hijacking the resolved ID.

#### WebSub Engine
- **Subscribe:** `subscribe_channel(channel_id)` — POSTs to `pubsubhubbub.appspot.com` with 7-day lease
- **Verify:** `GET /youtube/webhook?hub.challenge=...` — echoes challenge back to Google
- **Push:** `POST /youtube/webhook` — parses Atom XML, validates `channel_id` against tracked list before classifying, dispatches via `_dispatch_activity`
- **Renew:** `resubscribe_loop` runs every 6h, renews subscriptions within 24h of expiry

Connected/alternate brand-account pushes are rejected at the guard check (`channel_id` not in `yt_monitored_channels`) before any oEmbed call is made.

#### Content Classification (`_classify_and_build_item`)
Uses YouTube's oEmbed API for channel name/title, then:
- `HEAD https://www.youtube.com/shorts/{video_id}` — 200 → Short, 303/404 → not Short
- Live detection via oEmbed `author_name` heuristics

#### RSS Fallback (`poll_loop`)
- Runs every 60 seconds (overridable via `FALLBACK_POLL_INTERVAL`)
- Protected by `asyncio.Lock` to prevent overlapping poll cycles
- Exponential backoff on RSS 404 errors (1→2→4→8→16 cycle skip)
- Warmup seeding on first poll to prevent historical video spam

#### Dispatch (`_dispatch_activity`)
- Filters items by subscription `content_types` (all/videos/shorts/live/community)
- Sorts newest-first, caps at 3 items per dispatch cycle (blast radius control)
- Per-route dedup check before send
- Marks notified in both `yt_content_route_cache` and `videos` table

### `cogs/server_logger.py`
8-category audit logging mapped to dedicated Discord channels:

| Category | Events |
|---|---|
| `chat` | Message edit, delete, bulk delete |
| `member` | Join, leave, ban, kick, timeout |
| `profile` | Username, avatar, nickname changes |
| `role` | Role create, delete, permission edit |
| `channel` | Channel create, delete, edit |
| `server` | Server settings, emoji/sticker events |
| `voice` | Voice join, leave, move, mute, deafen |
| `mod` | Moderation audit log actions |

Log channel registrations auto-restore on `on_ready` — scans Discord for existing `📁 SERVER LOGS` category and re-registers all channel IDs.

### `cogs/utility.py`
- `/say` — sends bot message, stores message ID for `/edit_say`
- `/announcement` — interactive preview with embed toggle, role ping, and optional link button
- `/edit_say` — edits a previously sent bot message by stored message ID

---

## Database Schema (key tables)

```sql
CREATE TABLE yt_monitored_channels (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id                TEXT NOT NULL,
    yt_channel_id           TEXT NOT NULL,
    yt_channel_name         TEXT,
    discord_target_channel_id TEXT NOT NULL,
    ping_role_id            TEXT,
    ping_user_ids           TEXT,          -- JSON array
    content_types           TEXT DEFAULT 'all',
    lease_expires_at        TIMESTAMP,
    websub_verified         INTEGER DEFAULT 0,
    added_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE yt_content_route_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        TEXT NOT NULL,
    yt_channel_id   TEXT NOT NULL,
    content_id      TEXT NOT NULL,
    content_type    TEXT NOT NULL,
    target_channel_id TEXT NOT NULL,
    notified_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(guild_id, yt_channel_id, content_id, content_type, target_channel_id)
);
```

---

## Security

- **SSRF protection:** All outbound HTTP goes through `_fetch()` which enforces an allowlist of YouTube/Google hostnames
- **Mention injection prevention:** YouTube content cannot inject `@everyone`, `<@role>`, or `<@user>` — all sends use explicit `allowed_mentions`
- **RBAC:** Admin commands restricted to bot owner, server owner, and trusted users (`/trust`)
- **Connected account guard:** WebSub push handler rejects pushes whose `<yt:channelId>` doesn't match a tracked channel ID — prevents stray pushes from connected brand accounts
- **Secret hygiene:** `.env` and `*.db` excluded by `.gitignore`; `SYNC_COMMANDS=false` default prevents Discord 429 on restart

---

## Deployment (Railway)

```
railway.toml
  startCommand: python main.py
  healthcheckPath: /health        ← aiohttp.web GET /health
  restartPolicyType: on_failure
  restartPolicyMaxRetries: 5

Persistent volume mounted at /app/data
DATABASE_PATH=/app/data/youtube_bot.db
```

After any deploy that changes the command tree, run `/sync` once to push updated slash commands to Discord.

---

## Known Behaviour

- First WebSub push after a new subscription has 5–20 min Google hub latency (normal; RSS fallback covers this window)
- Subsequent pushes on established subscriptions arrive within seconds
- Railway cold starts can cause the first 1–2 interactions to time out (10062) if Discord delivers them before the bot's event loop is fully ready — run `/sync` after each deploy to reset the command tree
