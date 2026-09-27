"""
tgstore.py
----------
Telegram-channel-as-database — VPS khatam ho jaye to bhi cache zinda rahe.

Kaise kaam karta hai:
  - Ek private Telegram channel banao (DB_CHANNEL_ID) aur HellAPI bot ko
    admin bana do (post messages permission)
  - Har fresh song fetch pe bot channel me ek message store karta hai:
      #hellapi_stream|<video_id>|<expires_ts>|<fmt>
      {json: direct_url + metadata}
  - Message_id local SQLite index me save hota hai (restart-proof)
  - Cache miss pe: SQLite index → channel se forward karke text padha → serve
  - Naya VPS = recover_from_channel script → poora cache wapas

Graceful: DB_CHANNEL_ID khali ho to sab no-op (kuch nahi tootega).
"""

import os
import json
import time
import logging
import asyncio
from typing import Optional

logger = logging.getLogger(__name__)

DB_CHANNEL_ID = os.getenv("DB_CHANNEL_ID", "").strip()
TAG = "#hellapi_stream"

_bot = None
_bot_lock = asyncio.Lock()


def is_enabled() -> bool:
    return bool(DB_CHANNEL_ID)


def _build_text(video_id: str, expires_ts: float, fmt: str, payload: dict) -> str:
    return (
        f"{TAG}|{video_id}|{int(expires_ts)}|{fmt}\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _parse_text(text: str) -> Optional[dict]:
    """Channel message text → {'info':..., 'result':...} ya None."""
    try:
        lines = (text or "").strip().split("\n", 1)
        if not lines[0].startswith(TAG):
            return None
        _, video_id, exp_str, fmt = lines[0].split("|", 3)
        expires_ts = float(exp_str)
        if expires_ts <= time.time() + 30:      # expired / about-to-expire
            return None
        payload = json.loads(lines[1])
        result = {
            "direct_url": payload.get("direct_url") or "",
            "format_id":  payload.get("format_id") or "",
            "ext":        payload.get("ext") or "",
            "resolution": payload.get("resolution") or "",
            "is_live":    bool(payload.get("is_live", False)),
            "title":      payload.get("title") or "",
        }
        info = {
            "id":               video_id,
            "title":            payload.get("title") or "",
            "uploader":         payload.get("uploader") or "",
            "duration_seconds": payload.get("duration_seconds") or 0,
            "thumbnail":        payload.get("thumbnail") or "",
            "upload_date":      payload.get("upload_date") or "",
            "view_count":       payload.get("view_count") or 0,
            "description":      payload.get("description") or "",
            "is_live":          bool(payload.get("is_live", False)),
            "tags":             [],
            "webpage_url":      f"https://www.youtube.com/watch?v={video_id}",
            "_from_tg_store":   True,
        }
        return {"info": info, "result": result}
    except Exception as e:
        logger.warning(f"tg parse fail: {e}")
        return None


async def _get_bot():
    """Lazy bot instance (python-telegram-bot v21 async)."""
    global _bot
    if _bot is not None:
        return _bot
    async with _bot_lock:
        if _bot is None:
            from telegram import Bot
            from dotenv import load_dotenv
            load_dotenv()
            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            if not token:
                raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
            _bot = Bot(token)
            await _bot.initialize()
    return _bot


async def tg_store_stream(url: str, fmt: str, info: dict,
                          result: dict, expires_at_ts: float):
    """
    Fresh fetch ke baad channel me store karo + msg_id index me save.
    Har failure silent-warn — storing kabhi extraction fail na kare.
    """
    if not is_enabled():
        return
    try:
        from database import tg_index_put
        video_id = info.get("id") or ""
        if not video_id:
            return
        bot = await _get_bot()
        text = _build_text(video_id, expires_at_ts, fmt, {
            "direct_url":  result.get("direct_url"),
            "format_id":   result.get("format_id"),
            "ext":         result.get("ext"),
            "resolution":  result.get("resolution"),
            "is_live":     result.get("is_live", False),
            "title":       info.get("title"),
            "uploader":    info.get("uploader"),
            "duration_seconds": info.get("duration_seconds"),
            "thumbnail":   info.get("thumbnail"),
            "upload_date": info.get("upload_date"),
            "view_count":  info.get("view_count"),
            "description": info.get("description"),
        })
        msg = await bot.send_message(DB_CHANNEL_ID, text)
        tg_index_put(video_id, msg.message_id, expires_at_ts, fmt)
        logger.debug(f"TG stored: {video_id} -> msg {msg.message_id}")
    except Exception as e:
        logger.warning(f"tg_store_stream failed: {e}")


async def tg_load_stream(url: str, fmt: str = "") -> Optional[dict]:
    """
    Local index me msg_id hai → channel se forward karke text padho → parse.
    (Bot API history read nahi kar sakta, par forward se content mil jata hai.)
    """
    if not is_enabled():
        return None
    try:
        from database import tg_index_get, _url_to_video_id
        video_id = _url_to_video_id(url)
        if not video_id or video_id.startswith("q_") or len(video_id) != 11:
            return None
        row = tg_index_get(video_id)
        if not row:
            return None
        msg_id, expires_at_ts = row
        if expires_at_ts <= time.time() + 30:
            return None
        bot = await _get_bot()
        # same channel me forward → returned Message me text hota hai
        fwd = await bot.forward_message(
            chat_id=DB_CHANNEL_ID,
            from_chat_id=DB_CHANNEL_ID,
            message_id=msg_id,
        )
        data = _parse_text(fwd.text or "")
        try:
            await fwd.delete()               # temp forward hatao, asli rehne do
        except Exception:
            pass
        if data:
            logger.debug(f"TG HIT: {video_id}")
        return data
    except Exception as e:
        logger.warning(f"tg_load_stream failed: {e}")
        return None


# ---------------------------------------------------------------------------
# MP3 FILE STORE — actual file channel me, kabhi expire nahi hoti
# ---------------------------------------------------------------------------

AUDIO_EXT_SKIP = {"webm", "mkv"}          # (legacy name — ab niche NEEDS_CONVERT use hota)
# 🚀 SPEED: sirf ye formats MP3 me convert hote — m4a/mp3/mp4/ogg AS-IS
# upload hote hain (ffmpeg re-encode ka 5-30s CPU + quality loss khatam)
NEEDS_CONVERT = {"webm", "mkv"}
# 🚫 Bot API get_file sirf ≤20MB file download kar sakta tha — 45MB tak
# store karna bekaar tha (file /file/ se KABHI serve nahi ho sakti thi =
# 'DB channel me download hua par stream nahi hota' bug ka ek hissa).
MAX_FILE_MB    = float(os.getenv("TG_STORE_MAX_MB", "19"))


def _ffmpeg() -> Optional[str]:
    import shutil
    return shutil.which("ffmpeg")


def _convert_to_mp3(src_path: str, out_path: str) -> bool:
    """WebM/Opus/M4A -> MP3 128k (Bot API friendly + sab players chalate)."""
    ff = _ffmpeg()
    if not ff:
        logger.warning("ffmpeg not found — MP3 convert skip, original bhej rahe")
        return False
    import subprocess
    try:
        subprocess.run(
            [ff, "-y", "-loglevel", "error", "-i", src_path,
             "-vn", "-acodec", "libmp3lame", "-b:a", "128k",
             "-threads", "1", out_path],
            timeout=300, check=True,
        )
        return True
    except Exception as e:
        logger.warning(f"ffmpeg convert fail: {e}")
        return False


def _download_direct(direct_url: str, dest: str, max_mb: int = 100,
                     extra_headers: Optional[dict] = None) -> bool:
    """googlevideo/HTTP URL se file download (Range headers ke saath)."""
    import os as _os
    import urllib.request
    try:
        _h = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Range": "bytes=0-",
        }
        if extra_headers:
            _h.update(extra_headers)
        req = urllib.request.Request(direct_url, headers=_h)
        limit = max_mb * 1024 * 1024
        done  = 0
        # DOWNLOAD_PROXY set ho to downloads proxy IP se jayen
        # (googlevideo URLs extraction-IP pe signed hote hain)
        _purl = (_os.getenv("DOWNLOAD_PROXY", "") or "").strip()
        if _purl:
            _opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"http": _purl, "https": _purl}))
        else:
            _opener = urllib.request.build_opener()
        with _opener.open(req, timeout=120) as resp, \
                open(dest, "wb") as f:
            try:
                clen = resp.headers.get("Content-Length")
            except Exception:
                clen = None
            while True:
                chunk = resp.read(1024 * 512)
                if not chunk:
                    break
                done += len(chunk)
                if done > limit:
                    raise IOError(f"file > {max_mb}MB — skip")
                f.write(chunk)
            # 📏 TRUNCATION CHECK — stream beech me kati to file ADHURI hai.
            # Adhuri file archive karna = bot me 'aadha gaana play then
            # VC leave' bug. Reject karo, agli baar fresh download hoga.
            if clen:
                try:
                    if done < int(clen) * 0.98:
                        raise IOError(
                            f"truncated download: {done}/{clen} bytes")
                except ValueError:
                    pass
        return done > 0
    except Exception as e:
        logger.warning(f"direct download fail: {e}")
        return False


