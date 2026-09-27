"""
youtube.py  →  AnonXMusic/platforms/Youtube.py
-----------
HellAPI — Drop-in replacement for the yash (AnonXMusic) music bot.

FIXES v2 (VC-stream + half-song + thumbnail):
  • Download request me x-api-key header jata hai → /file/ + /relay/ ka
    401 khatam ("DB channel me download ho gaya par stream nahi hota")
  • Download pehle `.part` temp me → Content-Length verify → atomic
    rename. Adhuri file ab cache me SAVE HI NAHI hogi — pehle direct
    final path pe likhte the, connection kate to aadha gaana play hota
    tha aur bot VC chhod deta tha.
  • Cached-file sanity check — 100KB se chhoti stale file kabhi serve nahi
  • Thumbnail khali kabhi nahi — i.ytimg.com fallback (photo-send fail fix)
  • cookie_txt_file() shim — core/call.py ka import na toote

Setup:
  1. Is file ko AnonXMusic/platforms/Youtube.py se replace karo
  2. .env me:
       HELLAPI_URL=https://your-hellapi-server
       HELLAPI_KEY=HellAPIxxxxxxxxxxxxxxxx
  3. Bot restart

Compatible with: AnonXMusic, YukkiMusic style bots (YouTubeAPI class).
"""

import asyncio
import logging
import os
import re
import time
from urllib.parse import parse_qs, urlparse
from typing import Union

import aiohttp

# pyrogram OPTIONAL hai — sirf Message entity parsing ke liye.
try:
    from pyrogram.enums import MessageEntityType
    from pyrogram.types import Message
    _HAS_PYROGRAM = True
except Exception:                     # pragma: no cover
    Message = object                  # type: ignore
    MessageEntityType = None          # type: ignore
    _HAS_PYROGRAM = False

# ── Config ───────────────────────────────────────────────────────────────────
API_URL = os.environ.get("HELLAPI_URL", "").rstrip("/")
API_KEY = os.environ.get("HELLAPI_KEY", "")

if not API_URL:
    raise EnvironmentError(
        "[HellAPI] HELLAPI_URL not set.\n"
        "Add to .env:  HELLAPI_URL=http://your-server:8000"
    )
if not API_KEY:
    raise EnvironmentError(
        "[HellAPI] HELLAPI_KEY not set.\n"
        "Add to .env:  HELLAPI_KEY=HellAPIxxxxxxxxxxxxxxxx\n"
        "Get key: HellAPI Telegram Bot → /start"
    )

logger = logging.getLogger("HellAPI.youtube")

_HEADERS  = {"x-api-key": API_KEY, "Connection": "keep-alive"}
_TIMEOUT  = aiohttp.ClientTimeout(total=60, connect=10)
_VTIMEOUT = aiohttp.ClientTimeout(total=180, connect=10)
# ⬇ FILE download ke liye NO total-timeout — bada file slow link pe 120s me
# kata tha. sock_read=45 → 45s tak EK BHI byte nahi aaya tabhi abort.
# 🐕 STALL WATCHDOG: trickle-detection bhi — 30s me <64KB (≈2KB/s) aa raha
# ho to abort (290s wala "4.5MB in 5 min" stall yahin tha). Retry fast hoga.
_DTIMEOUT = aiohttp.ClientTimeout(total=900, connect=15, sock_read=45)

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")
MIN_VALID_BYTES = int(os.environ.get("MIN_VALID_BYTES", "100000"))
# ⚡ URL-STREAM MODE (default on): HellAPI ke /relay/ + /file/ URLs ko bot
# download kiye BINA seedha pytgcalls me stream karta hai — server khud
# HTTP range-requests se pipe karta hai. 15-25MB download ka wait (10-15s)
# khatam → "Stream Ready" 19s se ~3-6s. FILE_SERVE_MODE=proxy wali API pe
# bhi kaam karta hai; band karne ke liye URL_STREAM=0.
URL_STREAM = os.environ.get("URL_STREAM", "1").strip().lower() not in {
    "0", "false", "no", "off"
}


