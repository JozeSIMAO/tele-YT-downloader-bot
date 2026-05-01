# YouTube Telegram Bot - Any YouTube URL

This version supports:

```text
Normal YouTube URL      -> downloads the full video
Timestamped YouTube URL -> downloads only that timestamp section
```

Default quality is **480p**.

## Install on Arch Linux

```bash
sudo pacman -S python python-pip yt-dlp ffmpeg
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Setup

```bash
cp .env.example .env
nano .env
```

Paste your Telegram bot token:

```text
BOT_TOKEN=your_real_token_here
```

Make sure this is enabled:

```text
ALLOW_FULL_VIDEO_DOWNLOADS=true
```

## Run

```bash
source .venv/bin/activate
python bot.py
```

## Supported links

```text
https://youtu.be/VIDEO_ID
https://www.youtube.com/watch?v=VIDEO_ID
https://m.youtube.com/watch?v=VIDEO_ID
https://music.youtube.com/watch?v=VIDEO_ID
https://youtube.com/shorts/VIDEO_ID
https://youtu.be/VIDEO_ID?t=1m30s
https://www.youtube.com/watch?v=VIDEO_ID&start=90&end=120
```

## Bot commands

```text
/settings
/quality 360
/quality 480
/quality 720
/duration 20
/accurate off
```

For full videos, Telegram may reject large files. Use:

```text
/quality 360
```

for smaller files.