async def tg_store_file(video_id: str, title: str, direct_url: str,
                        ext: str = "", local_file: str = "",
                        url_headers: Optional[dict] = None,
                        kind: str = "audio") -> bool:
    """
    Song/video download -> convert -> channel me upload -> index save.
    kind='audio' → MP3 convert + send_audio
    kind='video' → send_video (MP4, no convert)
    Fire-and-forget — kabhi block na kare.
    """
    if not is_enabled() or not video_id or len(video_id) != 11:
        return False
    # Pehle se file hai? Skip.
    try:
        from database import tg_file_get
        if tg_file_get(video_id, kind=kind):
            return True
    except Exception:
        pass
    import tempfile, os as _os, shutil as _shutil
    tmpdir = tempfile.mkdtemp(prefix="hellapi_tg_")
    src    = _os.path.join(tmpdir, f"src.{ext or 'bin'}")
    out    = _os.path.join(tmpdir, "song.mp3" if kind == "audio" else "video.mp4")
    try:
        if local_file and _os.path.exists(local_file):
            _shutil.copy(local_file, src)
        elif not _download_direct(direct_url, src, extra_headers=url_headers):
            return False
        _src_ext = (ext or "").lower().lstrip(".")
        if kind == "audio" and _src_ext in NEEDS_CONVERT:
            # webm/mkv → MP3 (kuch players inhe theek se nahi chalate)
            ok_conv = _convert_to_mp3(src, out)
            upload_path = out if ok_conv else src
        else:
            # m4a/mp3/mp4/ogg AS-IS — bot ka ffmpeg player sab chalata hai
            upload_path = src
        if _os.path.getsize(upload_path) > MAX_FILE_MB * 1024 * 1024:
            logger.warning(f"file > {MAX_FILE_MB}MB — TG upload skip: {video_id}")
            return False
        # fresh Bot per call (worker threads/loops me safe — shared bot
        # cross-loop httpx issue karta hai)
        from telegram import Bot as TGBot
        bot = TGBot(_os.getenv("TELEGRAM_BOT_TOKEN", ""))
        await bot.initialize()
        with open(upload_path, "rb") as f:
            if kind == "video":
                msg = await bot.send_video(
                    DB_CHANNEL_ID, f,
                    caption=f"🎬 {title[:60]}\n#{video_id}",
                    supports_streaming=True,
                    read_timeout=180, write_timeout=180,
                )
                file_id = msg.video.file_id if msg.video else ""
            else:
                msg = await bot.send_audio(
                    DB_CHANNEL_ID, f,
                    caption=f"🎵 {title[:60]}\n#{video_id}",
                    title=title[:60], performer="",
                    read_timeout=180, write_timeout=180,
                )
                file_id = msg.audio.file_id if msg.audio else ""
        try:
            await bot.shutdown()
        except Exception:
            pass
        size_mb = round(_os.path.getsize(upload_path) / (1024 * 1024), 2)
        from database import tg_file_put
        tg_file_put(video_id, msg.message_id, file_id, title, size_mb, kind=kind)
        logger.info(f"TG FILE stored: {video_id} [{kind}] -> msg {msg.message_id} ({size_mb}MB)")
        return True
    except Exception as e:
        logger.warning(f"tg_store_file failed: {e}")
        return False
    finally:
        import shutil as _sh
        _sh.rmtree(tmpdir, ignore_errors=True)