def _is_streamable_url(u: str) -> bool:
    """HellAPI ke apne URLs (/relay/, /file/, /live/) — HTTP range support ke saath,
    bot ke x-api-key se authorized — inhe download kiye bina stream kiya ja sakta hai."""
    if not u:
        return False
    return ("/relay/" in u or "/file/" in u or "/live/" in u) and u.startswith("http")

# Connection pool — reuse connections for speed
_connector = None

def _get_connector():
    global _connector
    if _connector is None or _connector.closed:
        _connector = aiohttp.TCPConnector(
            limit=20,
            ttl_dns_cache=300,
            use_dns_cache=True,
        )
    return _connector


def cookie_txt_file():
    """
    core/call.py compatibility shim — purana bot isse import karta hai.
    HellAPI flow me yt-dlp/cookies ki zaroorat nahi.
    """
    return None


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _normalise_video_url(value: str, videoid: Union[bool, str] = False) -> str:
    """Accept a YouTube URL or exact video ID; never treat text as a search."""
    raw = str(value or "").strip()
    if videoid:
        raw = raw.split("&", 1)[0].split("?", 1)[0]
        if not _VIDEO_ID_RE.fullmatch(raw):
            raise ValueError("Expected an 11-character YouTube video ID.")
        return f"https://www.youtube.com/watch?v={raw}"

    if _VIDEO_ID_RE.fullmatch(raw):
        return f"https://www.youtube.com/watch?v={raw}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host.endswith("youtube.com"):
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith(("/shorts/", "/embed/")):
            candidate = parsed.path.split("/")[2]
        else:
            candidate = ""
    else:
        candidate = ""
    if not _VIDEO_ID_RE.fullmatch(candidate):
        raise ValueError(
            "Only a YouTube video URL or 11-character video ID is supported; "
            "song-name search is disabled."
        )
    return f"https://www.youtube.com/watch?v={candidate}"


def _video_id(value: str, videoid: Union[bool, str] = False) -> str:
    return parse_qs(urlparse(_normalise_video_url(value, videoid)).query)["v"][0]


# ── Standalone functions (direct imports) ────────────────────────────────────

async def download_song(link: str) -> str:
    """Download audio → file path. Used by bots importing directly."""
    try:
        video_id = _video_id(link)
    except ValueError:
        return None
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")
    if os.path.exists(file_path) and os.path.getsize(file_path) > MIN_VALID_BYTES:
        return file_path
    try:
        data = await _api_get("/api/stream", {
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "type": "audio"
        })
        stream_url = data.get("stream_url") or data.get("direct_url")
        if not stream_url:
            return None
        await _download_file(stream_url, file_path)
        return file_path if os.path.exists(file_path) \
            and os.path.getsize(file_path) > MIN_VALID_BYTES else None
    except Exception:
        _cleanup(file_path)
        return None


async def download_video(link: str) -> str:
    """Download video → file path. Used by bots importing directly."""
    try:
        video_id = _video_id(link)
    except ValueError:
        return None
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
    if os.path.exists(file_path) and os.path.getsize(file_path) > MIN_VALID_BYTES:
        return file_path
    try:
        data = await _api_get("/api/stream", {
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "type": "video"
        }, timeout=_VTIMEOUT)
        stream_url = data.get("stream_url") or data.get("direct_url")
        if not stream_url:
            return None
        await _download_file(stream_url, file_path)
        return file_path if os.path.exists(file_path) \
            and os.path.getsize(file_path) > MIN_VALID_BYTES else None
    except Exception:
        _cleanup(file_path)
        return None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _time_to_seconds(time_str: str) -> int:
    try:
        parts = [int(x) for x in str(time_str).split(":")]
        return sum(x * (60 ** i) for i, x in enumerate(reversed(parts)))
    except Exception:
        return 0


