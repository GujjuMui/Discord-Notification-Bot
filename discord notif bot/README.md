# YouTube Notification Bot

A production-ready Discord bot that monitors YouTube channels and sends rich notifications when new videos are published.

## Features

- **RSS Feed Monitoring**: Polls YouTube RSS feeds every 60 seconds for instant detection
- **Rich Discord Embeds**: Beautiful notifications with thumbnails, video info, and direct links
- **Multi-Channel Support**: Track unlimited YouTube channels across multiple Discord servers
- **Deduplication**: SQLite database prevents duplicate notifications
- **Slash Commands**: Modern Discord slash commands for easy management
- **Content Type Detection**: Supports videos, YouTube Shorts, and live streams
- **Admin Controls**: Pause/resume tracking, set notification channels

## Quick Start

### 1. Prerequisites

- Python 3.9 or higher
- A Discord Bot Token ([Get one here](https://discord.com/developers/applications))
- A Discord Server where you have admin permissions

### 2. Installation

```bash
# Clone or download the project
cd "discord notif bot"

# Create virtual environment (recommended)
python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configuration

```bash
# Copy the example environment file
copy .env.example .env

# Edit .env with your bot token
# DISCORD_BOT_TOKEN=your_actual_bot_token_here
# DISCORD_CHANNEL_ID=your_channel_id_here
```

### 4. Create Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click "New Application" and name it
3. Go to "Bot" section and click "Add Bot"
4. Copy the **Token** and paste it in your `.env` file
5. Enable these **Privileged Gateway Intents**:
   - Message Content Intent
6. Go to "OAuth2" > "URL Generator"
7. Select scopes: `bot`, `applications.commands`
8. Select permissions: `Send Messages`, `Embed Links`, `Use Slash Commands`
9. Copy the generated URL and invite the bot to your server

### 5. Run the Bot

```bash
python main.py
```

## Commands

| Command | Description | Permission |
|---------|-------------|------------|
| `/addchannel <url>` | Add a YouTube channel to track | Manage Server |
| `/removechannel <channel_id>` | Stop tracking a channel | Manage Server |
| `/listchannels` | Show all tracked channels | Manage Server |
| `/settarget <channel>` | Set Discord notification channel | Manage Server |
| `/pausechannel <channel_id>` | Pause notifications for a channel | Manage Server |
| `/resumechannel <channel_id>` | Resume notifications | Manage Server |
| `/ytinfo <url>` | Get info about a YouTube channel | Manage Server |
| `!ping` | Check bot latency | Everyone |
| `!help` | Show help message | Everyone |

## Adding YouTube Channels

### Supported URL Formats

- `https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxxxx` (Channel ID URL)
- `https://www.youtube.com/@handle` (Handle URL)
- `https://www.youtube.com/c/CustomName` (Custom URL - requires manual lookup)

### Example Usage

```
/addchannel https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw
```

## Database Schema

The bot uses SQLite with three tables:

### `channels`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| channel_id | TEXT | YouTube channel ID |
| channel_name | TEXT | Channel display name |
| channel_url | TEXT | YouTube channel URL |
| discord_channel_id | INTEGER | Discord notification channel |
| is_active | INTEGER | Tracking status (1=active, 0=paused) |

### `videos`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| video_id | TEXT | YouTube video ID (unique) |
| channel_id | TEXT | Associated channel |
| title | TEXT | Video title |
| video_url | TEXT | Direct video URL |
| thumbnail_url | TEXT | Thumbnail URL |
| is_short | INTEGER | YouTube Short flag |
| is_live | INTEGER | Live stream flag |

### `settings`
| Column | Type | Description |
|--------|------|-------------|
| key | TEXT | Setting key |
| value | TEXT | Setting value |

## Project Structure

```
discord notif bot/
├── main.py              # Bot entry point
├── config.py            # Environment configuration
├── database.py          # SQLite database manager
├── requirements.txt     # Python dependencies
├── .env.example         # Environment template
├── .env                 # Your configuration (create this)
├── youtube_bot.db       # SQLite database (auto-created)
├── README.md            # This file
└── cogs/
    ├── __init__.py
    └── youtube_tracker.py  # YouTube monitoring logic
```

## Deployment

### Docker

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
```

```bash
docker build -t yt-notification-bot .
docker run -d --name yt-bot --env-file .env yt-notification-bot
```

### Systemd (Linux)

Create `/etc/systemd/system/yt-bot.service`:

```ini
[Unit]
Description=YouTube Notification Bot
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/discord-notif-bot
ExecStart=/path/to/discord-notif-bot/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable yt-bot
sudo systemctl start yt-bot
```

## Troubleshooting

### Bot doesn't respond to commands
- Ensure the bot has `applications.commands` scope
- Check that slash commands are synced (check logs)
- Verify the bot has permissions in the channel

### No notifications being sent
- Check if channels are being tracked: `/listchannels`
- Verify Discord notification channel is set: `/settarget`
- Check bot logs for RSS feed errors
- Ensure channels are not paused

### "Could not extract channel ID" error
- Use the full channel URL format: `https://www.youtube.com/channel/UC...`
- Custom URLs (`/c/name`) may require the actual channel ID

## API Reference

The bot uses YouTube's RSS feeds which don't require API keys:

```
https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## License

MIT License - See LICENSE file for details

## Support

For issues or feature requests, please open a GitHub issue.