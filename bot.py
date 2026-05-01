#!/usr/bin/env python3
"""
YouTube Telegram Bot - Any YouTube URL

Normal YouTube link      -> downloads the full video
Timestamped YouTube link -> downloads only that timestamp section

Use this only for videos you own, have permission to download,
or videos where downloading is legally allowed.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import uuid
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


@dataclass
class DownloadInfo:
    url: str
    is_clip: bool
    start: int | None = None
    end: int | None = None
    duration: int | None = None


class TimestampError(ValueError):
    pass


def get_setting(user_id: int, key: str, default):
    return USER_SETTINGS.get(user_id, {}).get(key, default)


def set_setting(user_id: int, key: str, value) -> None:
    USER_SETTINGS.setdefault(user_id, {})[key] = value


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
            raise TimestampError(f"Invalid timestamp: {value}")

        nums = [int(part) for part in parts]

        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]

        raise TimestampError(f"Invalid timestamp: {value}")

    match = re.fullmatch(
        r"(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s?)?",
        s,
    )

    if match:
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        seconds = int(match.group("seconds") or 0)
        total = hours * 3600 + minutes * 60 + seconds
        if total > 0 or s in {"0", "0s"}:
            return total

    raise TimestampError(f"Could not understand timestamp: {value}")


def format_seconds(seconds: int | float) -> str:
    seconds = int(round(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def first_param(params: dict[str, list[str]], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in params and params[key]:
            return params[key][0]
    return None


def extract_youtube_url(text: str) -> str | None:
    match = YOUTUBE_URL_RE.search(text)
    if not match:
        return None

    url = match.group(1).strip().rstrip(").,;!?'\"")
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    allowed_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }

    if host not in allowed_hosts:
        return None

    return url


def remove_timestamp_params(url: str) -> str:
    parsed = urlparse(url)
    remove_keys = set(START_KEYS + END_KEYS + ("duration",))

    clean_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in remove_keys
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

    start_raw = first_param(params, START_KEYS)
    end_raw = first_param(params, END_KEYS)

    if not start_raw and parsed.fragment:
        if parsed.fragment.startswith("t="):
            start_raw = parsed.fragment[2:]
        elif parsed.fragment.startswith("start="):
            start_raw = parsed.fragment.split("=", 1)[1]

    start = parse_time_to_seconds(start_raw)
    end = parse_time_to_seconds(end_raw)

    return start, end


def check_dependency(command: str) -> bool:
    return shutil.which(command) is not None


def quality_selector(label: str) -> str:
    label = label.strip().lower().replace("p", "")

    if label == "360":
        return "b[height<=360]/best[height<=360]/best"
    if label == "480":
        return "b[height<=480]/best[height<=480]/best"
    if label == "720":
        return "b[height<=720]/best[height<=720]/best"
    if label == "1080":
        return "b[height<=1080]/best[height<=1080]/best"
    if label in {"best", "max"}:
        return "best"

    raise ValueError("Unsupported quality")


def build_download_info(
    url: str,
    default_duration: int,
    max_clip_duration: int,
    allow_full_video: bool,
) -> DownloadInfo:
    """
    IMPORTANT:
    - No timestamp means full video.
    - Timestamp means selected clip.
    """
    start, end = extract_times_from_url(url)

    if start is None:
        if not allow_full_video:
            raise TimestampError("Full-video downloads are disabled in this bot.")
        return DownloadInfo(url=url, is_clip=False)

    if end is None:
        end = start + default_duration

    if end <= start:
        raise TimestampError("The end time must be greater than the start time.")

    duration = end - start

    if duration > max_clip_duration:
        raise TimestampError(
            f"That timestamp clip is too long.\n\n"
            f"Maximum allowed timestamp duration: {max_clip_duration} seconds.\n"
            f"Requested duration: {duration} seconds."
        )

    return DownloadInfo(
        url=url,
        is_clip=True,
        start=start,
        end=end,
        duration=duration,
    )


def find_first_video_file(folder: Path) -> Path | None:
    video_extensions = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
    files = [
        path for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in video_extensions
    ]

    if not files:
        return None

    return max(files, key=lambda p: p.stat().st_size)


async def run_ytdlp_download(
    download: DownloadInfo,
    output_dir: Path,
    quality: str,
    accurate: bool,
    force_mp4: bool,
    fragments: int,
) -> Path:
    if download.is_clip:
        clean_url = remove_timestamp_params(download.url)
        section = f"*{format_seconds(download.start)}-{format_seconds(download.end)}"
        output_template = str(
            output_dir / "%(title).80s_%(section_start)s-%(section_end)s.%(ext)s"
        )

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
        cmd[1:1] = ["--merge-output-format", "mp4"]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    output_chunks: list[str] = []

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
        output_text = "\n".join(output_chunks[-20:])
        raise RuntimeError(
            f"yt-dlp failed with exit code {return_code}.\n\nLast output:\n{output_text}"
        )

    video_file = find_first_video_file(output_dir)
    if video_file is None:
        raise RuntimeError("Download finished, but I could not find the video file.")

    return video_file


async def send_typing_loop(update: Update, stop_event: asyncio.Event) -> None:
    if not update.effective_chat:
        return

    while not stop_event.is_set():
        try:
            await update.effective_chat.send_action(ChatAction.UPLOAD_VIDEO)
        except Exception:
            pass
        await asyncio.sleep(4)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "Hi. Send me any YouTube link and I’ll download it.\n\n"
        "Normal YouTube link = full video.\n"
        "Timestamped YouTube link = only that timestamp section.\n\n"
        "Examples:\n"
        "https://youtu.be/VIDEO_ID\n"
        "https://www.youtube.com/watch?v=VIDEO_ID\n"
        "https://m.youtube.com/watch?v=VIDEO_ID\n"
        "https://youtube.com/shorts/VIDEO_ID\n"
        "https://youtu.be/VIDEO_ID?t=1m30s\n"
        "https://www.youtube.com/watch?v=VIDEO_ID&start=90&end=120\n\n"
        "Commands:\n"
        "/duration 20 - set default clip length when the link only has a start time\n"
        "/quality 360 - use 360p for smaller files\n"
        "/quality 480 - use 480p\n"
        "/quality 720 - use 720p\n"
        "/accurate on - more exact timestamp cuts, but slower\n"
        "/settings - show current settings"
    )

    await update.message.reply_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    default_duration = get_setting(
        user_id,
        "default_duration",
        int(os.getenv("DEFAULT_DURATION", "30")),
    )
    max_duration = int(os.getenv("MAX_DURATION", "120"))
    quality_label = get_setting(
        user_id,
        "quality_label",
        os.getenv("DEFAULT_QUALITY_LABEL", "480"),
    )
    accurate = get_setting(
        user_id,
        "accurate",
        os.getenv("ACCURATE_MODE", "false").lower() == "true",
    )
    force_mp4 = os.getenv("FORCE_MP4", "true").lower() == "true"
    allow_full_video = os.getenv("ALLOW_FULL_VIDEO_DOWNLOADS", "true").lower() == "true"

    msg = (
        "Current settings:\n\n"
        f"Default timestamp duration: {default_duration} seconds\n"
        f"Maximum timestamp clip duration: {max_duration} seconds\n"
        f"Full video downloads: {'on' if allow_full_video else 'off'}\n"
        f"Quality: {quality_label}p\n"
        f"Accurate mode: {'on' if accurate else 'off'}\n"
        f"Force MP4: {'on' if force_mp4 else 'off'}"
    )

    await update.message.reply_text(msg)


async def duration_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    max_duration = int(os.getenv("MAX_DURATION", "120"))

    if not context.args:
        await update.message.reply_text("Usage: /duration 20")
        return

    try:
        seconds = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please enter a number. Example: /duration 20")
        return

    if seconds < 1:
        await update.message.reply_text("Duration must be at least 1 second.")
        return

    if seconds > max_duration:
        await update.message.reply_text(f"Duration cannot be more than {max_duration} seconds.")
        return

    set_setting(user_id, "default_duration", seconds)
    await update.message.reply_text(f"Default timestamp duration set to {seconds} seconds.")


async def quality_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("Usage: /quality 480\nOptions: 360, 480, 720, 1080, best")
        return

    label = context.args[0].strip().lower().replace("p", "")

    try:
        selector = quality_selector(label)
    except ValueError:
        await update.message.reply_text("Unsupported quality. Use: 360, 480, 720, 1080, best")
        return

    set_setting(user_id, "quality_label", label)
    set_setting(user_id, "quality", selector)

    if label == "best":
        await update.message.reply_text("Quality set to best.")
    else:
        await update.message.reply_text(f"Quality set to {label}p.")


async def accurate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await update.message.reply_text("Usage: /accurate on\nor:\n/accurate off")
        return

    enabled = context.args[0].lower() == "on"
    set_setting(user_id, "accurate", enabled)

    if enabled:
        await update.message.reply_text("Accurate mode is on. Timestamp cuts may be more exact, but slower.")
    else:
        await update.message.reply_text("Accurate mode is off. Downloads should be faster.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()
    url = extract_youtube_url(text)

    if not url:
        await update.message.reply_text("Please send a valid YouTube URL.")
        return

    default_duration = int(
        get_setting(
            user_id,
            "default_duration",
            int(os.getenv("DEFAULT_DURATION", "30")),
        )
    )
    max_duration = int(os.getenv("MAX_DURATION", "120"))
    allow_full_video = os.getenv("ALLOW_FULL_VIDEO_DOWNLOADS", "true").lower() == "true"
    quality = str(
        get_setting(
            user_id,
            "quality",
            quality_selector(os.getenv("DEFAULT_QUALITY_LABEL", "480")),
        )
    )
    accurate = bool(
        get_setting(
            user_id,
            "accurate",
            os.getenv("ACCURATE_MODE", "false").lower() == "true",
        )
    )
    force_mp4 = os.getenv("FORCE_MP4", "true").lower() == "true"
    fragments = int(os.getenv("CONCURRENT_FRAGMENTS", "8"))

    try:
        download = build_download_info(
            url=url,
            default_duration=default_duration,
            max_clip_duration=max_duration,
            allow_full_video=allow_full_video,
        )
    except TimestampError as exc:
        await update.message.reply_text(str(exc))
        return

    if download.is_clip:
        await update.message.reply_text(
            "Got it. Downloading this timestamp section:\n\n"
            f"Start: {format_seconds(download.start)}\n"
            f"End: {format_seconds(download.end)}\n"
            f"Duration: {download.duration} seconds\n\n"
            f"Mode: {'accurate but slower' if accurate else 'fast'}"
        )
    else:
        await update.message.reply_text(
            "Got it! Your video is on the way.\n\n"
            "If Telegram rejects the file because it is too large, use /quality 360."
        )

    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(send_typing_loop(update, stop_event))

    try:
        with tempfile.TemporaryDirectory(prefix=f"ytbot_{uuid.uuid4().hex}_") as temp_dir:
            temp_path = Path(temp_dir)

            video_file = await run_ytdlp_download(
                download=download,
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
                    f"The video was downloaded, but it is too large to send.\n\n"
                    f"File size: {file_size_mb:.1f} MB\n"
                    f"Bot limit: {max_file_size_mb} MB\n\n"
                    "Try /quality 360 or send a shorter timestamped link."
                )
                return

            if download.is_clip:
                caption = f"Here is your clip: {format_seconds(download.start)} to {format_seconds(download.end)}"
            else:
                caption = "Here is your video."

            with video_file.open("rb") as file_handle:
                await update.message.reply_video(
                    video=file_handle,
                    caption=caption,
                    supports_streaming=True,
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=60,
                    pool_timeout=60,
                )

    except Exception as exc:
        await update.message.reply_text(
            "Sorry, something went wrong while downloading or sending the video.\n\n"
            f"Error:\n{exc}"
        )

    finally:
        stop_event.set()
        typing_task.cancel()


def validate_environment() -> None:
    load_dotenv()

    token = os.getenv("YT_BOT_TOKEN")
    if not token or token == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit(
            "YT_BOT_TOKEN is missing.\n"
            "Create a .env file and add:\n"
            "YT_BOT_TOKEN=your_telegram_bot_token"
        )

    missing = []
    if not check_dependency("yt-dlp"):
        missing.append("yt-dlp")
    if not check_dependency("ffmpeg"):
        missing.append("ffmpeg")

    if missing:
        raise SystemExit(
            "Missing required programs: "
            + ", ".join(missing)
            + "\nInstall them first."
        )


def main() -> None:
    validate_environment()

    # Python 3.14 compatibility fix.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    token = os.getenv("YT_BOT_TOKEN")
    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("duration", duration_command))
    application.add_handler(CommandHandler("quality", quality_command))
    application.add_handler(CommandHandler("accurate", accurate_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running. Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
