#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import base64
from pathlib import Path
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode, urlunparse

from dotenv import load_dotenv
from fastapi import FastAPI, Request
import uvicorn

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- APP STATE ---------------- #

app_web = FastAPI()
application: Application | None = None


# ---------------- COOKIE HANDLER ---------------- #

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


# ---------------- YOUTUBE ---------------- #

YOUTUBE_URL_RE = re.compile(
    r"(https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/[^\s]+)",
    re.IGNORECASE,
)


def extract_url(text: str) -> str | None:
    m = YOUTUBE_URL_RE.search(text)
    return m.group(1) if m else None


def remove_params(url: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k not in {"t", "start", "end"}]
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), ""))


# ---------------- YT-DLP ---------------- #

async def run_download(url: str, output: Path) -> Path | None:
    cookies = write_cookies_file()

    cmd = ["yt-dlp"]

    if cookies:
        cmd += ["--cookies", cookies]

    cmd += [
        "--no-playlist",
        "--restrict-filenames",
        "-f",
        "best",
        "-o",
        str(output / "%(title)s.%(ext)s"),
        remove_params(url),
    ]

    process = await asyncio.create_subprocess_exec(*cmd)
    await process.wait()

    files = list(output.glob("*"))
    return max(files, key=lambda f: f.stat().st_size) if files else None


# ---------------- TELEGRAM ---------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is running via webhook 🚀")


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = extract_url(update.message.text or "")

    if not url:
        await update.message.reply_text("Send a valid YouTube link.")
        return

    await update.message.reply_text("Downloading...")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            file = await run_download(url, Path(tmp))

            if not file:
                await update.message.reply_text("Download failed.")
                return

            with file.open("rb") as f:
                await update.message.reply_video(video=f)

    except Exception as e:
        await update.message.reply_text(f"Error:\n{e}")


# ---------------- WEBHOOK ---------------- #

@app_web.post("/webhook")
async def webhook(req: Request):
    global application

    if not application:
        return {"error": "not initialized"}

    data = await req.json()
    update = Update.de_json(data, application.bot)

    await application.process_update(update)

    return {"ok": True}


@app_web.get("/")
def health():
    return {"status": "alive"}


# ---------------- SETUP ---------------- #

async def setup_webhook():
    url = os.getenv("WEBHOOK_URL")

    await application.bot.set_webhook(
        url=f"{url}/webhook",
        drop_pending_updates=True,
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

    # handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    # IMPORTANT FIX (your error)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(application.initialize())
    loop.run_until_complete(application.start())
    loop.run_until_complete(setup_webhook())

    print("Bot fully initialized")

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app_web, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
