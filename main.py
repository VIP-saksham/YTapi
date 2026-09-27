import re
"""
main.py
--------
HellAPI v5.0 — Commercial YouTube Stream API

Plans:
  free   →   50 req/day,   5 req/min  — Free
  basic  →  500 req/day,  20 req/min  — ₹99/month
  pro    → 5000 req/day,  60 req/min  — ₹299/month
  ultra  →  Unlimited,   200 req/min  — ₹699/month

Auth:
  Header:      x-api-key: HellAPIxxxxxxxx
  Query param: ?api_key=HellAPIxxxxxxxx
"""

import os
import logging
import re
import asyncio
import threading
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, Response
from dotenv import load_dotenv

from ytdlp import (
    get_video_info,
    get_available_formats,
    get_direct_download_url,
    get_proxy_pool,
    get_proxy_health,
    get_circuit_state,
    get_hourly_budget,
    get_cookie_health,
    stream_cache_stats,
    negative_cache_stats,
    reload_proxies,
    search_youtube,
    cache_stats,
    cache_clear,
    IPBlockedError,
    ExtractionError,
    get_stream_data,
    get_stream_data_with_fallback,
    _relay_get,
    _prearchive,
    _tg_file_stored,
    _alive_proxies,
    local_cache_path,
    _local_cache_evict,
)


# ── 🔥 PRE-ARCHIVE (search-driven prefetch) ─────────────────────────────
# /search results ke top songs BG me race-extract ho jate hain — user jab
# play karta hai to relay ready milta hai (first-play 13s → 2-3s).
_PRE_ARCHIVE = int(os.getenv("PRE_ARCHIVE_N", "3"))
_PRE_SEEN: set = set()
_PRE_SEEN_LOCK = threading.Lock()
_PRE = None          # uvicorn ke pehle event-loop — create_task usi pe chalega


def _prekick(ids: list):
    """Prefetch task (non-blocking, chup-chaap)."""
    global _PRE
    if _PRE is None:
        _PRE = asyncio.get_event_loop()
    for vid in ids[:_PRE_ARCHIVE]:
        with _PRE_SEEN_LOCK:
            if vid in _PRE_SEEN:
                continue
            _PRE_SEEN.add(vid)
        try:
            _PRE.create_task(
                _prearchive(f"https://www.youtube.com/watch?v={vid}"))
        except Exception:
            pass

# ── Shared async httpx client (relay ke liye) ────────────────────────────
import httpx as _httpx
_HTTP = None
_HTTP_LOCK = threading.Lock()
_HTTP_PROXY_CLIENTS = {}


def _http_client(proxy: Optional[str] = None) -> _httpx.AsyncClient:
    """Shared client (direct) ya per-proxy cached client (proxied relay)."""
    global _HTTP
    if proxy:
        with _HTTP_LOCK:
            c = _HTTP_PROXY_CLIENTS.get(proxy)
            if c is None or c.is_closed:
                c = _httpx.AsyncClient(
                    proxy=f"http://{proxy}",
                    timeout=_httpx.Timeout(30, read=90),
                    follow_redirects=True,
                    limits=_httpx.Limits(max_connections=20,
                                         max_keepalive_connections=10),
                )
                _HTTP_PROXY_CLIENTS[proxy] = c
            return c
    with _HTTP_LOCK:
        if _HTTP is None or _HTTP.is_closed:
            _HTTP = _httpx.AsyncClient(
                timeout=_httpx.Timeout(30, read=90),
                follow_redirects=True,
                limits=_httpx.Limits(max_connections=20,
                                     max_keepalive_connections=10),
            )
        return _HTTP

import database as db
from database import (
    is_ip_blocked,
    record_invalid_attempt,
    reset_attempts,
    log_key_audit,
    get_bf_stats,
    get_video_cache_stats,
    get_stream_cache_stats,
    clear_video_cache,
)

load_dotenv()

logger   = logging.getLogger(__name__)
APP_NAME = os.getenv("APP_NAME", "HellAPI")
APP_URL  = os.getenv("APP_URL", "")           # e.g. https://hellapi.onrender.com
BOT_URL  = os.getenv("BOT_URL", "")           # e.g. https://t.me/HellAPIBot
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
ALLOW_SEARCH = os.getenv("ALLOW_SEARCH", "false").strip().lower() in {
    "1", "true", "yes", "on"
}


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # yt-dlp auto-update + IP-watchdog daemon (hourly update, block pe restart)
    try:
        import autoupdate
        autoupdate.start_daemon()
    except Exception as e:
        logger.warning(f"autoupdate daemon start fail: {e}")
    logger.info(f"{APP_NAME} v5.0 started.")
    yield

app = FastAPI(
    title=f"{APP_NAME} — YouTube Stream API",
    description=(
        "Commercial-grade YouTube audio/video stream API.\n\n"
        f"Get your free API key from our Telegram bot: {BOT_URL or 'Contact admin'}\n\n"
        "**Authentication:** Pass your key via `x-api-key` header or `?api_key=` query param.\n\n"
        "**Plans:** Free → Basic → Pro → Ultra (see `/plans`)"
    ),
    version="5.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_client_ip(request: Request) -> str:
    """Extract real client IP, respecting reverse-proxy headers."""
    # Render / Railway / Heroku set X-Forwarded-For
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


def _extract_key(request: Request) -> str:
    key = request.headers.get("x-api-key") or request.query_params.get("api_key")
    if not key:
        raise HTTPException(
            status_code=401,
            detail={
                "error":   "missing_key",
                "message": (
                    "API key required. Pass via header 'x-api-key' or '?api_key=' param. "
                    f"Get your free key at {BOT_URL or 'our Telegram bot'}."
                ),
            },
        )
    return key

def _embed_key(url: str, request: Request) -> str:
    """Stream-URL me api_key embed karo (headerless ffmpeg/VC ke liye)."""
    if not url or "api_key=" in url:
        return url
    key = _extract_key(request)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}api_key={key}" if key else url



