# Changelog

All notable changes to this project are documented here.
Format: `[version] — date — summary`

---

## [3.3.0] — 2026-10-12

### Added
- **`/listyt_all`** (owner-only) — cross-guild YouTube tracking audit; aggregates all tracked channels across every guild, grouped by guild name/ID, paginated to stay within Discord's 25 field / 1024 char / 4096 desc limits; sends multiple embeds if needed
- **`/guildlist`** (slash, owner-only) — full guild list with ✅/⚠️ authorization status, member counts, and allowlist instructions; complements the existing `!guildlist` prefix command
- **`is_bot_owner_only()`** check — strict bot-owner-only `app_commands.check` that excludes guild owners and trusted users; used for owner audit commands
- **Guild allowlist** (`ALLOWED_GUILD_IDS` env var) — blocks all slash commands in unauthorized guilds with `⛔ This bot is not authorized` message; `on_guild_join` DMs owner when bot is added to any server with authorization status
- **RSS backoff extended to HTTP 500/502/503** — YouTube CDN 5xx errors now use the same exponential backoff as 404s instead of logging full ERROR tracebacks

### Fixed
- `/about` footer hardcoded version — now reads `bot.bot_version` live
- `/help` dropdown: added Moderation, Broadcast, and Utility categories missing since v3.2.0; all 7 categories now document commands with parameters and permission badges
- `HelpSelect.callback` safe fallback for unknown category values

---

## [3.2.1] — 2026-10-11

### Fixed
- `/about` footer was hardcoded to `v3.0.0` — now reads live version from `bot.bot_version` attribute
- `/help` was missing Moderation, Utility, and Broadcast categories added in v3.2.0; all 7 categories now documented with full command lists, parameters, and permission badges
- `HelpSelect.callback` now has a safe fallback for unknown category values instead of a potential `KeyError`

### Changed
- `bot.bot_version` exposed on the bot object at startup so cogs can reference it without a circular import
- README fully rewritten: complete command reference table (all 30+ commands), environment variable table, architecture diagram, WebSub lifecycle, security section, Railway deployment guide

---

## [3.2.0] — 2026-10-11

### Added
- **`cogs/moderation.py`** — new cog with full moderation suite, diagnostics, demo mode, broadcast, and test message

- **Moderation suite** (staff/trusted): `/ban`, `/kick`, `/timeout`, `/untimeout`, `/purge`
  - All commands run atomic pre-flight checks: input validation → bot permission check → role hierarchy check → guild owner protection → self-target protection → execution
  - `/purge` skips messages older than 14 days (Discord API limitation)

- **Info commands** (public): `/userinfo`, `/serverinfo`, `/permissions`, `/checkaccess`
  - `/checkaccess` is a non-destructive pre-flight evaluator — shows exactly which constraint would block an action

- **Owner-only diagnostics**: `/checkpermissions`, `/checkcommands`, `/diagnose`, `/testmoderation`
  - `/diagnose` checks gateway intents, guild permissions, WebSub status, DB health, and command tree sync
  - `/testmoderation` dry-runs all 5 validation stages for a moderation action without executing

- **Demo mode engine**: set `DEMO_MODE=true` + `DEMO_GUILD_ID=<id>` in env to intercept all destructive moderation actions in that guild and return `[SIMULATED DEMO MODE — NO REAL ACTION TAKEN]` embeds

- **`/broadcast`**: send an announcement to up to 10 channels simultaneously
  - 10 separate channel parameters (cleanest Discord UX, no typing)
  - Supports images and GIFs via `image_url` parameter
  - Optional role/everyone/here ping, embed or plain text mode, custom title
  - 0.5s delay between sends to avoid Discord rate limits
  - Deduplicates channels if the same channel is selected twice

- **`/testmessage`**: owner-only test embed with 4 layout variants (standard, announcement, alert, youtube_notification)
  - Strictly scoped to `TEST_CHANNEL_ID` env var
  - 30-second per-channel cooldown
  - Interactive `[Delete Test Message]` button attached to output

### Changed
- `main.py`: Moderation cog loaded on startup; banner updated to show all 4 cogs
- `.env.example`: added `DEMO_MODE`, `DEMO_GUILD_ID`, `DEMO_AUDIT_LOG_CHANNEL`, `TEST_CHANNEL_ID`

---

## [3.1.2] — 2026-10-10

### Security
- **WebSub challenge validation** — `hub.challenge` now validated for length (≤512 chars) and printability before echoing; rejects oversized or non-printable strings that could cause memory abuse
- **WebSub push body size cap** — POST payloads over 512 KB are rejected with HTTP 413 before XML parsing; prevents memory exhaustion from crafted large payloads
- **Safe `hub.lease_seconds` parse** — wrapped in `try/except` with fallback to `_LEASE_SECONDS`; a malformed value no longer crashes the verification handler
- **`table_counts()` allowlist guard** — table names validated against a `frozenset` before interpolation into SQL; prevents accidental injection if the list is ever extended

### Fixed
- **`poll_loop` and `resubscribe_loop` no longer terminate on unhandled exceptions** — added `@poll_loop.error` and `@resubscribe_loop.error` handlers; previously an unexpected error in the outer loop body would cause `discord.ext.tasks` to cancel the loop permanently and silently stop all YouTube notifications

---

## [3.1.1] — 2026-10-10

