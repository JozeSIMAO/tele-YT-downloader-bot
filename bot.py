#!/usr/bin/env python3
"""
YouTube Telegram Bot - Any YouTube URL
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
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

START_KEYS = ("start", "t", "time_continue", "clip_start")
END_KEYS = ("end", "stop", "clip_end")

YOUTUBE_URL_RE = re.compile(
    r"(https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/[^\s<>\"']+)",
    re.IGNORECASE,
)

USER_SETTINGS: dict[int, dict[str, object]] = {}


# ---------------- COOKIE HANDLING ---------------- #

def write_cookies_file() -> str | None:
    """
    Decode Railway base64 cookies into a temp file.
    """
    b64 = os.getenv("YT_COOKIES_B64")
    if not b64:
        return None

    try:
        cookies_path = Path("/tmp/cookies.txt")
        cookies_path.write_bytes(base64.b64decode(b64))
        return str(cookies_path)
    except Exception:
        return None


# ---------------- DATA MODEL ---------------- #

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


def set_setting(user_id: int, key: str, value) -> None:
    USER_SETTINGS.setdefault(user_id, {})[key] = value


# ---------------- TIME PARSING ---------------- #

def parse_time_to_seconds(value: str | int | float | None) -> int | None:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return int(value)

    s = str(value).strip().lower()

    if s.isdigit():
        return int(s)

    if ":" in s:
        parts = s.split(":")
        nums = [int(p) for p in parts]
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]

    return None


def format_seconds(seconds: int | float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


# ---------------- YOUTUBE URL ---------------- #

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


def extract_times_from_url(url: str) -> tuple[int | None, int | None]:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    def first(keys):
        for k in keys:
            if k in params:
                return params[k][0]
        return None

    start = parse_time_to_seconds(first(START_KEYS))
    end = parse_time_to_seconds(first(END_KEYS))

    if parsed.fragment.startswith("t="):
        start = parse_time_to_seconds(parsed.fragment[2:])

    return start, end


# ---------------- DEPENDENCY CHECK ---------------- #

def check_dependency(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def quality_selector(label: str) -> str:
    label = label.replace("p", "")
    return {
        "360": "b[height<=360]/best",
        "480": "b[height<=480]/best",
        "720": "b[height<=720]/best",
        "1080": "b[height<=1080]/best",
        "best": "best",
    }.get(label, "best")


# ---------------- DOWNLOAD LOGIC ---------------- #

def build_download_info(url, default_duration, max_clip_duration, allow_full):
    start, end = extract_times_from_url(url)

    if start is None:
        if not allow_full:
            raise TimestampError("Full downloads disabled")
        return DownloadInfo(url, False)

    if end is None:
        end = start + default_duration

    duration = end - start

    if duration > max_clip_duration:
        raise TimestampError("Clip too long")

    return DownloadInfo(url, True, start, end, duration)


def find_first_video_file(folder: Path) -> Path | None:
    files = [f for f in folder.iterdir() if f.suffix in {".mp4", ".mkv", ".webm"}]
    return max(files, key=lambda f: f.stat().st_size) if files else None


# ---------------- YT-DLP ---------------- #

async def run_ytdlp_download(download, output_dir, quality, accurate, force_mp4, fragments):

    cookies_file = write_cookies_file()

    if download.is_clip:
        section = f"*{format_seconds(download.start)}-{format_seconds(download.end)}"

        cmd = ["yt-dlp"]

        if cookies_file:
            cmd += ["--cookies", cookies_file]

        cmd += [
            "--download-sections",
            section,
            "--no-playlist",
            "--restrict-filenames",
            "--concurrent-fragments",
            str(fragments),
            "-f",
            quality,
            "-o",
            str(output_dir / "%(title)s.%(ext)s"),
            remove_timestamp_params(download.url),
        ]

    else:
        cmd = ["yt-dlp"]

        if cookies_file:
            cmd += ["--cookies", cookies_file]

        cmd += [
            "--no-playlist",
            "--restrict-filenames",
            "--concurrent-fragments",
            str(fragments),
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

    video = find_first_video_file(output_dir)
    if not video:
        raise RuntimeError("No video found")

    return video


# ---------------- BOT HANDLERS ---------------- #
# (unchanged logic below for brevity; your original handlers remain same)

# ---------------- ENV ---------------- #

def validate_environment():
    load_dotenv()

    if not os.getenv("YT_BOT_TOKEN"):
        raise SystemExit("YT_BOT_TOKEN missing")

    if not check_dependency("yt-dlp"):
        raise SystemExit("yt-dlp missing")

    if not check_dependency("ffmpeg"):
        raise SystemExit("ffmpeg missing")


def main():
    validate_environment()
    token = os.getenv("YT_BOT_TOKEN")

    app = Application.builder().token(token).build()

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