async def tg_get_file_id(video_id: str) -> Optional[str]:
    """Stored file ka Telegram file_id — bot pe direct forward bhi kar sakte."""
    try:
        from database import tg_file_get
        row = tg_file_get(video_id)
        return row[0] if row and row[0] else None
    except Exception:
        return None


def tg_file_status() -> dict:
    if not is_enabled():
        return {"enabled": False}
    try:
        from database import tg_file_stats
        return {"enabled": True, **tg_file_stats()}
    except Exception as e:
        return {"enabled": True, "error": str(e)[:80]}


def tg_status() -> dict:
    if not is_enabled():
        return {"enabled": False}
    try:
        from database import tg_stats
        return {"enabled": True, "channel": DB_CHANNEL_ID, **tg_stats()}
    except Exception as e:
        return {"enabled": True, "error": str(e)[:80]}


async def recover_from_channel(max_items: int = 500) -> dict:
    """
    Naya VPS pe call karo: index me pade LIVE video_ids ke messages channel
    se padh kar local DB stream-cache me wapas bhar deta hai.
    RAM cache thanda rehta hai — pehli request pe warm ho jayega.
    """
    if not is_enabled():
        return {"recovered": 0, "skipped": 0, "enabled": False}
    from database import tg_index_all, cache_stream
    recovered = skipped = 0
    entries = tg_index_all()[:max_items]
    for video_id, msg_id, expires_at in entries:
        try:
            data = await tg_load_stream(
                f"https://www.youtube.com/watch?v={video_id}")
            if data:
                cache_stream(
                    f"https://www.youtube.com/watch?v={video_id}",
                    "best", data["info"], data["result"], expires_at)
                recovered += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
    return {"recovered": recovered, "skipped": skipped,
            "scanned": len(entries), "enabled": True}
