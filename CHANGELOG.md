# Changelog

All notable changes to this project are documented here.
Format: `[version] — date — summary`

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
