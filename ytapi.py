"""
ytapi.py
--------
HellAPI — @youtubeapiiiibot fallback client (FINAL tier).

@youtubeapiiiibot hamara APNA key-bot hai (HellAPI key deta hai).
Ye module usi bot se stream URLs nikalta hai jab direct YouTube +
Invidious/Piped sab fail hon.

Flow:
  1. Userbot se bot ko video link bhejo
  2. Bot ka reply parse karo (stream link/download link)
  3. URL ya file wapas karo — HellAPI use karke stream karega

Session: SHRUTI... nahi — STRING_SESSION (Keyara wala) use hota hai,
  file-lock issue se bachne ke liye in_memory session clone hota hai.

Env:
  YTAPI_BOT_USERNAME  — default youtubeapiiiibot
  YTAPI_USER_SESSION  — pyrogram session string (userbot)
  YTAPI_API_ID / YTAPI_API_HASH
  YTAPI_ENABLED       — default 1
  YTAPI_TIMEOUT       — default 90s
"""

import os
import re
import time
import logging
import asyncio
import threading
from typing import Optional

logger = logging.getLogger(__name__)

BOT_USERNAME = os.getenv("YTAPI_BOT_USERNAME", "youtubeapiiiibot").strip("@")
YTAPI_ENABLED = os.getenv("YTAPI_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
YTAPI_TIMEOUT = int(os.getenv("YTAPI_TIMEOUT", "90"))

# userbot creds — HellAPI .env se, fallback Keyara .env pe
API_ID = int(os.getenv("YTAPI_API_ID", "0") or 0)
API_HASH = os.getenv("YTAPI_API_HASH", "")
SESSION = os.getenv("YTAPI_USER_SESSION", "")

if not SESSION and os.path.exists("/root/nakshu/KeyaraMusic/.env"):
    try:
        for line in open("/root/nakshu/KeyaraMusic/.env", encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line.startswith("STRING_SESSION="):
                SESSION = line.split("=", 1)[1].strip()
            elif line.startswith("API_ID=") and not API_ID:
                API_ID = int(line.split("=", 1)[1].strip() or 0)
            elif line.startswith("API_HASH=") and not API_HASH:
                API_HASH = line.split("=", 1)[1].strip()
    except Exception:
        pass

_enabled_cache = {"ts": 0.0, "val": None}
_lock = threading.Lock()


def enabled() -> bool:
    return YTAPI_ENABLED and bool(SESSION) and bool(API_ID) and bool(API_HASH)


_YT_LINK = re.compile(r"https?://[^\s\`\|\)\]]+", re.I)


def _parse_reply(text: str) -> Optional[dict]:
    """Bot reply se stream URL nikaalo."""
    if not text:
        return None
    t = text.strip()
    low = t.lower()
    # error indicators
    if any(x in low for x in ("invalid", "expired", "not found", "error",
                              "failed", "try again", "🚫", "❌")):
        # lekin agar link bhi hai to error nahi maanenge
        if not _YT_LINK.search(t):
            return None
    m = _YT_LINK.search(t)
    if not m:
        return None
    url = m.group(0)
    ext = "mp4"
    if ".mp3" in url or "audio" in low:
        ext = "mp3"
    elif ".webm" in url:
        ext = "webm"
    elif ".m4a" in url:
        ext = "m4a"
    return {
        "title": "",
        "format_id": "ytapi",
        "ext": ext,
        "resolution": "audio",
        "direct_url": url,
        "is_live": False,
        "duration": None,
        "via": f"ytapi:{BOT_USERNAME}",
    }


def _run_probe(vid: str, mode: str) -> Optional[dict]:
    """Userbot se bot ko request bhejo, reply parse karo. Sync."""
    from pyrogram import Client, filters
    from pyrogram.handlers import MessageHandler

    async def _inner():
        asyncio.set_event_loop(asyncio.get_event_loop())
        app = Client("ytapi_client", api_id=API_ID, api_hash=API_HASH,
                     session_string=SESSION, in_memory=True,
                     workers=4)
        await app.start()
        got = asyncio.Event()
        texts = []
        files = []

        async def handler(client, message):
            if message.text or message.caption:
                texts.append(message.text or message.caption or "")
            if message.audio or message.document or message.voice:
                files.append(message)
            got.set()

        app.add_handler(MessageHandler(
            handler, filters.chat(BOT_USERNAME) & filters.incoming), group=-5)
        try:
            await app.send_message(BOT_USERNAME, f"https://www.youtube.com/watch?v={vid}")
            try:
                await asyncio.wait_for(got.wait(), timeout=YTAPI_TIMEOUT)
            except asyncio.TimeoutError:
                return None
            for t in texts:
                res = _parse_reply(t)
                if res:
                    return res
            if files:
                m = files[0]
                att = m.audio or m.document or m.voice
                file_id = getattr(att, "file_id", "")
                if file_id:
                    return {
                        "title": (getattr(att, "file_name", "") or vid)[:80],
                        "format_id": "ytapi-file",
                        "ext": "mp3",
                        "resolution": "audio",
                        "direct_url": f"ytapi-file://{file_id}",
                        "is_live": False,
                        "duration": getattr(att, "duration", None),
                        "via": f"ytapi:{BOT_USERNAME}",
                        "tg_file_id": file_id,
                    }
            return None
        finally:
            try:
                await app.stop()
            except Exception:
                pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_inner())
    finally:
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(None)


def resolve_sync(vid: str) -> Optional[dict]:
    """HellAPI se call karo — video_id se stream URL nikaalo."""
    if not enabled():
        return None
    vid = (vid or "").strip()
    if len(vid) != 11:
        vid = vid.split("v=")[-1].split("&")[0][:11]
    if len(vid) != 11:
        return None
    try:
        return _run_probe(vid, "audio")
    except Exception as e:
        logger.warning(f"ytapi resolve fail {vid}: {type(e).__name__}: {str(e)[:80]}")
        return None


async def resolve(vid: str) -> Optional[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, resolve_sync, vid)


def get_status() -> dict:
    return {
        "enabled": enabled(),
        "bot": BOT_USERNAME,
        "session_ok": bool(SESSION),
        "api_id_ok": bool(API_ID),
        "api_hash_ok": bool(API_HASH),
        "timeout_s": YTAPI_TIMEOUT,
    }
