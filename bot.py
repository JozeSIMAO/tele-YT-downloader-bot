#!/usr/bin/env python3
"""
YouTube Telegram Bot - Automatically processes YouTube URLs sent in messages.

Designed for Railway deployment.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import uuid
import base64
from pathlib import Path
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode, urlunparse

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ---------------- APP STATE ---------------- #

app_web = None
application = None

# ---------------- REGEX ---------------- #

YOUTUBE_URL_RE = re.compile(
    r"(https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/[^\s<>\"']+)",
    re.IGNORECASE,
)

# ---------------- ENV & CONFIG ---------------- #

def load_env():
    load_dotenv()

def get_env(key: str, default=None):
    return os.getenv(key, default)

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

# ---------------- URL & Timestamp Parsing ---------------- #

def extract_url(text: str) -> str | None:
    m = YOUTUBE_URL_RE.search(text)
    return m.group(1) if m else None

def remove_params(url: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k not in {"t", "start", "end"}]
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), ""))

def format_seconds(seconds: int | float) -> str:
    seconds = int(round(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def extract_times_from_url(url: str) -> tuple[int | None, int | None]:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    start_raw = first_param(params, ("start", "t", "time_continue", "clip_start"))
    end_raw = first_param(params, ("end", "stop", "clip_end"))

    if not start_raw and parsed.fragment:
        if parsed.fragment.startswith("t="):
            start_raw = parsed.fragment[2:]
        elif parsed.fragment.startswith("start="):
            start_raw = parsed.fragment.split("=", 1)[1]

    start = parse_time_to_seconds(start_raw)
    end = parse_time_to_seconds(end_raw)
    return start, end

def first_param(params: dict[str, list[str]], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in params and params[key]:
            return params[key][0]
    return None

def parse_time_to_seconds(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = int(value)
        return seconds if seconds >= 0 else None
    s = str(value).strip().lower()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if ":" in s:
        parts = s.split(":")
        if not all(part.strip().isdigit() for part in parts):
            raise ValueError(f"Invalid timestamp: {value}")
        nums = [int(part) for part in parts]
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        raise ValueError(f"Invalid timestamp: {value}")
    match = re.fullmatch(
        r"(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?",
        s,
    )
    if match:
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        seconds = int(match.group("seconds") or 0)
        total = hours * 3600 + minutes * 60 + seconds
        if total > 0 or s in {"0", "0s"}:
            return total
    raise ValueError(f"Could not understand timestamp: {value}")

# ---------------- Download Info & Functions ---------------- #

from dataclasses import dataclass

@dataclass
class DownloadInfo:
    url: str
    is_clip: bool
    start: int | None = None
    end: int | None = None
    duration: int | None = None

def build_download_info(
    url: str,
    default_duration: int,
    max_clip_duration: int,
    allow_full_video: bool,
) -> DownloadInfo:
    start, end = extract_times_from_url(url)
    if start is None:
        if not allow_full_video:
            raise ValueError("Full-video downloads are disabled.")
        return DownloadInfo(url=url, is_clip=False)
    if end is None:
        end = start + default_duration
    if end <= start:
        raise ValueError("End time must be after start time.")
    duration = end - start
    if duration > max_clip_duration:
        raise ValueError(
            f"Clip duration {duration}s exceeds maximum of {max_clip_duration}s."
        )
    return DownloadInfo(
        url=url,
        is_clip=True,
        start=start,
        end=end,
        duration=duration,
    )

def find_first_video_file(folder: Path) -> Path | None:
    video_exts = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
    files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in video_exts]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_size)

import asyncio

async def run_ytdlp_download(
    download: DownloadInfo,
    output_dir: Path,
    quality: str,
    accurate: bool,
    force_mp4: bool,
    fragments: int,
) -> Path:
    if download.is_clip:
        clean_url = remove_params(download.url)
        section = f"*{format_seconds(download.start)}-{format_seconds(download.end)}"
        output_template = str(output_dir / "%(title).80s_%(section_start)s-%(section_end)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--download-sections",
            section,
            "--no-playlist",
            "--restrict-filenames",
            "--concurrent-fragments",
            str(fragments),
            "-f",
            quality,
            "-o",
            output_template,
            clean_url,
        ]
        if accurate:
            cmd.insert(1, "--force-keyframes-at-cuts")
    else:
        output_template = str(output_dir / "%(title).80s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--restrict-filenames",
            "--concurrent-fragments",
            str(fragments),
            "-f",
            quality,
            "-o",
            output_template,
            download.url,
        ]
    if force_mp4:
        cmd.insert(1, "--merge-output-format")
        cmd.insert(2, "mp4")
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output_chunks = []
    assert process.stdout is not None
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="replace").strip()
        output_chunks.append(decoded)
        if len(output_chunks) > 80:
            output_chunks = output_chunks[-80:]
    return_code = await process.wait()
    if return_code != 0:
        raise RuntimeError(f"yt-dlp failed with code {return_code}\nLast lines:\n" + "\n".join(output_chunks[-20:]))
    video_file = find_first_video_file(output_dir)
    if not video_file:
        raise RuntimeError("Download completed but no video file found.")
    return video_file

# ---------------- Quality Selector ---------------- #

def quality_selector(label: str) -> str:
    label = label.strip().lower().replace("p", "")
    if label == "360":
        return "b[height<=360]/best[height<=360]/best"
    elif label == "480":
        return "b[height<=480]/best[height<=480]/best"
    elif label == "720":
        return "b[height<=720]/best[height<=720]/best"
    elif label == "1080":
        return "b[height<=1080]/best[height<=1080]/best"
    elif label in {"best", "max"}:
        return "best"
    else:
        raise ValueError("Unsupported quality")

# ---------------- Telegram Handlers ---------------- #

async def send_typing_loop(update: Update, stop_event: asyncio.Event):
    if not update.effective_chat:
        return
    while not stop_event.is_set():
        try:
            await update.effective_chat.send_action(ChatAction.UPLOAD_VIDEO)
        except Exception:
            pass
        await asyncio.sleep(4)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Hi! Send me a YouTube link and I’ll download it.\n\n"
        "It will automatically detect timestamp parameters if present.\n"
        "Just send the link, and I will process it.\n\n"
        "Commands:\n"
        "/help - show help\n"
        "/settings - show current settings\n"
        "/duration <seconds> - set default clip duration\n"
        "/quality <360|480|720|1080|best> - set quality\n"
        "/accurate on|off - toggle accurate cuts\n"
    )
    await update.message.reply_text(msg)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# Placeholder for /settings, /duration, /quality, /accurate commands...
# You can implement these as needed, similar to your previous code.

# ---------------- Main message handler ---------------- #

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    url = extract_url(text)

    if not url:
        return  # Not a YouTube URL, ignore

    # Fetch settings from environment
    default_duration = int(os.getenv("DEFAULT_DURATION", "30"))
    max_duration = int(os.getenv("MAX_DURATION", "120"))
    allow_full_video = os.getenv("ALLOW_FULL_VIDEO_DOWNLOADS", "true").lower() == "true"
    quality_label = os.getenv("DEFAULT_QUALITY_LABEL", "480")
    try:
        quality = quality_selector(quality_label)
    except Exception:
        quality = "best"
    accurate = os.getenv("ACCURATE_MODE", "false").lower() == "true"
    force_mp4 = os.getenv("FORCE_MP4", "true").lower() == "true"
    fragments = int(os.getenv("CONCURRENT_FRAGMENTS", "8"))

    # Build download info
    try:
        download_info = build_download_info(
            url=url,
            default_duration=default_duration,
            max_clip_duration=max_duration,
            allow_full_video=allow_full_video,
        )
    except Exception as e:
        await update.message.reply_text(str(e))
        return

    # Notify user
    if download_info.is_clip:
        await update.message.reply_text(
            f"Downloading clip: {format_seconds(download_info.start)} to {format_seconds(download_info.end)}"
        )
    else:
        await update.message.reply_text("Downloading full video...")

    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(send_typing_loop(update, stop_event))
    try:
        with tempfile.TemporaryDirectory(prefix=f"ytbot_{uuid.uuid4().hex}_") as tmp_dir:
            temp_path = Path(tmp_dir)
            video_file = await run_ytdlp_download(
                download=download_info,
                output_dir=temp_path,
                quality=quality,
                accurate=accurate,
                force_mp4=force_mp4,
                fragments=fragments,
            )

            max_file_size_mb = int(os.getenv("MAX_TELEGRAM_FILE_MB", "45"))
            file_size_mb = video_file.stat().st_size / (1024 * 1024)

            if file_size_mb > max_file_size_mb:
                await update.message.reply_text(
                    f"The video is too large to send ({file_size_mb:.1f} MB). Try /quality 360."
                )
                return

            caption = (
                f"Here is your clip: {format_seconds(download_info.start)} to {format_seconds(download_info.end)}"
                if download_info.is_clip
                else "Here is your full video."
            )

            with video_file.open("rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption=caption,
                    supports_streaming=True,
                )

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
    finally:
        stop_event.set()
        try:
            await asyncio.sleep(0)  # allow cancellation
        except:
            pass
        # Cancel typing task if still running
        if not typing_task.done():
            typing_task.cancel()

# ---------------- Main setup ---------------- #

def main():
    load_env()

    token = get_env("YT_BOT_TOKEN")
    if not token:
        raise SystemExit("YT_BOT_TOKEN is missing. Set it in your environment variables.")

    # Compatibility fix
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    global application
    application = Application.builder().token(token).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    # You can add commands for settings, duration, quality, etc., if needed.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running. Press Ctrl+C to stop.")
    application.run_polling()

if __name__ == "__main__":
    main()
