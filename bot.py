#!/usr/bin/env python3
"""
YouTube Telegram Bot - Webhook Version (Railway Ready)
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import uuid
import base64
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode, urlunparse

from dotenv import load_dotenv
from fastapi import FastAPI, Request
import uvicorn

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------- FASTAPI ---------------- #

app_web = FastAPI()
application: Application | None = None


# ---------------- COOKIE HANDLING ---------------- #

def write_cookies_file() -> str | None:
    b64 = os.getenv("YT_COOKIES_B64")
    if not b64:
        return None

    try:
        path = Path("/tmp/cookies.txt")
        path.write_bytes(base64.b64decode(b64))
        return str(path)
    except Exception:
        return None


# ---------------- CONSTANTS ---------------- #

START_KEYS = ("start", "t", "time_continue", "clip_start")
END_KEYS = ("end", "stop", "clip_end")

YOUTUBE_URL_RE = re.compile(
    r"(https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/[^\s]+)",
    re.IGNORECASE,
)

USER_SETTINGS: dict[int, dict[str, object]] = {}


# ---------------- DATA ---------------- #

@dataclass
class DownloadInfo:
    url: str
    is_clip: bool
    start: int | None = None
    end: int | None = None
    duration: int | None = None


class TimestampError(ValueError):
    pass


# ---------------- SETTINGS ---------------- #

def get_setting(user_id: int, key: str, default):
    return USER_SETTINGS.get(user_id, {}).get(key, default)


def set_setting(user_id: int, key: str, value):
    USER_SETTINGS.setdefault(user_id, {})[key] = value


# ---------------- UTILS ---------------- #

def check_dependency(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def extract_youtube_url(text: str) -> str | None:
    match = YOUTUBE_URL_RE.search(text)
    return match.group(1) if match else None


def remove_timestamp_params(url: str) -> str:
    parsed = urlparse(url)

    clean_query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k not in (*START_KEYS, *END_KEYS, "duration")
    ]

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(clean_query),
            "",
        )
    )


def parse_time(value: str | None) -> int | None:
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if ":" in value:
        parts = [int(x) for x in value.split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def extract_times(url: str):
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    def first(keys):
        for k in keys:
            if k in params:
                return params[k][0]
        return None

    start = parse_time(first(START_KEYS))
    end = parse_time(first(END_KEYS))

    if parsed.fragment.startswith("t="):
        start = parse_time(parsed.fragment[2:])

    return start, end


# ---------------- DOWNLOAD LOGIC ---------------- #

def build_download_info(url, default_duration, max_clip, allow_full):
    start, end = extract_times(url)

    if start is None:
        if not allow_full:
            raise TimestampError("Full downloads disabled")
        return DownloadInfo(url, False)

    if end is None:
        end = start + default_duration

    if end <= start:
        raise TimestampError("Invalid timestamp")

    duration = end - start

    if duration > max_clip:
        raise TimestampError("Clip too long")

    return DownloadInfo(url, True, start, end, duration)


def find_video(folder: Path) -> Path | None:
    files = [f for f in folder.iterdir() if f.suffix in {".mp4", ".mkv", ".webm"}]
    return max(files, key=lambda f: f.stat().st_size) if files else None


# ---------------- YT-DLP ---------------- #

async def run_ytdlp(download, output_dir, quality, accurate, force_mp4, fragments):

    cookies_file = write_cookies_file()

    cmd = ["yt-dlp"]

    if cookies_file:
        cmd += ["--cookies", cookies_file]

    if download.is_clip:
        section = f"*{download.start}-{download.end}"

        cmd += [
            "--download-sections",
            section,
            "--no-playlist",
            "--restrict-filenames",
            "-f",
            quality,
            "-o",
            str(output_dir / "%(title)s.%(ext)s"),
            remove_timestamp_params(download.url),
        ]
    else:
        cmd += [
            "--no-playlist",
            "--restrict-filenames",
            "-f",
            quality,
            "-o",
            str(output_dir / "%(title)s.%(ext)s"),
            download.url,
        ]

    if force_mp4:
        cmd.insert(1, "--merge-output-format=mp4")

    process = await asyncio.create_subprocess_exec(*cmd)
    await process.wait()

    return find_video(output_dir)


# ---------------- TELEGRAM HANDLERS ---------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is running via webhook 🚀")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = extract_youtube_url(update.message.text or "")

    if not url:
        await update.message.reply_text("Send a valid YouTube link.")
        return

    await update.message.reply_text("Downloading...")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            video = await run_ytdlp(
                build_download_info(
                    url,
                    int(os.getenv("DEFAULT_DURATION", "30")),
                    int(os.getenv("MAX_DURATION", "120")),
                    True,
                ),
                Path(tmp),
                "best",
                False,
                True,
                8,
            )

            if not video:
                await update.message.reply_text("Download failed.")
                return

            with video.open("rb") as f:
                await update.message.reply_video(video=f)

    except Exception as e:
        await update.message.reply_text(f"Error:\n{e}")


# ---------------- WEBHOOK ROUTES ---------------- #

@app_web.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}


@app_web.get("/")
def health():
    return {"status": "alive"}


# ---------------- SETUP WEBHOOK ---------------- #

async def setup_webhook():
    url = os.getenv("WEBHOOK_URL")

    await application.bot.set_webhook(
        url=f"{url}/webhook",
        drop_pending_updates=True
    )

    print("Webhook set:", url)


# ---------------- MAIN ---------------- #

def main():
    global application

    load_dotenv()

    token = os.getenv("YT_BOT_TOKEN")

    if not token:
        raise SystemExit("Missing YT_BOT_TOKEN")

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    asyncio.get_event_loop().run_until_complete(setup_webhook())

    port = int(os.getenv("PORT", "8000"))

    uvicorn.run(app_web, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