def _seconds_to_min(seconds) -> str:
    try:
        m, s = divmod(int(seconds or 0), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return "0:00"


# Yukki/AnonX-style bots ye bina-underscore naam import karte hain
seconds_to_min = _seconds_to_min


async def _ydl_search(query: str, limit: int = 1) -> list:
    """
    Yukki/AnonX-style platform files (Apple.py, Resso.py, etc.) isse import
    karti hain — song-name se YouTube pe search (HellAPI /search).
    """
    try:
        data = await _api_get("/search", {"q": query, "limit": max(1, min(int(limit), 10))})
    except Exception:
        return []

    results = []
    for r in data.get("results", []):
        vid = r.get("id") or ""
        if not vid:
            continue
        results.append({
            "id":           vid,
            "vidid":        vid,
            "title":        r.get("title") or "Unknown",
            "link":         r.get("url") or f"https://www.youtube.com/watch?v={vid}",
            "url":          r.get("url") or f"https://www.youtube.com/watch?v={vid}",
            "duration":     r.get("duration"),
            "duration_min": _seconds_to_min(r.get("duration")),
            "thumb":        r.get("thumbnail") or "",
            "thumbnail":    r.get("thumbnail") or "",
        })
    return results


_MIX_SEED_RE = re.compile(
    r"[?&]list=RD(?:AMVM|MM|GM|CLAK5uy_[A-Za-z0-9_-]{20,})?([A-Za-z0-9_-]{11})")


async def _resolve_query_to_video(link) -> "Optional[dict]":
    """Song-NAME / Mix-URL / playlist-URL → ek video dict (id,title,link,...).
    Bot ke 'FAILED TO PROCESS QUERY' ka fix: pehle sirf video-URL chalega tha,
    song-name aur Mix links raise karte the."""
    raw = str(link or "").strip()
    # 🎯 YouTube Mix/Radio: list-id me seed video-id encoded hota hai
    m = _MIX_SEED_RE.search(raw)
    if m and "v=" not in raw:
        vid = m.group(1)
        return {"id": vid, "vidid": vid,
                "link": f"https://www.youtube.com/watch?v={vid}"}
    # 📋 Real playlist URL → API khud first-video resolve karta hai
    if "list=" in raw and "v=" not in raw:
        try:
            data = await _api_get("/api/stream", {"url": raw}, retries=0)
            sv = data.get("stream_url") or data.get("direct_url") or ""
            m2 = re.search(r"/(?:relay|file)/([A-Za-z0-9_-]{11})", sv)
            if m2:
                vid = m2.group(1)
                return {"id": vid, "vidid": vid,
                        "title": data.get("title") or "Unknown",
                        "duration": data.get("duration"),
                        "thumbnail": data.get("thumbnail") or "",
                        "link": f"https://www.youtube.com/watch?v={vid}"}
        except Exception:
            return None
        return None
    # 🎵 Song-name → HellAPI /search
    results = await _ydl_search(raw, limit=1)
    if not results:
        raise LookupError(f"No YouTube results for: {raw[:60]}")
    return results[0]


def _cleanup(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


async def _api_get(path: str, params: dict, timeout=None, retries: int = 2) -> dict:
    """GET request to HellAPI with auto-retry on failure."""
    url = f"{API_URL}{path}"
    last_exc = None
    for attempt in range(retries + 1):
        try:
            async with aiohttp.ClientSession(
                connector=_get_connector(),
                connector_owner=False,
            ) as session:
                async with session.get(
                    url,
                    params=params,
                    headers=_HEADERS,
                    timeout=timeout or _TIMEOUT,
                ) as resp:
                    if resp.status == 401:
                        raise PermissionError(
                            "HellAPI key invalid/expired. "
                            "Get new key from HellAPI bot → /start"
                        )
                    if resp.status == 429:
                        data = await resp.json()
                        msg  = (data.get("detail") or {}).get("message", "Rate limit exceeded.")
                        raise ConnectionAbortedError(f"Rate limit: {msg}")
                    if resp.status == 503:
                        raise ConnectionError("YouTube IP block. Retry in a few minutes.")
                    if resp.status != 200:
                        text = await resp.text()
                        raise ConnectionError(f"HellAPI {resp.status}: {text[:100]}")
                    return await resp.json()
        except (PermissionError, ConnectionAbortedError):
            raise   # Don't retry auth/rate limit errors
        except Exception as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
    raise last_exc or ConnectionError("HellAPI request failed")


async def _url_alive(url: str) -> bool:
    """🛡 1-byte pre-flight: URL streamable hai? (200/206 = zinda, Range probe).
    Dead/403/401/410/502 pe False — caller download-fallback use karega.
    2.5s budget — slow URL ka wait nahi."""
    try:
        async with aiohttp.ClientSession(
            connector=_get_connector(),
            connector_owner=False,
        ) as session:
            async with session.get(
                url,
                headers={**_HEADERS, "Range": "bytes=0-0"},
                timeout=aiohttp.ClientTimeout(total=2.5, connect=1.5),
            ) as resp:
                # 200/206 = zinda. 416 bhi alive signal hai (bad range, magar
                # server zinda hai) — stream chal jayega.
                return resp.status in (200, 206, 416)
    except Exception:
        return False


async def _download_file(url: str, path: str, timeout=None):
    """
    Stream download URL → disk.

    🛡 INTEGRITY FIX (aadha-gaana bug):
      1. `.part` temp file me download (final path pe NAHI)
      2. Content-Length se verify — connection beech me kata to file REJECT
      3. Atomic os.replace — poora file hi kabhi final path pe aayega
      4. 2 attempts + fail hone par purani corrupt cache bhi clean
    Header me x-api-key jata hai → /file/ + /relay/ ka 401 khatam.
    """
    last_exc = None
    for attempt in range(3):
        part = f"{path}.part{attempt}"
        try:
            async with aiohttp.ClientSession(
                connector=_get_connector(),
                connector_owner=False,
            ) as session:
                async with session.get(
                    url,
                    headers=_HEADERS,
                    timeout=timeout or _DTIMEOUT,
                ) as resp:
                    if resp.status != 200:
                        raise ConnectionError(f"Download failed: HTTP {resp.status}")
                    clen = resp.headers.get("Content-Length")
                    _w_last = time.time()
                    _w_bytes = 0
                    with open(part, "wb") as f:
                        async for chunk in resp.content.iter_chunked(131072):
                            f.write(chunk)
                            _w_bytes += len(chunk)
                            _now = time.time()
                            if _now - _w_last >= 30:
                                if _w_bytes < 65536:   # <64KB/30s = stalled trickle
                                    raise ConnectionError(
                                        f"Download stalled: {_w_bytes}B in 30s")
                                _w_last = _now
                                _w_bytes = 0

            got = os.path.getsize(part) if os.path.exists(part) else 0
            if got <= MIN_VALID_BYTES:
                raise ConnectionError(f"Download too small: {got} bytes")
            if clen:
                try:
                    want = int(clen)
                    if got < want * 0.98:   # 2% tolerance — chunked edge cases
                        raise ConnectionError(
                            f"Truncated download: {got}/{want} bytes")
                except ValueError:
                    pass
            os.replace(part, path)          # atomic — complete file only
            return
        except Exception as e:
            last_exc = e
            logger.warning(f"download attempt {attempt + 1} failed: {e}")
            _cleanup(part)
            await asyncio.sleep(0.8 * (attempt + 1))
    # dono attempts fail — purani (shayad corrupt) cache bhi hata do
    try:
        _cleanup(path)
    except Exception:
        pass
    raise last_exc or ConnectionError("download failed")


# ── YouTubeAPI class ──────────────────────────────────────────────────────────

class YouTubeAPI:

    def __init__(self):
        self.base     = "https://www.youtube.com/watch?v="
        self.regex    = r"(?:youtube\.com|youtu\.be)"
        self.listbase = "https://youtube.com/playlist?list="
        self.reg      = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    async def name(self, link: str, videoid: Union[bool, str] = None):
        """
        Yukki-style: song-NAME ya URL/ID → (track_details, vidid)
        """
        if videoid:
            link = self.base + link

        # Case 1: URL ya 11-char video ID
        try:
            vidid = _video_id(link)
            watch = f"https://www.youtube.com/watch?v={vidid}"
            data  = await _api_get("/info", {"url": watch})
            d = data.get("data", {})
            return {
                "title":        d.get("title") or "Unknown",
                "link":         watch,
                "vidid":        vidid,
                "duration_min": _seconds_to_min(d.get("duration_seconds")),
            }, vidid
        except ValueError:
            pass   # song-name / Mix / playlist hai — niche resolve karo

        # Case 2: song-name / Mix-URL / playlist-URL → resolver
        r = await _resolve_query_to_video(link)
        if not r:
            raise ValueError(f"No results found for: {link}")
        return {
            "title":        r["title"],
            "link":         r["link"],
            "vidid":        r["id"],
            "duration_min": r["duration_min"],
        }, r["id"]

    async def exists(self, link: str, videoid: Union[bool, str] = None) -> bool:
        try:
            _normalise_video_url(link, videoid)
            return True
        except ValueError:
            return False

    async def url(self, message_1) -> Union[str, None]:
        """Message se pehla URL nikalo (pyrogram Message hi pass karo)."""
        if not _HAS_PYROGRAM:
            return None
        messages = [message_1]
        if message_1.reply_to_message:
            messages.append(message_1.reply_to_message)
        for message in messages:
            if message.entities:
                for entity in message.entities:
                    if entity.type == MessageEntityType.URL:
                        text = message.text or message.caption
                        return text[entity.offset: entity.offset + entity.length]
            elif message.caption_entities:
                for entity in message.caption_entities:
                    if entity.type == MessageEntityType.TEXT_LINK:
                        return entity.url
        return None

    def _clean(self, link: str) -> str:
        return link.split("&")[0] if "&" in link else link

    async def details(self, link: str, videoid: Union[bool, str] = None):
        """Returns: title, duration_min, duration_sec, thumbnail, vidid
        🎵 Song-NAME → /search fallback."""
        try:
            link = _normalise_video_url(link, videoid)
        except ValueError:
            r = await _resolve_query_to_video(link)
            if not r:
                raise
            link = r["link"]
        data = await _api_get("/info", {"url": link})
        d = data.get("data", {})
        title = d.get("title") or "Unknown"
        duration_sec = int(d.get("duration_seconds") or 0)
        duration_min = _seconds_to_min(duration_sec)
        vidid = d.get("id") or _video_id(link)
        # 🖼 thumbnail guarantee — khali thumb bot ke photo-send todta tha
        thumbnail = d.get("thumbnail") \
            or f"https://i.ytimg.com/vi/{vidid}/hqdefault.jpg"
        return title, duration_min, duration_sec, thumbnail, vidid

    async def title(self, link: str, videoid: Union[bool, str] = None) -> str:
        t, *_ = await self.details(link, videoid)
        return t

    async def duration(self, link: str, videoid: Union[bool, str] = None) -> str:
        _, d, *_ = await self.details(link, videoid)
        return d

    async def thumbnail(self, link: str, videoid: Union[bool, str] = None) -> str:
        _, _, _, t, _ = await self.details(link, videoid)
        return t

    async def track(self, link: str, videoid: Union[bool, str] = None):
        """Returns: track_details dict, vidid.
        🎵 Song-NAME diya ho to HellAPI /search se pehla result (bot ke
        'FAILED TO PROCESS QUERY' ka fix — search disabled tha)."""
        try:
            link = _normalise_video_url(link, videoid)
        except ValueError:
            r = await _resolve_query_to_video(link)
            if not r:
                raise
            vid = r["id"]
            return {
                "title":        r.get("title") or "Unknown",
                "link":         r["link"],
                "vidid":        vid,
                "duration_min": r.get("duration_min") or _seconds_to_min(r.get("duration")),
                "thumb":        r.get("thumb") or r.get("thumbnail")
                                or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            }, vid
        data = await _api_get("/info", {"url": link})
        d = data.get("data", {})
        title = d.get("title") or "Unknown"
        vidid = d.get("id") or _video_id(link)
        duration_min = _seconds_to_min(d.get("duration_seconds"))
        thumbnail = d.get("thumbnail") \
            or f"https://i.ytimg.com/vi/{vidid}/hqdefault.jpg"
        yturl = link
        return {
            "title":        title,
            "link":         yturl,
            "vidid":        vidid,
            "duration_min": duration_min,
            "thumb":        thumbnail,
        }, vidid

    async def formats(self, link: str, videoid: Union[bool, str] = None):
        """Returns: formats_list, link"""
        link = _normalise_video_url(link, videoid)
        data = await _api_get("/formats", {"url": link})
        result = []
        for f in data.get("formats", []):
            fmt_str = f.get("resolution") or f.get("ext") or ""
            if "dash" in fmt_str.lower():
                continue
            result.append({
                "format":      f"{f.get('format_id')} - {fmt_str}",
                "filesize":    f.get("filesize_approx"),
                "format_id":   f.get("format_id"),
                "ext":         f.get("ext"),
                "format_note": f.get("resolution") or "",
                "yturl":       link,
            })
        return result, link

    async def slider(self, link: str, query_type: int,
                     videoid: Union[bool, str] = None):
        """Return details for URL ya song-name (/search fallback)."""
        if not videoid and not re.search(self.regex, str(link or "")):
            r = await _resolve_query_to_video(link)
            if r:
                link = r["link"]
            else:
                raise ValueError(f"No results found for: {link}")
        title, duration, _, thumbnail, vidid = await self.details(link, videoid)
        return title, duration, thumbnail, vidid

    async def playlist(self, link, limit, user_id,
                       videoid: Union[bool, str] = None):
        """
        Yukki-style: list of (title, duration_min, url, vidid) tuples.
        """
        if videoid:
            link = self.listbase + link
        link = self._clean(link)
        try:
            data    = await _api_get("/info", {"url": link}, retries=0)
            entries = data.get("data", {}).get("entries", []) or []
            out = []
            for e in entries[: max(1, int(limit))]:
                if not isinstance(e, dict) or not e.get("id"):
                    continue
                vidid = e["id"]
                title = e.get("title") or "Unknown"
                dur   = e.get("duration_seconds") or e.get("duration") or 0
                out.append((title, _seconds_to_min(dur), self.base + vidid, vidid))
            if out:
                return out
        except Exception:
            pass
        return []

    async def related(self, videoid: str,
                      exclude_ids: Union[list, set, None] = None):
        """Returns next recommended track dict or None."""
        exclude = set(exclude_ids or [])
        exclude.add(videoid)
        try:
            info      = await _api_get("/info", {"url": self.base + videoid})
            seed_title = info.get("data", {}).get("title", "")
            if not seed_title:
                return None
            search = await _api_get("/search", {"q": seed_title, "limit": 10})
            for r in search.get("results", []):
                vid = r.get("id")
                dur = r.get("duration")
                if vid and dur and vid not in exclude:
                    return {
                        "title":        r.get("title", "Unknown"),
                        "vidid":        vid,
                        "duration_min": _seconds_to_min(dur),
                        "thumb":        r.get("thumbnail", ""),
                        "link":         r.get("url", self.base + vid),
                    }
        except Exception:
            pass
        return None

    async def video(self, link: str, videoid: Union[bool, str] = None):
        """Returns: (1, file_path) or (0, error_msg)"""
        try:
            link = _normalise_video_url(link, videoid)
            data       = await _api_get("/api/stream",
                                        {"url": link, "type": "video"},
                                        timeout=_VTIMEOUT)
            stream_url = data.get("stream_url") or data.get("direct_url")
            if not stream_url:
                return 0, "No stream URL"
            vid_id    = _video_id(link)
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            file_path = os.path.join(DOWNLOAD_DIR, f"{vid_id}.mp4")
            if os.path.exists(file_path) \
                    and os.path.getsize(file_path) > MIN_VALID_BYTES:
                return 1, file_path
            await _download_file(stream_url, file_path)
            if os.path.exists(file_path) \
                    and os.path.getsize(file_path) > MIN_VALID_BYTES:
                return 1, file_path
            _cleanup(file_path)
            return 0, "Empty file"
        except Exception as e:
            return 0, str(e)

    async def download(
        self,
        link: str,
        mystic,
        video:     Union[bool, str] = None,
        videoid:   Union[bool, str] = None,
        songaudio: Union[bool, str] = None,
        songvideo: Union[bool, str] = None,
        format_id: Union[bool, str] = None,
        title:     Union[bool, str] = None,
        force_download: Union[bool, str] = None,
    ) -> tuple:
        """Main download — used by /play, /vplay. Returns (path, True) or (None, False).
        force_download=True → URL-stream skip, hamesha file download (fallback)."""
        file_path = None
        _t0 = time.perf_counter()
        try:
            link = _normalise_video_url(link, videoid)
            media_type = "video" if (video or songvideo) else "audio"
            if format_id:
                data = await _api_get("/ytdl",
                                      {"url": link, "format": str(format_id)},
                                      timeout=_VTIMEOUT)
            else:
                data = await _api_get("/api/stream",
                                      {"url": link, "type": media_type},
                                      timeout=_VTIMEOUT)
            _t_api = time.perf_counter()

            # 🔴 LIVE detection: relay/archive path live ke liye kaam nahi karta
            # (googlevideo DASH URL live pe dead hota hai) — /api/live se HLS
            # playlist URL lo aur seedha stream karo (download impossible).
            if data.get("is_live") or "m3u8" in (data.get("stream_url") or "") \
                    or "m3u8" in (data.get("direct_url") or ""):
                try:
                    live = await _api_get("/api/live", {"url": link},
                                          timeout=_VTIMEOUT)
                    hls = live.get("hls_url") or live.get("stream_url")
                    if hls:
                        logger.info(
                            f"timing {_video_id(link)}: api={(_t_api - _t0) * 1000:.0f}ms "
                            f"-> LIVE HLS stream (no download)")
                        return hls, True
                except Exception as e:
                    logger.warning(f"live fetch fail: {e}")
                return None, False

            stream_url = (data.get("stream_url")
                          or data.get("direct_url")
                          or data.get("url"))
            if not stream_url:
                return None, False

            vid_id    = _video_id(link)
            ext       = data.get("ext") or ("mp4" if media_type == "video" else "mp3")
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            file_path = os.path.join(DOWNLOAD_DIR, f"{vid_id}.{ext}")

            # 🎬 VPLAY FIX: relay URL DASH-only (sirf video-track, audio missing)
            # ho to VC me video ya to frozen aata hai ya audio nahi hota.
            # Archive video (proper progressive mp4) hai to WAHI best hai.
            if media_type == "video" and "/relay/" in stream_url:
                try:
                    async with aiohttp.ClientSession(
                        connector=_get_connector(), connector_owner=False,
                    ) as _s2:
                        async with _s2.head(
                            stream_url, headers=_HEADERS,
                            timeout=aiohttp.ClientTimeout(total=6, connect=3),
                        ) as _hr:
                            _ct = (_hr.headers.get("Content-Type") or "").lower()
                    if ("webm" in _ct or _ct.endswith("webm")) and await _url_alive(stream_url):
                        logger.info(
                            f"timing {vid_id}: api={(_t_api - _t0) * 1000:.0f}ms "
                            f"-> URL-stream (relay DASH-webm, ffmpeg handles)")
                        return stream_url, True
                except Exception:
                    pass

            # 🛡 sanity check — 100KB se chhoti purani file = corrupt cache
            if os.path.exists(file_path) \
                    and os.path.getsize(file_path) > MIN_VALID_BYTES:
                _exp = data.get("filesize")
                if not _exp:
                    return file_path, True
                _have = os.path.getsize(file_path)
                # API ne exact stored size diya — mismatch (2%+ ya 64KB se
                # bada) matlab purani ADHURI cache → auto-heal refetch
                if abs(_have - int(_exp)) <= max(int(_exp) * 0.02, 65536):
                    return file_path, True
                logger.info(f"stale cache {_have}/{_exp} bytes — refetch {vid_id}")
                _cleanup(file_path)

            # ⚡ URL-STREAM: HellAPI ke /relay/ + /file/ URLs download ke bina
            # seedha pytgcalls ko — server HTTP-range se pipe karta hai.
            # "Stream Ready" 19s → 3-6s (extraction+roundtrip jitna hi).
            # 🛡 PRE-FLIGHT: 1-byte probe se URL zinda-hai check — dead/expired
            # relay URL VC ko kabhi nahi pahunchega, download-fallback chalega.
            # 🔁 PROBE-FAIL → REFRESH RETRY: dead relay pe FULL DOWNLOAD mat
            # karo (95-600s trap) — ?refresh=1 se API naya URL de deta hai.
            if URL_STREAM and not force_download \
                    and _is_streamable_url(stream_url):
                if await _url_alive(stream_url):
                    _src = "relay" if "/relay/" in stream_url else "file"
                    logger.info(
                        f"timing {vid_id}: api={(_t_api - _t0) * 1000:.0f}ms "
                        f"-> URL-stream ({_src}, no download)")
                    return stream_url, True
                # /file/ dead hui to refresh kuch nahi badlega — sirf relay
                if "/relay/" in stream_url:
                    _sep = "&" if "?" in stream_url else "?"
                    _r_url = f"{stream_url}{_sep}refresh=1"
                    try:
                        await asyncio.sleep(0.3)
                        if await _url_alive(_r_url):
                            logger.info(
                                f"timing {vid_id}: api={(_t_api - _t0) * 1000:.0f}ms "
                                f"-> URL-stream (relay REFRESHED, no download)")
                            return _r_url, True
                    except Exception:
                        pass
                    logger.warning(
                        f"relay dead + refresh fail {vid_id} — full download fallback")

            await _download_file(stream_url, file_path)
            _t_dl = time.perf_counter()

            if os.path.exists(file_path) \
                    and os.path.getsize(file_path) > MIN_VALID_BYTES:
                logger.info(
                    f"timing {vid_id}: api={(_t_api - _t0) * 1000:.0f}ms "
                    f"download={_t_dl - _t_api:.1f}s "
                    f"({os.path.getsize(file_path) // 1024}KB)")
                return file_path, True
            _cleanup(file_path)
            return None, False

        except Exception:
            # 🔁 LAST-RESORT: API 429/unavailable ho gaya ho, par BOT-CACHE me
            # pehle se valid file hai to WAHI serve karo (vplay 429 pe bhi
            # chalna chahiye — pehle audio-archive se galat play hota tha)
            try:
                if file_path and os.path.exists(file_path) \
                        and os.path.getsize(file_path) > MIN_VALID_BYTES:
                    logger.info(f"timing {vid_id if 'vid_id' in dir() else ''}: "
                                f"API fail — BOT-CACHE fallback ({file_path})")
                    return file_path, True
                if file_path:
                    _cleanup(file_path)
            except Exception:
                pass
            return None, False


# ── Singleton ─────────────────────────────────────────────────────────────────

YouTube = YouTubeAPI()
