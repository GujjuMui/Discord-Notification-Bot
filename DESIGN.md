# YouTube Notification Bot - Design Document

## Overview
A Discord bot that monitors YouTube channels and sends notifications to Discord when new videos are uploaded.

## Core Features

### 1. YouTube Channel Monitoring
- Track multiple YouTube channels simultaneously
- Check for new video uploads at configurable intervals
- Support both YouTube Data API v3 and RSS feed methods
- Store video metadata (title, URL, thumbnail, publish time)

### 2. Discord Integration
- Send formatted embed messages to designated Discord channels
- Support multiple Discord servers
- Customizable notification messages per channel
- Role mention support (@everyone, @here, or custom roles)

### 3. Data Persistence
- Store tracked YouTube channels
- Track last-checked video IDs to avoid duplicate notifications
- Store Discord channel mappings
- Configuration management

### 4. Admin Commands
- Add/remove YouTube channels to track
- Set notification Discord channels
- Configure check intervals
- List all tracked channels
- Test notifications

## Technical Architecture

### Technology Stack
- **Language**: Python 3.9+
- **Discord Library**: discord.py 2.x
- **HTTP Client**: aiohttp (async)
- **Database**: SQLite (local) or PostgreSQL (production)
- **YouTube API**: Google YouTube Data API v3
- **Environment Management**: python-dotenv

### Core Components

#### 1. Bot Core (`bot.py`)
```
Main bot initialization
Command handler registration
Event loop management
```

#### 2. YouTube Monitor (`youtube_monitor.py`)
```
Periodic checking logic
API interaction
Video data fetching
Change detection
```

#### 3. Database Manager (`database.py`)
```
Channel tracking
Video history
Configuration storage
CRUD operations
```

#### 4. Notification Handler (`notifications.py`)
```
Discord embed creation
Message formatting
Error handling
Rate limit management
```

#### 5. Configuration (`config.py`)
```
Environment variables
Default settings
API keys management
```

## Data Models

### YouTube Channel
```python
{
    "id": "unique_id",
    "youtube_channel_id": "UC...",
    "youtube_channel_name": "Channel Name",
    "last_video_id": "video_id",
    "last_checked": "timestamp",
    "check_interval": 300,  # seconds
    "active": true
}
```

### Discord Mapping
```python
{
    "id": "unique_id",
    "youtube_channel_id": "UC...",
    "discord_guild_id": "123456789",
    "discord_channel_id": "987654321",
    "notification_message": "New video from {channel}!",
    "mention_role_id": null,  # optional
    "active": true
}
```

### Video Record
```python
{
    "id": "unique_id",
    "video_id": "youtube_video_id",
    "youtube_channel_id": "UC...",
    "title": "Video Title",
    "url": "https://youtube.com/watch?v=...",
    "thumbnail_url": "https://...",
    "published_at": "timestamp",
    "notified_at": "timestamp"
}
```

## Bot Commands

### Admin Commands (requires permissions)
- `/yt add <channel_url> [check_interval]` - Add YouTube channel to track
- `/yt remove <channel_id>` - Remove tracked channel
- `/yt list` - List all tracked channels
- `/yt setchannel <youtube_channel> <discord_channel>` - Set notification channel
- `/yt test <youtube_channel>` - Send test notification
- `/yt interval <youtube_channel> <seconds>` - Change check interval
- `/yt mention <youtube_channel> <role>` - Set role to mention
- `/yt status` - Show bot status and stats

### User Commands
- `/yt help` - Show help message
- `/yt info <youtube_channel>` - Show channel tracking info

## YouTube API Integration

### Method 1: YouTube Data API v3
**Pros:**
- Official API
- Detailed metadata
- Real-time data
- Better reliability

**Cons:**
- Requires API key
- Daily quota limits (10,000 units)
- Each request costs units

**Endpoint:** `GET https://www.googleapis.com/youtube/v3/search`
**Parameters:**
- `part=snippet`
- `channelId=UC...`
- `order=date`
- `maxResults=1`
- `type=video`

### Method 2: RSS Feed
**Pros:**
- No API key needed
- No quota limits
- Simple XML parsing

**Cons:**
- Limited metadata
- Slight delays
- No thumbnails

**URL:** `https://www.youtube.com/feeds/videos.xml?channel_id=UC...`

### Recommended Approach
Use RSS as primary method with API as fallback for enhanced metadata.

## Monitoring Strategy

### Polling Loop
```
1. Every N seconds (configurable per channel):
   - Fetch latest video from YouTube
   - Compare with last known video ID
   - If new video detected:
     - Store video metadata
     - Send Discord notifications
     - Update last known video ID
   - Handle rate limits and errors
```

### Optimization
- Stagger checks for multiple channels
- Implement exponential backoff on errors
- Cache channel metadata
- Batch database operations

## Discord Notification Format

### Embed Structure
```
┌─────────────────────────────────────┐
│ 🎥 New Video from [Channel Name]    │
├─────────────────────────────────────┤
│ [Video Thumbnail]                   │
│                                     │
│ Title: [Video Title]                │
│ Link: [YouTube URL]                 │
│ Published: [X minutes ago]          │
└─────────────────────────────────────┘
@Role mention (if configured)
```