def require_valid_key(request: Request) -> dict:
    key = _extract_key(request)
    ip  = _get_client_ip(request)

    # ── Brute-force / IP block check ────────────────────────────────────
    if is_ip_blocked(ip):
        raise HTTPException(
            status_code=429,
            detail={
                "error":   "ip_blocked",
                "message": "Too many invalid attempts. Your IP is temporarily blocked.",
                "retry_after": "5 minutes",
            },
            headers={"Retry-After": str(db.BF_BLOCK_SEC)},
        )

    # ── Key validation ───────────────────────────────────────────────────
    key_row = db.validate_key(key)
    if not key_row:
        result = record_invalid_attempt(ip)
        # Log failed attempt
        log_key_audit(0, key[:39], "invalid_attempt", ip=ip,
                      note=f"attempt {result['attempts']}/{db.BF_MAX_ATTEMPTS}")
        if result["blocked"]:
            logger.warning(f"IP blocked after {result['attempts']} invalid attempts: {ip}")
            raise HTTPException(
                status_code=429,
                detail={
                    "error":   "ip_blocked",
                    "message": (
                        f"Too many invalid key attempts. "
                        f"IP blocked for {db.BF_BLOCK_SEC // 60} minutes."
                    ),
                },
            )
        raise HTTPException(
            status_code=401,
            detail={
                "error":   "invalid_key",
                "message": (
                    "Invalid or expired API key. "
                    f"Get a new key at {BOT_URL or 'our Telegram bot'}."
                ),
            },
        )

    # Valid key — clear any failed attempt counter for this IP
    reset_attempts(ip)

    # ── Rate limit check ─────────────────────────────────────────────────
    rate = db.check_rate_limit(key)
    if not rate["allowed"]:
        plan     = rate.get("plan", "free")
        plan_cfg = db.PLANS.get(plan, db.PLANS["free"])
        reason   = rate.get("reason", "")

        if reason == "rpm_exceeded":
            msg = (
                f"Too many requests per minute. "
                f"Your plan '{plan}' allows {plan_cfg['rpm']} req/min. "
                "Upgrade for higher limits."
            )
            retry_after = 60
        else:
            lim = "Unlimited" if plan_cfg["rpd"] == -1 else str(plan_cfg["rpd"])
            msg = (
                f"Daily limit reached ({rate['used']}/{lim}). "
                f"Plan: {plan}. Resets at midnight UTC. "
                f"Upgrade at {BOT_URL or 'our Telegram bot'}."
            )
            now_utc = datetime.utcnow()
            midnight = (now_utc + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            retry_after = max(60, int((midnight - now_utc).total_seconds()))

        raise HTTPException(
            status_code=429,
            detail={
                "error":       "rate_limit_exceeded",
                "message":     msg,
                "plan":        plan,
                "used":        rate.get("used", 0),
                "limit":       rate.get("limit", 0),
                "upgrade_url": BOT_URL,
            },
            headers={"Retry-After": str(retry_after)},
        )

    return {"key": key, "plan": rate["plan"], "used": rate["used"],
            "limit": rate["limit"], "row": key_row}


def require_admin(request: Request):
    key = request.headers.get("x-api-key") or request.query_params.get("api_key")
    # Fail-closed: ADMIN_KEY set nahi hai to admin endpoints band — koi bypass nahi.
    if not ADMIN_KEY or not key or key != ADMIN_KEY:
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "Admin access required."},
        )


# ---------------------------------------------------------------------------
# Error mapper
# ---------------------------------------------------------------------------