### Fixed
- **Discord 429 rate-limit drops notifications** — `_send_notification` now retries up to 3 times on HTTP 429 responses with exponential backoff (5s/10s/20s, or Discord's own `retry_after` header if present). Previously a single 429 would silently drop the notification — it would not be marked as sent, and the next RSS poll would retry, but repeated 429s could mean the notification was never delivered. The bot now waits and retries within the same send attempt.

---

## [3.1.0] — 2026-10-03

### Fixed
- **Channel resolver mapping to connected brand accounts** — Reordered `CHANNEL_ID_PATTERNS` so `externalId` and `itemprop=channelId` are checked before the generic `"channelId":"UC..."` JSON key, which could previously match a connected alternate channel before reaching the page owner's ID (e.g. `@kingsiedgamer` resolving to `kingsied` instead of the main channel)
- **WebSub push handler dispatching connected account videos** — Guard check now rejects pushes whose `<yt:channelId>` is not in the tracked channel list *before* calling `_classify_and_build_item`, preventing stray oEmbed calls and notifications for connected brand accounts

### Changed
- `resolve_channel_id` now returns `tuple[str, str]` (`channel_id`, `channel_name`) with pattern-index logging for future diagnosis — all three callers (`add_channel`, `remove_yt`, `ytinfo`) updated
- `DESIGN.md` fully rewritten to reflect v3.x WebSub architecture (previous doc described v1.0 polling design)

---

## [3.0.0] — 2026-10-02

### Added
- **WebSub (PubSubHubbub) push engine** — Google delivers new uploads in seconds
- Embedded `aiohttp.web` HTTP server (`GET /youtube/webhook` challenge handler + `POST` Atom XML parser)
- `GET /health` endpoint for Railway health checks
- oEmbed content classification — video / short / live detection without HTML scraping
- `HEAD /shorts/VIDEO_ID` redirect detection for YouTube Shorts
- WebSub lease auto-renewal (`resubscribe_loop` every 6h, renews at T-24h before 7-day expiry)
- `lease_expires_at` and `websub_verified` columns in `yt_monitored_channels`
- `update_yt_websub_lease()` and `get_channels_needing_resubscription()` DB helpers
- Push status indicator (🟢/🟡) in `/list_yt`
- WebSub status line in `/about` and `/botstatus`
- `WEBHOOK_URL`, `WEBHOOK_PORT`, `FALLBACK_POLL_INTERVAL` environment variables
- Per-entry `try/except` in push payload processor — one bad entry can't kill others
- `asyncio.ensure_future` done-callback for silent exception detection

### Changed
- RSS fallback interval reduced from 900s (15 min) to 60s — active safety net alongside WebSub
- Bot version bumped to 3.0.0
- `/add_yt` now auto-subscribes to WebSub on channel add
- `/list_yt` now defers before DB read to prevent Unknown Interaction (10062) errors
- WebSub callback URL sanitized (strip trailing slash/fragment) before sending to Google hub

### Removed
- All HTML/JSON page scrapers (`_short_items_from_page`, `_live_items_from_page`, `_community_items_from_page`)
- `_extract_initial_data`, `_walk_json`, `scrape_pages`, `fetch_channel_activity`
- `ytInitialData` extraction, `shortsLockupViewModel` parsing, `backstageAttachment` scraping
- `_fetch_playlist_feed`, `_enrich_live_items`
- Deprecated `asyncio.get_event_loop().create_task()` → replaced with `asyncio.ensure_future()`

---

## [2.3.0] — 2026-09-15

### Added
- Railway volume persistence for SQLite (`DATABASE_PATH=/app/data/youtube_bot.db`)
- Auto-restore of log channel registrations after Railway redeploys
- Warmup seeding — seeds dedup cache on first poll to prevent historical video spam
- Cross-type deduplication — same `video_id` not re-sent as both `video` and `short`
- Exponential backoff for RSS 404 errors (1→2→4→8→16 cycle skip, max 80 min)
- `SYNC_COMMANDS` env gate — prevents Discord HTTP 429 on every restart
- `/sync` slash command for on-demand command tree sync

### Fixed
- `asyncio.get_event_loop()` deprecation warnings (Python 3.10+)
- `/setup_logs` "Application did not respond" — added defer before channel creation
- `on_message_edit` flooding audit log with bot/webhook edits
- Empty embed field in `on_user_update` when avatar didn't change
- `remove_yt` double-response crash on URL resolution path
- `format_mentions` incorrectly converting order numbers to user mentions
- Broken regex in `helpers.py` (double-escaped `\\b` → raw strings)

---

## [2.0.0] — 2026-08-01

### Added
- Multi-guild, per-route YouTube tracking (`yt_monitored_channels` table)
- 8-channel categorized server audit logging via `server_logger.py`
- `/announcement` with interactive preview, embed toggle, role ping, and link button
- `/say`, `/edit_say` staff messaging commands
- Trusted user RBAC system (`/trust add/remove/list`)
- Per-route content type filters (all / videos / shorts / live / community)
- Per-route role and user pings
- `yt_content_route_cache` for per-destination deduplication
- WAL journal mode for SQLite concurrent write safety

---

## [1.0.0] — 2026-07-01

### Added
- Initial release — single-guild YouTube RSS polling bot
- `/add_yt`, `/remove_yt`, `/list_yt`, `/ytinfo` commands
- SQLite deduplication via `yt_content_cache`
- discord.py slash commands with `app_commands`