### Color Coding
- New video: Red (#FF0000 - YouTube red)
- Test notification: Blue (#0099FF)
- Error notification: Orange (#FF9900)

## Error Handling

### YouTube API Errors
- Quota exceeded: Switch to RSS feed, log warning
- Invalid channel: Disable tracking, notify admin
- Network errors: Retry with exponential backoff
- Rate limits: Respect and queue requests

### Discord Errors
- Invalid channel: Disable mapping, notify admin
- Missing permissions: Log error, notify admin
- Rate limits: Queue messages, implement backoff

### Database Errors
- Connection loss: Retry with backoff
- Lock conflicts: Implement retry logic
- Corruption: Backup and recovery system

## Security Considerations

### API Keys
- Store in environment variables (.env)
- Never commit to version control
- Rotate periodically

### Permissions
- Bot requires minimal Discord permissions:
  - Send Messages
  - Embed Links
  - Mention Roles (if used)
- Admin commands require Manage Server permission

### Rate Limiting
- Implement request queuing
- Respect API rate limits
- Add cooldowns to commands

## Configuration Files

### .env
```
DISCORD_BOT_TOKEN=your_bot_token
YOUTUBE_API_KEY=your_youtube_api_key
DATABASE_URL=sqlite:///youtube_bot.db
CHECK_INTERVAL=300
LOG_LEVEL=INFO
```

### requirements.txt
```
discord.py>=2.0.0
aiohttp>=3.8.0
python-dotenv>=0.19.0
feedparser>=6.0.0
google-api-python-client>=2.0.0
sqlalchemy>=2.0.0
aiosqlite>=0.17.0
```

## Database Schema

### SQLite Schema
```sql
CREATE TABLE youtube_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    youtube_channel_id TEXT UNIQUE NOT NULL,
    channel_name TEXT NOT NULL,
    last_video_id TEXT,
    last_checked TIMESTAMP,
    check_interval INTEGER DEFAULT 300,
    active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE discord_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    youtube_channel_id TEXT NOT NULL,
    discord_guild_id TEXT NOT NULL,
    discord_channel_id TEXT NOT NULL,
    notification_message TEXT,
    mention_role_id TEXT,
    active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (youtube_channel_id) REFERENCES youtube_channels(youtube_channel_id)
);

CREATE TABLE video_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT UNIQUE NOT NULL,
    youtube_channel_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    thumbnail_url TEXT,
    published_at TIMESTAMP,
    notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (youtube_channel_id) REFERENCES youtube_channels(youtube_channel_id)
);

CREATE INDEX idx_youtube_channel_id ON youtube_channels(youtube_channel_id);
CREATE INDEX idx_discord_guild ON discord_mappings(discord_guild_id);
CREATE INDEX idx_video_id ON video_history(video_id);
```

## Deployment

### Local Development
1. Clone repository
2. Install dependencies: `pip install -r requirements.txt`
3. Create .env file with credentials
4. Run: `python bot.py`

### Production Options

#### Option 1: VPS/Cloud Server
- Deploy to DigitalOcean, AWS EC2, or similar
- Use systemd service for auto-restart
- Set up logging and monitoring

#### Option 2: Docker Container
- Create Dockerfile
- Use docker-compose for easy deployment
- Persistent volume for database

#### Option 3: Platform as a Service
- Deploy to Railway, Heroku, or Render
- Use environment variables for config
- Connect to managed database

### Monitoring
- Log all notifications sent
- Track API usage and quota
- Monitor bot uptime
- Alert on errors

## Future Enhancements

### Phase 2
- Web dashboard for management
- Multiple notification formats
- Video filtering (duration, keywords)
- Live stream notifications
- Statistics and analytics

### Phase 3
- Support for other platforms (Twitch, Twitter)
- Custom notification templates
- Webhook support
- Multi-language support
- Advanced scheduling

## Testing Strategy

### Unit Tests
- YouTube API client
- Database operations
- Command parsing
- Notification formatting

### Integration Tests
- End-to-end notification flow
- API failure scenarios
- Database persistence
- Discord message delivery

### Manual Testing
- Add/remove channels
- Test notifications
- Permission checks
- Error handling

## Performance Considerations

### Scalability
- Current design supports ~100 channels
- Each channel checked every 5 minutes
- ~20 API requests per minute

### Optimization Opportunities
- Implement caching layer
- Batch API requests
- Use webhooks if available
- Connection pooling for database

## File Structure
```
discord-notif-bot/
├── bot.py                 # Main bot entry point
├── cogs/
│   └── youtube.py         # YouTube commands cog
├── services/
│   ├── youtube_monitor.py # YouTube monitoring service
│   ├── youtube_api.py     # YouTube API client
│   └── notifications.py   # Notification handler
├── models/
│   ├── database.py        # Database models and operations
│   └── schemas.py         # Data schemas
├── utils/
│   ├── config.py          # Configuration management
│   ├── logger.py          # Logging setup
│   └── helpers.py         # Helper functions
├── .env.example           # Example environment variables
├── requirements.txt       # Python dependencies
├── README.md             # Project documentation
├── DESIGN.md             # This file
└── tests/                # Test files
    ├── test_youtube.py
    ├── test_database.py
    └── test_notifications.py
```

## Getting Started Guide

### Prerequisites
1. Python 3.9 or higher
2. Discord Bot Token (from Discord Developer Portal)
3. YouTube Data API Key (from Google Cloud Console)
4. Discord server with admin permissions

### Setup Steps
1. Create Discord bot and get token
2. Enable YouTube Data API v3 and get API key
3. Install Python dependencies
4. Configure environment variables
5. Initialize database
6. Invite bot to Discord server
7. Run bot and configure channels

## Maintenance

### Regular Tasks
- Monitor API quota usage
- Check error logs
- Update dependencies
- Backup database
- Review and optimize queries

### Troubleshooting
- Bot not responding: Check token and permissions
- No notifications: Verify API key and channel IDs
- Duplicate notifications: Check database state
- Rate limit errors: Adjust check intervals

---

**Version:** 1.0  
**Last Updated:** 2026-09-28  
**Status:** Design Complete - Ready for Implementation