def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, IPBlockedError):
        msg = str(exc)
        # Circuit-breaker block vs normal YouTube block — retry time dono me alag.
        if "circuit_open" in msg:
            retry_after = get_circuit_state()["retry_after_sec"] or 30
        else:
            retry_after = 60
        return HTTPException(
            status_code=429,
            detail={
                "error":       "ip_blocked",
                "message":     "YouTube rate limit / IP block. Thodi der baad retry karo.",
                "retry_after_sec": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )
    if isinstance(exc, ExtractionError):
        msg = str(exc).lower()
        if "private" in msg:
            friendly = "This video is private."
        elif "unavailable" in msg or "removed" in msg:
            friendly = "This video is unavailable or has been removed."
        elif "copyright" in msg:
            friendly = "This video is blocked due to copyright."
        elif "age" in msg:
            friendly = "This video requires age verification. Set YTDLP_COOKIE_FILE to bypass."
        else:
            friendly = f"Could not extract stream URL. ({str(exc)[:150]})"
        return HTTPException(
            status_code=400,
            detail={"error": "extraction_failed", "message": friendly},
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status_code=400,
            detail={"error": "bad_request", "message": str(exc)},
        )
    logger.error(f"Unhandled error: {exc}", exc_info=True)
    return HTTPException(
        status_code=500,
        detail={"error": "internal_error", "message": str(exc)},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_url(q: str) -> str:
    """Resolve an input while keeping song-name search explicitly opt-in."""
    if q.startswith("http://") or q.startswith("https://"):
        return q
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", q.strip()):
        return f"https://www.youtube.com/watch?v={q.strip()}"
    if not ALLOW_SEARCH:
        raise HTTPException(
            status_code=410,
            detail={
                "error": "search_disabled",
                "message": (
                    "Song-name search is disabled. Send a YouTube URL or video ID."
                ),
            },
        )
    results = await search_youtube(q, max_results=1)
    if not results:
        raise ValueError(f"No YouTube results found for: {q}")
    return results[0]["url"]


def _log(key_info: dict, endpoint: str, query: str,
         success: bool = True, media_type: str = "audio"):
    try:
        row = key_info.get("row") or db.validate_key(key_info["key"])
        if row:
            db.log_usage(row["user_id"], key_info["key"],
                         endpoint, query, success, media_type)
    except Exception as e:
        logger.warning(f"log_usage: {e}")


def _lib_save(info: dict, source: str = "play"):
    """
    CENTRAL SONG LIBRARY — har successful search/play yahan save hota hai.
    Har API user ka song tumhare database me permanently + play_count ke saath.
    Fire-and-forget — API response kabhi block na ho.
    """
    try:
        vid = (info.get("id") or info.get("video_id") or "").strip()
        if not vid or len(vid) != 11:
            return
        db.library_put(
            vid,
            info.get("title") or "",
            info.get("uploader") or "",
            int(info.get("duration_seconds") or 0),
            info.get("thumbnail") or "",
            source=source,
        )
    except Exception as e:
        logger.warning(f"lib_save: {e}")


# ---------------------------------------------------------------------------
# TG FILE SERVING — stored MP3 pehle, YouTube baad me
# ---------------------------------------------------------------------------

def _vid_from_query(q: str) -> str:
    """URL ya 11-char ID se video_id (search queries ke liye '')."""
    q = (q or "").strip()
    if not q:
        return ""
    if "youtu" in q:
        try:
            vid = db._url_to_video_id(q)
            return vid if vid and not vid.startswith("q_") and len(vid) == 11 else ""
        except Exception:
            return ""
    return q if len(q) == 11 and q.replace("-", "").replace("_", "").isalnum() else ""


def _tg_file_lookup(q: str) -> Optional[dict]:
    """Stored MP3 hai kya? URL/id ke liye direct, naam ke liye library match."""
    vid = _vid_from_query(q)
    if not vid:
        # naam se search — library me top match jiski file stored hai
        lib = db.library_search(q, 3)
        for cand in lib:
            row = db.tg_file_get(cand["video_id"])
            if row and row[0]:
                return {"video_id": cand["video_id"], "file_id": row[0],
                        "title": row[1], "matched_by": "library"}
        return None
    row = db.tg_file_get(vid)
    if row and row[0]:
        return {"video_id": vid, "file_id": row[0], "title": row[1],
                "matched_by": "id"}
    return None


def _file_play_url(request: Request, video_id: str, api_key: str) -> str:
    base = (APP_URL or str(request.base_url)).rstrip("/")
    return f"{base}/file/{video_id}?api_key={api_key}"


def _stored_meta(vid: str, fallback_title: str = "") -> dict:
    """Library/video_cache se metadata (title/duration/thumbnail)."""
    lib = db.library_get(vid) or {}
    vc  = db.get_cached_video_metadata(f"https://www.youtube.com/watch?v={vid}") or {}
    return {
        "title":      lib.get("title") or vc.get("title") or fallback_title,
        "uploader":   lib.get("uploader") or vc.get("uploader") or "",
        "duration":   int(lib.get("duration_sec") or vc.get("duration_seconds") or 0),
        "thumbnail":  lib.get("thumbnail") or vc.get("thumbnail") or "",
    }


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------

@app.get("/youtube.py", tags=["Files"], include_in_schema=False)
async def serve_youtube_py(request: Request):
    """Serve the working HellAPI platform client (Youtube.py) — koi bhi valid
    API-key wala bot owner download kar sakta hai (apne bot me drop-in)."""
    require_valid_key(request)
    import pathlib
    from fastapi.responses import PlainTextResponse
    src = pathlib.Path(__file__).parent / "youtube.py"
    if not src.exists():
        raise HTTPException(status_code=404, detail="youtube.py not found")
    return PlainTextResponse(content=src.read_text(encoding="utf-8"),
                             media_type="text/plain")


@app.get("/", tags=["Info"], summary="API info & endpoint list")
def root():
    return {
        "api":       APP_NAME,
        "version":   "5.0.0",
        "status":    "online",
        "bot_url":   BOT_URL,
        "docs_url":  f"{APP_URL}/docs" if APP_URL else "/docs",
        "plans_url": f"{APP_URL}/plans" if APP_URL else "/plans",
        "auth":      "x-api-key header  OR  ?api_key= param",
        "endpoints": {
            "GET /stream":     "?url=YT_URL_OR_ID  →  universal stream",
            "GET /song":       "?url=YT_URL_OR_ID [&video=true]  →  AnonXMusic/Yukki style",
            "GET /api/stream": "?url=YT_URL_OR_ID &type=audio|video",
            "GET /ytdl":       "?url= &format=bestaudio  →  raw yt-dlp style",
            "GET /search":     "disabled by default (set ALLOW_SEARCH=true to enable)",
            "GET /info":       "?url=  →  video metadata",
            "GET /formats":    "?url=  →  all available formats",
            "GET /my/stats":   "your usage stats",
            "GET /plans":      "all plans & pricing",
        },
    }


@app.get("/plans", tags=["Info"], summary="All plans & pricing")
def plans():
    result = {}
    for plan_id, plan in db.PLANS.items():
        result[plan_id] = {
            **plan,
            "rpd_display": "Unlimited" if plan["rpd"] == -1 else f"{plan['rpd']:,}",
            "get_key":     BOT_URL,
        }
    return {"plans": result, "get_key_bot": BOT_URL}


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------

@app.get("/search", tags=["Search"], summary="Search YouTube")
async def yt_search(
    request: Request,
    q:     str = Query(..., description="Search query"),
    limit: int = Query(5, ge=1, le=10, description="Number of results (1-10)"),
):
    key_info = require_valid_key(request)
    if not ALLOW_SEARCH:
        raise HTTPException(
            status_code=410,
            detail={
                "error": "search_disabled",
                "message": "Song-name search is disabled for this API.",
            },
        )
    try:
        results = await search_youtube(q, max_results=limit)
        for r in results:
            _lib_save(r, source="search")     # song library me save
        # 🔥 PRE-ARCHIVE: top-3 results BG me race-extract — first-play fast
        _vids = []
        for r in results:
            v = str(r.get("id") or "").strip()
            if not v or len(v) < 8:
                v = (r.get("url") or "").split("v=")[-1].split("&")[0].strip()
            if v and len(v) >= 8:
                _vids.append(v[:11])
        if _vids:
            _prekick(_vids)
        _log(key_info, "/search", q, media_type="search")
        return {"ok": True, "query": q, "count": len(results), "results": results}
    except Exception as e:
        _log(key_info, "/search", q, success=False)
        raise _map_error(e)


# ---------------------------------------------------------------------------
# /stream  — universal
# ---------------------------------------------------------------------------

@app.get("/stream", tags=["Stream"], summary="Universal stream URL")
async def universal_stream(
    request: Request,
    url:     Optional[str] = Query(None, description="YouTube URL"),
    query:   Optional[str] = Query(None, description="Search query"),
):
    key_info = require_valid_key(request)
    q = url or query
    if not q:
        raise HTTPException(400, {"error": "missing_param",
                                  "message": "Provide 'url' or 'query'."})
    try:
        resolved = await _resolve_url(q)
        info, result = await _fetch_info_and_dl(resolved, "bestaudio/best")
        _lib_save(info, source="play")            # song library me save
        _log(key_info, "/stream", q)
        return {
            "status":     "ok",
            "cached":     False,
            "plan":       key_info["plan"],
            "title":      info.get("title") or "",
            "duration":   info.get("duration_seconds") or 0,
            "thumbnail":  info.get("thumbnail") or "",
            "uploader":   info.get("uploader") or "",
            "view_count": info.get("view_count") or 0,
            "stream_url": _embed_key(result["direct_url"], request),
            "source_url": resolved,
            "ext":        result.get("ext") or "",
            "format_id":  result.get("format_id") or "",
            "is_live":    result.get("is_live", False),
        }
    except Exception as e:
        _log(key_info, "/stream", q, success=False)
        raise _map_error(e)


# ---------------------------------------------------------------------------
# /song  — AnonXMusic / YukkiMusic / MusicBot style
# ---------------------------------------------------------------------------

@app.get("/song", tags=["Stream"], summary="Song stream (AnonX/Yukki style)")
async def song_endpoint(
    request: Request,
    url:     Optional[str] = Query(None),
    query:   Optional[str] = Query(None),
    video:   bool          = Query(False, description="True for video stream"),
):
    key_info = require_valid_key(request)
    q = url or query
    if not q:
        raise HTTPException(400, {"error": "missing_param",
                                  "message": "Provide 'url' or 'query'."})
    # ── FILE-FIRST: stored MP3 hai? YouTube ko chhue bina serve karo ─────
    if not video:
        hit = _tg_file_lookup(q)
        if hit:
            meta     = _stored_meta(hit["video_id"], hit["title"])
            play_url = _file_play_url(request, hit["video_id"], key_info["key"])
            mtype    = "audio"
            _log(key_info, "/song", q, media_type=mtype)
            try:
                db.library_put(hit["video_id"], meta["title"],
                               meta["uploader"], meta["duration"],
                               meta["thumbnail"], source="file-hit")
            except Exception:
                pass
            return {
                "results": [{
                    "title":        meta["title"] or hit["title"],
                    "link":         f"https://www.youtube.com/watch?v={hit['video_id']}",
                    "duration":     f"{meta['duration']//60}:{meta['duration']%60:02d}",
                    "duration_sec": meta["duration"],
                    "thumbnail":    meta["thumbnail"],
                    "uploader":     meta["uploader"],
                    "streamLink":   play_url,
                    "stream_url":   play_url,
                    "ext":          "mp3",
                    "isLive":       False,
                    "view_count":   0,
                    "source":       "tg_file",
                }],
                "cached": True,
                "note":   "Served from permanent TG MP3 store — zero YouTube hit",
            }

    try:
        fmt      = "bestvideo+bestaudio/best" if video else "bestaudio/best"
        resolved = await _resolve_url(q)
        info, result = await _fetch_info_and_dl(resolved, fmt)
        _lib_save(info, source="play")            # song library me save

        dur  = info.get("duration_seconds") or 0
        mins, secs = divmod(int(dur), 60)
        mtype = "video" if video else "audio"
        _log(key_info, "/song", q, media_type=mtype)

        return {
            "results": [{
                "title":        info.get("title") or "",
                "link":         resolved,
                "duration":     f"{mins}:{secs:02d}",
                "duration_sec": dur,
                "thumbnail":    info.get("thumbnail") or "",
                "uploader":     info.get("uploader") or "",
                "streamLink":   _embed_key(result["direct_url"], request),
                "stream_url":   _embed_key(result["direct_url"], request),
                "ext":          result.get("ext") or "",
                "isLive":       result.get("is_live", False),
                "view_count":   info.get("view_count") or 0,
            }]
        }
    except Exception as e:
        _log(key_info, "/song", q, success=False)
        raise _map_error(e)


# ---------------------------------------------------------------------------
# /api/stream  — audio / video toggle
# ---------------------------------------------------------------------------

@app.get("/api/stream", tags=["Stream"], summary="Audio or video stream")
async def api_stream(
    request: Request,
    url:     Optional[str] = Query(None),
    query:   Optional[str] = Query(None),
    type:    str           = Query("audio", description="'audio' or 'video'"),
):
    key_info = require_valid_key(request)
    q = url or query
    if not q:
        raise HTTPException(400, {"error": "missing_param",
                                  "message": "Provide 'url' or 'query'."})
    if type not in ("audio", "video"):
        raise HTTPException(400, {"error": "bad_param",
                                  "message": "type must be 'audio' or 'video'."})
    vid_q = _vid_from_query(q) or (q.strip() if len(q.strip()) == 11 else "")
    # 🎬 VPLAY-KIND GUARD: video manga ho aur VIDEO archive/disk-cache
    # available ho to YouTube ko chhue BINA wahi serve karo — 429/unavailable
    # hone par bhi vplay hamesha chalega (pehle yahan AUDIO fallback hota tha
    # jo vplay me audio baja deta tha)
    if type == "video" and vid_q and len(vid_q) == 11:
        try:
            _tg_row = db.tg_file_get(vid_q, kind="video")
            _has_local = bool(local_cache_path(vid_q, "video"))
            if (_tg_row and _tg_row[0]) or _has_local:
                _meta = _stored_meta(vid_q)
                _purl = _file_play_url(request, vid_q, key_info["key"]) + "&kind=video"
                _log(key_info, "/api/stream", q, media_type="video")
                return {
                    "ok": True, "type": "video", "plan": key_info["plan"],
                    "url": f"https://www.youtube.com/watch?v={vid_q}",
                    "title": _meta.get("title") or vid_q,
                    "duration": _meta.get("duration") or 0,
                    "thumbnail": _meta.get("thumbnail") or "",
                    "uploader": _meta.get("uploader") or "",
                    "stream_url": _purl, "direct_url": _purl,
                    "format_id": "tgfile", "ext": "mp4", "is_live": False,
                }
        except Exception:
            pass
    try:
        fmt      = "bestaudio/best" if type == "audio" else "bestvideo+bestaudio/best"
        resolved = await _resolve_url(q)
        info, result = await _fetch_info_and_dl(resolved, fmt)
        _lib_save(info, source="play")            # song library me save
        _log(key_info, "/api/stream", q, media_type=type)

        return {
            "ok":         True,
            "type":       type,
            "plan":       key_info["plan"],
            "url":        resolved,
            "title":      info.get("title") or "",
            "duration":   info.get("duration_seconds") or 0,
            "thumbnail":  info.get("thumbnail") or "",
            "uploader":   info.get("uploader") or "",
            "stream_url": _embed_key(result["direct_url"], request),
            "direct_url": _embed_key(result["direct_url"], request),
            "format_id":  result.get("format_id") or "",
            "ext":        result.get("ext") or "",
            "is_live":    result.get("is_live", False),
        }
    except Exception as e:
        _log(key_info, "/api/stream", q, success=False)
        raise _map_error(e)


# ---------------------------------------------------------------------------
# /ytdl  — raw yt-dlp style
# ---------------------------------------------------------------------------

@app.get("/ytdl", tags=["Stream"], summary="Raw yt-dlp style response")
async def ytdl_endpoint(
    request: Request,
    url:    str = Query(...),
    format: str = Query("bestaudio"),
):
    key_info = require_valid_key(request)
    try:
        info, result = await _fetch_info_and_dl(url, format)
        _lib_save(info, source="play")            # song library me save
        _log(key_info, "/ytdl", url)
        return {
            "id":          info.get("id"),
            "title":       info.get("title"),
            "uploader":    info.get("uploader"),
            "duration":    info.get("duration_seconds"),
            "thumbnail":   info.get("thumbnail"),
            "webpage_url": url,
            "url":         _embed_key(result["direct_url"], request),
            "ext":         result.get("ext"),
            "format_id":   result.get("format_id"),
            "acodec":      "opus",
            "vcodec":      "none" if "audio" in format else "h264",
        }
    except Exception as e:
        _log(key_info, "/ytdl", url, success=False)
        raise _map_error(e)


# ---------------------------------------------------------------------------
# /info  and  /formats
# ---------------------------------------------------------------------------

@app.get("/info", tags=["Video"], summary="Video metadata")
async def video_info(request: Request, url: str = Query(...)):
    key_info = require_valid_key(request)
    try:
        data = await get_video_info(url)
        _lib_save(data, source="info")            # song library me save
        _log(key_info, "/info", url)
        return {"success": True, "plan": key_info["plan"], "data": data}
    except Exception as e:
        _log(key_info, "/info", url, success=False)
        raise _map_error(e)


@app.get("/formats", tags=["Video"], summary="All available formats")
async def video_formats(request: Request, url: str = Query(...)):
    key_info = require_valid_key(request)
    try:
        formats = await get_available_formats(url)
        _log(key_info, "/formats", url)
        return {"success": True, "count": len(formats), "formats": formats}
    except Exception as e:
        _log(key_info, "/formats", url, success=False)
        raise _map_error(e)


# ---------------------------------------------------------------------------
# /file/<id> — stored MP3 ko HTTP stream karke do (music bots ke liye)
# 💾 LOCAL-DISK FIRST: VPS disk pe cached file hai to TG roundtrip skip —
# FileResponse zero-proxy latency. Kind param: audio (default) / video.
# ---------------------------------------------------------------------------


async def _serve_tg_file(request: Request, video_id: str, row: tuple,
                         kind: str = "audio"):
    """TG-archive (ya local-disk) file ko stream karke do.
    relay self-heal + prearchive-await paths isse call karte hain."""
    if not row or not row[0]:
        raise HTTPException(404, {"error": "no_file",
                                  "message": "Is song ki file store nahi hai."})
    file_id, title = row[0], row[1]

    # 💾 TIER-1: VPS local disk — TG se FASTER (no roundtrip)
    lp = local_cache_path(video_id, kind)
    if lp:
        from pathlib import Path
        ext = "mp4" if kind == "video" else "mp3"
        mt = "video/mp4" if kind == "video" else "audio/mpeg"
        from urllib.parse import quote as _q
        _ascii = re.sub(r'[^A-Za-z0-9 _.-]', '', (title or '')[:48]).strip() \
            or video_id
        _utf8 = _q((title or video_id)[:80])
        _cd = (f"inline; filename=\"{_ascii}.{ext}\"; "
               f"filename*=UTF-8''{_utf8}.{ext}")
        return FileResponse(Path(lp), media_type=mt,
                            headers={"Content-Disposition": _cd})

    import asyncio as _aio
    import tgstore

    def _fetch_stream():
        async def _inner():
            # worker thread me koi loop nahi hota — fresh Bot + fresh loop
            from telegram import Bot as TGBot
            import os as _os
            bot = TGBot(_os.getenv("TELEGRAM_BOT_TOKEN", ""))
            await bot.initialize()
            try:
                tfile = await bot.get_file(file_id)
                return tfile.file_path   # URL ya local path
            finally:
                try:
                    await bot.shutdown()
                except Exception:
                    pass
        # sync context (thread) — fresh loop banao, event_loop pe depend mat karo
        return _aio.run(_inner())

    try:
        # 🛡 _fetch_stream me asyncio.run() hai — isko WORKER THREAD me chalao
        # (async endpoint ke running-loop se direct call → 'cannot be called
        # from a running event loop' 502 aata tha)
        loop = asyncio.get_running_loop()
        file_path = await loop.run_in_executor(None, _fetch_stream)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, {"error": "tg_fetch_fail",
                                  "message": str(e)[:120]})

    import httpx
    try:
        if file_path.startswith("http"):
            upstream = httpx.stream("GET", file_path, timeout=60,
                                    follow_redirects=True)
        else:
            from pathlib import Path
            mt = "video/mp4" if kind == "video" else "audio/mpeg"
            ext = "mp4" if kind == "video" else "mp3"
            return FileResponse(Path(file_path), media_type=mt,
                                filename=f"{title or video_id}.{ext}")
    except Exception as e:
        raise HTTPException(502, {"error": "tg_fetch_fail",
                                  "message": str(e)[:120]})

    def _iter():
        with upstream as resp:
            for chunk in resp.iter_bytes(64 * 1024):
                yield chunk

    # 🛡 UNICODE-SAFE header: title ke non-latin1 chars (–, हिंदी) pe
    # purana header 500 deta tha ('latin-1' codec can't encode)
    from urllib.parse import quote as _q
    ext = "mp4" if kind == "video" else "mp3"
    mt = "video/mp4" if kind == "video" else "audio/mpeg"
    _ascii = re.sub(r'[^A-Za-z0-9 _.-]', '', (title or '')[:48]).strip() \
        or video_id
    _utf8 = _q((title or video_id)[:80])
    _cd = (f"inline; filename=\"{_ascii}.{ext}\"; "
           f"filename*=UTF-8''{_utf8}.{ext}")

    return StreamingResponse(
        _iter(), media_type=mt,
        headers={"Content-Disposition": _cd},
    )


@app.get("/file/{video_id}", tags=["Library"], summary="Stream stored file (local-disk-first)")
async def serve_stored_file(video_id: str, request: Request,
                            kind: str = Query("audio", description="'audio' or 'video'")):
    key_info = require_valid_key(request)
    if kind not in ("audio", "video"):
        kind = "audio"
    row = db.tg_file_get(video_id, kind=kind)
    # 🎬 AUDIO-FALLBACK GUARD: video manga par video nahi → 404 (audio
    # SERVE MAT karo — vplay me VIDEO ki jagah AUDIO baj jata tha!)
    if not row or not row[0]:
        # 💾 disk-cache hit ho sakta hai bina TG-store ke (SIRF manga kind)
        lp = local_cache_path(video_id, kind)
        if lp:
            from pathlib import Path
            mt = "video/mp4" if kind == "video" else "audio/mpeg"
            return FileResponse(Path(lp), media_type=mt)
        raise HTTPException(404, {"error": "no_file",
                                  "message": ("Is video ki MP4 store nahi hai." if kind == "video"
                                              else "Is song ki MP3 store nahi hai.")})
    return await _serve_tg_file(request, video_id, row, kind)



# ---------------------------------------------------------------------------
# 🔴 LIVE endpoints — m3u8/HLS ko ffmpeg pipe karo (VC live stream)
# Bot live URLs ko relay/download path se nahi bhej sakta — ffmpeg ko
# 7-day googlevideo HLS playlist chahiye hoti hai. Ye endpoints URL ko
# permanent key-embed karke proxy se pipe karte hain.
# ---------------------------------------------------------------------------

@app.get("/live/{video_id}", tags=["Stream"], summary="Live HLS playlist (key-embedded, proxy-piped)")
async def live_playlist(request: Request, video_id: str):
    require_valid_key(request)
    r = _relay_get(video_id)
    if not r:
        raise HTTPException(404, {"error": "live_relay_not_found",
                                  "message": "Naya /api/live request karo."})
    from urllib.parse import quote as _q
    _hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    key = _extract_key(request)
    sep = "&" if "?" in r["url"] else "?"
    upstream_url = f"{r['url']}{sep}api_key={key}"
    proxy = r.get("proxy")
    if proxy:
        _hdrs["proxy"] = f"http://{proxy}"
    try:
        upstream = await _http_client(proxy).get(r["url"], headers=_hdrs)
        body = upstream.text
    except Exception as e:
        raise HTTPException(502, {"error": "live_upstream_fail", "message": str(e)[:120]})
    from urllib.parse import urlparse as _up
    base = r["url"].rsplit("/", 1)[0]
    out_lines = []
    for line in body.splitlines():
        ls = line.strip()
        if not ls:
            out_lines.append("")
            continue
        if ls.startswith("#"):
            out_lines.append(line)
            continue
        # segment/variant URLs → hamare /lseg/ endpoint pe rewrite (key embedded)
        if ls.startswith("http"):
            seg_url = ls
        else:
            seg_url = f"{base}/{ls}"
        out_lines.append(f"/lseg/{video_id}?u={_q(seg_url, safe='')}")
    return Response(
        content="\n".join(out_lines) + "\n",
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/lseg/{video_id}", tags=["Stream"], summary="Live segment pipe (key-embedded)")
async def live_segment(request: Request, video_id: str, u: str):
    require_valid_key(request)
    r = _relay_get(video_id)
    proxy = (r or {}).get("proxy")
    _hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if proxy:
        _hdrs["proxy"] = f"http://{proxy}"
    try:
        upstream = await _http_client(proxy).stream("GET", u, headers=_hdrs)
        resp = await upstream.__aenter__()
        if resp.status_code >= 400:
            await upstream.__aexit__(None, None, None)
            raise HTTPException(502, {"error": "live_seg_fail",
                                      "message": f"upstream {resp.status_code}"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, {"error": "live_seg_fail", "message": str(e)[:120]})

    async def _seg_iter():
        try:
            async for chunk in resp.aiter_bytes(256 * 1024):
                yield chunk
        finally:
            await upstream.__aexit__(None, None, None)

    ct = resp.headers.get("content-type", "video/mp2t")
    return StreamingResponse(_seg_iter(), media_type=ct,
                             headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# /api/live  — 🔴 LIVE stream support (m3u8/HLS → ffmpeg-able URL)
# ---------------------------------------------------------------------------

@app.get("/api/live", tags=["Stream"], summary="LIVE stream URL (HLS via ffmpeg)")
async def api_live(request: Request, url: str = Query(...)):
    key_info = require_valid_key(request)
    try:
        fmt = "best[protocol*=m3u8]/bestaudio/best"
        resolved = await _resolve_url(url)
        info, result = await _fetch_info_and_dl(resolved, fmt)
        _log(key_info, "/api/live", url)
        stream_url = result.get("direct_url") or ""
        vid = info.get("id") or (resolved.split("v=")[-1].split("&")[0])
        is_hls = ("m3u8" in stream_url) or (result.get("is_live", False))
        if is_hls and get_proxy_pool():
            # relay entry banao taaki /live/{vid} playlist serve kar sake
            from ytdlp import (_relay_set_keep as _rs, _extract_race as _xr,
                               _direct_url_from_info as _dui)
            dl_fmt = "best[protocol*=m3u8]/bestaudio/best"
            try:
                _live_info = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _xr(resolved, dl_fmt, _alive_proxies()))
                _du = _dui(_live_info, allow_dash=True)   # HLS live OK
                if _du:
                    _rs(vid, resolved, dl_fmt, _du, "", False,
                        info.get("title") or vid)
            except Exception:
                pass
        base_url = APP_URL.rstrip("/") if APP_URL else "http://localhost:8000"
        return {
            "ok": True,
            "type": "live",
            "plan": key_info["plan"],
            "url": resolved,
            "title": info.get("title") or "",
            "is_live": True,
            "stream_url": _embed_key(stream_url, request),
            "direct_url": _embed_key(stream_url, request),
            "hls_url": _embed_key(f"{base_url}/live/{vid}", request),
            "thumbnail": info.get("thumbnail") or "",
        }
    except Exception as e:
        _log(key_info, "/api/live", url, success=False)
        raise _map_error(e)



# vid → "video"|"audio" — relay expire hone ke BAAD bhi pata ho ki request
# video thi ya audio (relay-404 fallback galat kind serve na de)
_RELAY_KIND_HINT: dict = {}


@app.get("/relay/{video_id}", tags=["Stream"], summary="Live relay stream (googlevideo pipe)")
async def relay_stream(request: Request, video_id: str, rng: Optional[str] = None,
                       refresh: int = Query(0, description="1 = relay URL re-extract (bot probe-fail retry)")):
    """Extraction ke turant baad live stream — bot ko instant URL.
    🐕 SLOW-UPSTREAM WATCHDOG: 256KB >12s me aaye (proxy-pipe ~10KB/s) to
    BG me direct re-extract karke relay-update — agli request fast hogi.
    🔁 ?refresh=1: relay purana/slow hai to FRESH extract + naya URL (bot
    full-download ki jagah ye call karega — 95-600s download trap khatam)."""
    require_valid_key(request)
    # 🎬 KIND-AWARE: request ka kind _RELAY_KIND_HINT se pata karo —
    # relay-map me audio+video ALAG copies reh sakti hain, galat kind
    # serve nahi hogi (vplay me audio bajne ka root-cause yahi tha)
    _hint = _RELAY_KIND_HINT.get(video_id)
    _want_v_hint = (_hint == "video") if _hint else (refresh == 0 and False)
    if refresh and not _relay_get(video_id):
        # refresh manga par relay-gayaab — archive/prearchive path chalega
        pass
    elif refresh:
        # 🔁 FRESH EXTRACT: naya URL (aur fresh proxy) — cache bypass
        try:
            from ytdlp import (_extract_race as _xr,
                               _direct_url_from_info as _dui,
                               _alive_proxies as _ap,
                               _relay_set_keep as _rs)
            _old = _relay_get(video_id)
            _want_v = bool((_old or {}).get("video")) if _old else _want_v_hint
            _dl = ("best[vcodec!=none][acodec!=none]/bestvideo+bestaudio/best"
                   if _want_v else "bestaudio[ext=m4a]/bestaudio/best")
            _al = await asyncio.get_event_loop().run_in_executor(None, _ap)
            if _al:
                _info, _px = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _xr((_old or {}).get("orig") or
                                      f"https://www.youtube.com/watch?v={video_id}",
                                      _dl, _al))
                _du = _dui(_info, allow_dash=not _want_v)
                if _du:
                    _rs(video_id, (_old or {}).get("orig") or
                        f"https://www.youtube.com/watch?v={video_id}",
                        _dl, _du, _px or "", _want_v,
                        (_old or {}).get("title") or video_id)
                    logger.info(f"🔁 relay refresh: {video_id} via {_px or 'direct'}")
        except Exception as e:
            logger.warning(f"relay refresh fail {video_id}: {str(e)[:100]} — old relay use hoga")
    from ytdlp import _relay_get_any as _rga
    r = _rga(video_id, _want_v_hint)
    if r:
        _RELAY_KIND_HINT[video_id] = "video" if r.get("video") else "audio"
    if not r:
        # 💾 RELAY-EXPIRE FALLBACK — 🎬 AUDIO-FALLBACK GUARD:
        # relay-gone hone par bhi SIRF request-ki-kind hi serve karo —
        # video-relay expire → audio serve kar diya to vplay me VIDEO ki
        # jagah AUDIO baj jata tha! (bot /file/?kind= par pura bharosa karta hai)
        _last_r = _RELAY_KIND_HINT.get(video_id)
        if _last_r:
            _kinds = ("video",) if _last_r == "video" else ("audio",)
        else:
            _kinds = ("video", "audio") if refresh else ("audio",)
        for _k in _kinds:
            _row = None
            try:
                from database import tg_file_get as _tfg
                _row = _tfg(video_id, kind=_k)
            except Exception:
                pass
            if _row and _row[0]:
                return await _serve_tg_file(request, video_id, _row, _k)
            _lp = local_cache_path(video_id, _k)
            if _lp:
                from pathlib import Path
                mt = "video/mp4" if _k == "video" else "audio/mpeg"
                return FileResponse(Path(_lp), media_type=mt)
        # 🔥 PRE-ARCHIVE-AWAIT: prefetch abhi race kar raha hoga
        if not _tg_file_stored(video_id, kind="audio"):
            try:
                await asyncio.wait_for(
                    _prearchive(
                        f"https://www.youtube.com/watch?v={video_id}"),
                    timeout=float(os.getenv("PREARCHIVE_WAIT", "18")))
            except Exception:
                pass
            if _tg_file_stored(video_id, kind="audio"):
                from database import tg_file_get as _tfg
                _row = _tfg(video_id, kind="audio")
                if _row and _row[0]:
                    return await _serve_tg_file(request, video_id, _row,
                                                "audio")
        raise HTTPException(404, {"error": "relay_not_found",
                                  "message": "Relay expired — naya /api/stream request karo."})
    want_video = bool(r.get("video"))
    title = (r.get("title") or video_id)[:48]
    _safe = re.sub(r'[^A-Za-z0-9_-]', '_', title)

    # 🎬 VIDEO PROGRESSIVE GUARD: relay URL DASH-only ho to VC me video
    # black-screen/frozen aata hai — archive video (sahi progressive mp4)
    # hai to wahi serve karo; nahi to clear 426 (bot refresh/archive try karega)
    if want_video and not _tg_file_stored(video_id, kind="video"):
        try:
            _u = r.get("url") or ""
            if _u and "googlevideo" in _u and "itag=18" not in _u \
                    and "/videoplayback" in _u:
                import httpx as _hx
                with _hx.Client(timeout=6) as _hc:
                    _hd = _hc.head(_u)
                _ct = (_hd.headers.get("content-type") or "").lower()
                if "webm" in _ct or "video/webm" in _ct:
                    from database import tg_file_get as _tfg
                    _vrow = _tfg(video_id, kind="video")
                    if _vrow and _vrow[0]:
                        logger.info(f"🎬 relay DASH-video → archive serve {video_id}")
                        return await _serve_tg_file(request, video_id, _vrow,
                                                    "video")
                    # BG me video archive fill karwao (agli baar instant)
                    from ytdlp import _bg_fill as _bgf
                    import threading as _th
                    _th.Thread(target=_bgf,
                               args=(video_id, r.get("orig"),
                                     "best[vcodec!=none][acodec!=none]/bestvideo+bestaudio/best",
                                     r.get("proxy") or "", True),
                               daemon=True).start()
        except Exception:
            pass

    _hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if not rng:
        rng = request.headers.get("range")   # 🛡 Range-header resume (bot/curl)
    if rng:
        _hdrs["Range"] = rng
    try:
        cm = _http_client(r.get("proxy")).stream("GET", r["url"], headers=_hdrs)
        resp = await cm.__aenter__()
        if resp.status_code in (403, 410):
            await cm.__aexit__(None, None, None)
            # 🔁 SELF-HEAL: googlevideo URL expire — FRESH race (cache bypass,
            # warna cached payload ka wahi dead URL wapas aata hai)
            try:
                from ytdlp import (_extract_race as _xr,
                                   _direct_url_from_info as _dui,
                                   _alive_proxies as _ap,
                                   _relay_set_keep as _rs)
                _dl = ("best[vcodec!=none][acodec!=none]/bestvideo+bestaudio/best"
                       if want_video else "bestaudio[ext=m4a]/bestaudio/best")
                _al = await asyncio.get_event_loop().run_in_executor(None, _ap)
                if _al:
                    _info, _px = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: _xr(r["orig"], _dl, _al))
                _du = _dui(_info, allow_dash=not want_video)
                if _du:
                    _rs(video_id, r["orig"], _dl, _du, _px or "",
                        want_video, r.get("title") or video_id)
            except Exception:
                pass
            r2 = _relay_get(video_id)
            if r2 and r2["url"] != r["url"]:
                _h2 = dict(_hdrs)
                cm2 = _http_client(r2.get("proxy")).stream("GET", r2["url"], headers=_h2)
                resp2 = await cm2.__aenter__()
                if resp2.status_code < 400:
                    r, cm, resp = r2, cm2, resp2
                else:
                    await cm2.__aexit__(None, None, None)
                    raise HTTPException(502, {"error": "relay_url_dead"})
            else:
                from database import tg_file_get
                _row = tg_file_get(video_id, kind="video" if want_video else "audio")
                if _row and _row[0]:
                    return await _serve_tg_file(request, video_id, _row,
                                                "video" if want_video else "audio")
                raise HTTPException(502, {"error": "relay_url_dead"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, {"error": "relay_fail", "message": str(e)[:120]})

    out = {
        "Content-Disposition": f'inline; filename="{_safe}' + ('.mp4"' if want_video else '.mp3"'),
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    for h in ("content-length", "content-range"):
        if h in resp.headers:
            out[h] = resp.headers[h]

    async def _fresh_race(px_old: str):
        """Naya URL + NAYA proxy (race) — same-proxy resume fail hone par."""
        from ytdlp import (_extract_race as _xr,
                           _direct_url_from_info as _dui,
                           _alive_proxies as _ap,
                           _relay_set_keep as _rs)
        _al = await asyncio.get_event_loop().run_in_executor(None, _ap)
        _excl = [p for p in _al if p != (px_old or "")]
        if not _excl:
            return None
        _dl = ("best[vcodec!=none][acodec!=none]/bestvideo+bestaudio/best"
               if want_video else "bestaudio[ext=m4a]/bestaudio/best")
        _info, _px = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _xr(r["orig"], _dl, _excl))
        _du = _dui(_info, allow_dash=not want_video)
        if not _du:
            return None
        _rs(video_id, r["orig"], _dl, _du, _px or "", want_video,
            r.get("title") or video_id)
        return _du, (_px or "")

    async def _iter():
        # 🛡 MID-STREAM AUTO-RESUME: upstream (googlevideo/proxy) beech me mare
        # to Range se wahi offset se reconnect — bot ko katna pata nahi chalega
        # (pehle 502 / Truncated-download aata tha).
        nonlocal cm, resp, r
        _sent = 0
        _t0 = asyncio.get_event_loop().time()
        _sw = {"f": False}
        try:
            _want = int(resp.headers.get("content-length", "")) or None
        except ValueError:
            _want = None
        _retries = 0
        try:
            while True:
                _cut = False
                try:
                    async for chunk in resp.aiter_bytes(256 * 1024):
                        yield chunk
                        _sent += len(chunk)
                        if not _sw["f"] and 262144 <= _sent < 400000:
                            _dt = asyncio.get_event_loop().time() - _t0
                            if _dt > 12.0:
                                _sw["f"] = True
                                logger.warning(
                                    f"relay slow-pipe {_sent//1024}KB/"
                                    f"{_dt:.0f}s - direct re-extract BG "
                                    f"{video_id}")

                                async def _reex():
                                    try:
                                        from ytdlp import (
                                            get_stream_data_with_fallback)
                                        _rf = ("bestvideo+bestaudio/best"
                                               if r.get("video") else None)
                                        await get_stream_data_with_fallback(
                                            r["orig"], _rf)
                                    except Exception:
                                        pass
                                asyncio.get_event_loop().create_task(_reex())
                except Exception:
                    # TransportError / TaskGroup(ExceptionGroup) / httpcore —
                    # upstream ne connection kata — resume baki hai?
                    if _retries >= 4 or (_want and _sent >= _want):
                        raise
                    _cut = True
                if (not _cut) or (_want and _sent >= _want):
                    break
                _retries += 1
                logger.warning(
                    f"relay mid-stream cut @{_sent}B → resume "
                    f"#{_retries} {video_id}")
                await asyncio.sleep(0.4)
                _ok = False
                if _sent > 0:
                    # TIER-1: same URL+proxy, Range se resume
                    _h2 = dict(_hdrs)
                    _h2["Range"] = f"bytes={_sent}-"
                    try:
                        _cm = _http_client(r.get("proxy")).stream(
                            "GET", r["url"], headers=_h2)
                        _r2 = await _cm.__aenter__()
                        if _r2.status_code == 206:
                            try:
                                await cm.__aexit__(None, None, None)
                            except Exception:
                                pass
                            cm, resp = _cm, _r2
                            _ok = True
                            try:
                                _want = _sent + int(_r2.headers.get(
                                    "content-length", "0"))
                            except ValueError:
                                pass
                        else:
                            await _cm.__aexit__(None, None, None)
                    except Exception:
                        pass
                if not _ok:
                    # TIER-2: FRESH race — naya URL + NAYA proxy
                    try:
                        _fr = await _fresh_race(r.get("proxy"))
                        if _fr:
                            _du, _px2 = _fr
                            _h2 = dict(_hdrs)
                            if _sent > 0:
                                _h2["Range"] = f"bytes={_sent}-"
                            _cm = _http_client(_px2).stream(
                                "GET", _du, headers=_h2)
                            _r2 = await _cm.__aenter__()
                            if _r2.status_code in (200, 206):
                                try:
                                    await cm.__aexit__(None, None, None)
                                except Exception:
                                    pass
                                r = {**r, "url": _du, "proxy": _px2}
                                cm, resp = _cm, _r2
                                _ok = True
                                try:
                                    _want = _sent + int(_r2.headers.get(
                                        "content-length", "0"))
                                except ValueError:
                                    pass
                            else:
                                await _cm.__aexit__(None, None, None)
                    except Exception:
                        pass
                if not _ok:
                    raise _httpx.TransportError("relay resume failed")
        finally:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass

    return StreamingResponse(_iter(), headers=out)


@app.post("/admin/archive-songs", tags=["Admin"])
def admin_archive_songs(request: Request, limit: int = Query(5, ge=1, le=20)):
    """Library ke top songs jinki MP3 store nahi — store karne laga (non-blocking
    subset, ek call me `limit` songs). Loop chalane ke liye baar-baar hit karo."""
    require_admin(request)
    import asyncio as _aio
    import tgstore
    top  = db.library_top(50)
    todo = [s for s in top if not (db.tg_file_get(s["video_id"]) or (None,))[0]]
    batch = todo[:limit]
    done = failed = 0
    for s in batch:
        vid = s["video_id"]
        try:
            # stream URL lo (cache/ TG/ fresh — get_stream_data sab layers use karta hai)
            # sync thread me hai — fresh loop per call (asyncio.run)
            info, result = _aio.run(
                get_stream_data(f"https://www.youtube.com/watch?v={vid}",
                                "bestaudio/best"))
            durl = result.get("direct_url") or ""
            if not durl:
                failed += 1
                continue
            ok = _aio.run(
                tgstore.tg_store_file(vid, info.get("title") or s.get("title", ""),
                                      durl, result.get("ext") or ""))
            done += 1 if ok else 0
            failed += 0 if ok else 1
            import time as _t
            _t.sleep(2)   # Telegram flood-safe
        except Exception as e:
            logger.warning(f"archive {vid} fail: {e}")
            failed += 1
    return {"ok": True, "archived": done, "failed": failed,
            "remaining": max(0, len(todo) - len(batch)),
            "note": "repeat call karo jab tak remaining 0 na ho jaye"}


# ---------------------------------------------------------------------------
# /library — PUBLIC: shared song database (kya popular hai)
# ---------------------------------------------------------------------------

@app.get("/library/top", tags=["Library"], summary="Most played songs")
def library_top_endpoint(
    request: Request,
    limit: int = Query(10, ge=1, le=50),
):
    require_valid_key(request)
    return {"ok": True, "songs": db.library_top(limit)}


@app.get("/library/recent", tags=["Library"], summary="Recently played")
def library_recent_endpoint(
    request: Request,
    limit: int = Query(10, ge=1, le=50),
):
    require_valid_key(request)
    return {"ok": True, "songs": db.library_recent(limit)}


@app.get("/library/search", tags=["Library"], summary="Search your saved songs")
def library_search_endpoint(
    request: Request,
    q:     str = Query(..., description="Song name / uploader"),
    limit: int = Query(10, ge=1, le=50),
):
    require_valid_key(request)
    songs = db.library_search(q, limit)
    return {"ok": True, "query": q, "count": len(songs), "songs": songs}


@app.get("/library/stats", tags=["Library"], summary="Library stats")
def library_stats_endpoint(request: Request):
    require_valid_key(request)
    return {"ok": True, **db.library_stats()}


# ---------------------------------------------------------------------------
# /my/stats
# ---------------------------------------------------------------------------

@app.get("/my/stats", tags=["Account"], summary="Your usage stats")
def my_stats(request: Request):
    key_info = require_valid_key(request)
    row      = key_info.get("row") or db.validate_key(key_info["key"])
    stats    = db.get_usage_stats(row["user_id"])
    plan_cfg = db.PLANS.get(key_info["plan"], db.PLANS["free"])
    rpd      = plan_cfg["rpd"]
    days     = db.days_remaining_by_key(key_info["key"])
    return {
        "plan":            key_info["plan"],
        "plan_price":      plan_cfg["price"],
        "daily_limit":     rpd if rpd != -1 else "unlimited",
        "rpm_limit":       plan_cfg["rpm"],
        "used_today":      stats["today"],
        "remaining_today": max(0, rpd - stats["today"]) if rpd != -1 else "unlimited",
        "total_requests":  stats["total"],
        "total_audio":     stats["total_audio"],
        "total_video":     stats["total_video"],
        "last_used":       stats["last_used"],
        "key_expires_in":  f"{days} days",
        "upgrade_url":     BOT_URL,
    }


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@app.get("/admin/stats", tags=["Admin"], summary="Global stats")
def admin_stats(request: Request):
    require_admin(request)
    return db.get_admin_stats()


@app.get("/admin/proxy-status", tags=["Admin"])
def proxy_status(request: Request):
    require_admin(request)
    pool   = get_proxy_pool()
    masked = [p.split("@")[-1][:40] for p in pool]
    return {
        "proxy_count":   len(pool),
        "proxies":       masked,
        "proxy_health":  get_proxy_health(),
        "circuit":       get_circuit_state(),
        "hourly_budget": get_hourly_budget(),
        "cookie_health": get_cookie_health(),
        "stream_cache":  stream_cache_stats(),
        "negative_cache": negative_cache_stats(),
    }


@app.post("/admin/proxy-reload", tags=["Admin"])
def proxy_reload(request: Request):
    require_admin(request)
    return {"success": True, "proxy_count": reload_proxies()}


@app.get("/admin/cache-stats", tags=["Admin"])
def admin_cache_stats(request: Request):
    require_admin(request)
    ram  = cache_stats()
    db_c = get_video_cache_stats()
    import tgstore
    return {
        "ram_cache":       ram,
        "db_video_cache":  db_c,
        "db_stream_cache": get_stream_cache_stats(),
        "tg_store":        tgstore.tg_status(),
        "tg_files":        tgstore.tg_file_status(),
    }


@app.get("/admin/tg-store-status", tags=["Admin"])
def admin_tg_store_status(request: Request):
    """Telegram DB channel ka live status (index stats + channel reachability)."""
    require_admin(request)
    import tgstore
    status = tgstore.tg_status()
    if status.get("enabled"):
        try:
            bot = asyncio.get_event_loop().run_until_complete(
                tgstore._get_bot())
            me = asyncio.get_event_loop().run_until_complete(
                bot.get_me(timeout=10))
            status["bot_username"] = me.username
        except Exception as e:
            status["bot_check"] = f"fail: {str(e)[:60]}"
    return status


@app.post("/admin/tg-store-recover", tags=["Admin"])
def admin_tg_store_recover(request: Request):
    """Naya VPS pe call karo — channel history se pura cache wapas recover."""
    require_admin(request)
    import tgstore
    result = asyncio.get_event_loop().run_until_complete(
        tgstore.recover_from_channel())
    return result


@app.post("/admin/cache-clear", tags=["Admin"])
def admin_cache_clear(
    request: Request,
    target: str = Query("all", description="'ram' | 'db' | 'all'"),
    older_than_days: int = Query(None, description="Only clear DB entries older than N days"),
):
    require_admin(request)
    cleared = []
    if target in ("ram", "all"):
        cache_clear()
        cleared.append("ram")
    if target in ("db", "all"):
        clear_video_cache(older_than_days)
        cleared.append("db")
    return {"success": True, "cleared": cleared}


@app.get("/admin/security", tags=["Admin"], summary="Brute-force & IP block status")
def admin_security(request: Request):
    require_admin(request)
    stats = get_bf_stats()
    return {
        "active_ip_blocks":   stats["active_blocks"],
        "pending_ips":        stats["pending_attempts"],
        "config": {
            "max_attempts":   db.BF_MAX_ATTEMPTS,
            "window_seconds": db.BF_WINDOW_SEC,
            "block_seconds":  db.BF_BLOCK_SEC,
        },
    }


@app.post("/admin/unblock-ip", tags=["Admin"], summary="Manually unblock an IP")
def admin_unblock_ip(request: Request, ip: str = Query(...)):
    require_admin(request)
    reset_attempts(ip)
    return {"success": True, "message": f"IP {ip} unblocked."}


@app.get("/admin/update-status", tags=["Admin"], summary="yt-dlp update + OAuth + auto-restart status")
def admin_update_status(request: Request):
    require_admin(request)
    import autoupdate
    return {"success": True, **autoupdate.get_update_status()}


@app.post("/admin/update-now", tags=["Admin"], summary="Force yt-dlp update (+ restart if updated)")
def admin_update_now(request: Request):
    require_admin(request)
    import autoupdate
    return {"success": True, **autoupdate.force_update_and_restart()}


@app.post("/admin/restart-services", tags=["Admin"], summary="Restart API + bot services (fresh IP state)")
def admin_restart_services(request: Request):
    require_admin(request)
    import autoupdate
    autoupdate._do_restart("admin manual restart")
    return {"success": True,
            "message": "Restart command bheja gaya (systemd: 2-5s me services wapas).",
            "command": autoupdate.RESTART_CMD}


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def _global_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "internal_error", "message": str(exc)},
    )


# ---------------------------------------------------------------------------
# Internal helper — fetch info + download url in parallel using cache
# ---------------------------------------------------------------------------

async def _fetch_info_and_dl(url: str, fmt: str):
    """Fetch metadata and stream URL with one coalesced yt-dlp extraction."""
    return await get_stream_data_with_fallback(url, format_id=fmt)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False,
                workers=int(os.getenv("WORKERS", "1")))
