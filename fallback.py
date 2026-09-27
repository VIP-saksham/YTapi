"""
fallback.py
-----------
HellAPI — Invidious/Piped fallback resolver.

Kab use hota hai: yt-dlp extraction IP-block/bot-check se fail ho jaye
(VPS IP flagged — cookies ke bina yahi hota hai). Tab community instances
se stream URL resolve karte hain:

  - Invidious API:  /api/v1/videos/{id}  → itag → /latest_version?itag=X&local=true
    (local=true = instance ke through proxied — googlevideo IP-bound hota hai)
  - Piped API:      /streams/{id}        → audioStreams[].url (proxied)

Health-aware: jo instance last pass hua wo pehle; dead instances cooldown pe.

Env:
  FALLBACK_ENABLED = 1 (default)
  FB_INSTANCE_TIMEOUT = 8 (seconds per instance)
"""

import os
import time
import json
import logging
import threading
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

FALLBACK_ENABLED = os.getenv("FALLBACK_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
INSTANCE_TIMEOUT = int(os.getenv("FB_INSTANCE_TIMEOUT", "8"))

# Invidious instances — sirf LIVE tested (2026-09-19 health scan ka result).
# invidious.f5.si = PASS (video API 200). Baaki sab 403/dead nikle — hata diye.
INVIDIOUS_INSTANCES = [
    "https://invidious.f5.si",
]
# Piped API instances
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://api.piped.private.coffee",
    "https://pipedapi.reallyaweso.me",
    "https://pipedapi.adminforge.de",
]

_lock = threading.Lock()
_health: Dict[str, dict] = {}          # base_url → {"ok": n, "fail": n, "cooldown_until": ts}
_last_used: Optional[str] = None


def _healthy(base: str) -> bool:
    rec = _health.get(base)
    return not rec or rec.get("cooldown_until", 0) <= time.time()


def _record(base: str, ok: bool):
    with _lock:
        rec = _health.setdefault(base, {"ok": 0, "fail": 0, "cooldown_until": 0})
        if ok:
            rec["ok"] += 1
            rec["cooldown_until"] = 0
        else:
            rec["fail"] += 1
            # fail pe 10 min cooldown (exponential nahi — instance jaldi recover hota hai)
            rec["cooldown_until"] = time.time() + 600
        global _last_used
        if ok:
            _last_used = base


def _ordered(bases: list) -> list:
    """Last-used pehle, healthy only."""
    with _lock:
        health = {k: dict(v) for k, v in _health.items()}
        last = _last_used
    out = [b for b in bases if _healthy(b)]
    if last in out:
        out.remove(last)
        out.insert(0, last)
    return out or bases[:1]      # sab cooldown me ho to pehla try karo hi


def _http_json(url: str, timeout: int = INSTANCE_TIMEOUT):
    import httpx
    r = httpx.get(url, timeout=timeout, follow_redirects=True, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json",
    })
    r.raise_for_status()
    return r.json()


def _verify_stream_url(url: str) -> bool:
    """
    Resolve ke baad QUICK download probe (64KB range GET).
    Instance API 200 de sakta hai lekin proxied download HTML/Anubis
    challenge de raha ho — aisa URL client ko dena hi mat.
    """
    import httpx
    try:
        with httpx.Client(timeout=8, follow_redirects=True) as c:
            with c.stream("GET", url, headers={"Range": "bytes=0-65535"}) as r:
                if r.status_code not in (200, 206):
                    return False
                ct = r.headers.get("content-type", "")
                if "html" in ct or "text/" in ct:
                    return False
                got = 0
                for chunk in r.iter_bytes(65536):
                    got += len(chunk)
                    if got >= 32768:
                        break
                return got >= 8192      # kam se kam 8KB asli data
    except Exception:
        return False


def _from_invidious(base: str, vid: str, prefer_audio: bool = True) -> Optional[dict]:
    """Invidious se metadata + proxied audio URL."""
    data = _http_json(f"{base}/api/v1/videos/{vid}")
    title = data.get("title") or ""
    duration = data.get("lengthSeconds")
    live = bool(data.get("liveNow"))

    fmts = data.get("adaptiveFormats") or []
    audio = [f for f in fmts
             if str(f.get("type", "")).startswith("audio/")
             and f.get("itag")]
    audio.sort(key=lambda f: int(f.get("bitrate") or 0), reverse=True)
    itag = audio[0]["itag"] if audio else None

    if itag and not live:
        # proxied URL — instance ka IP googlevideo se stream karega
        url = f"{base}/latest_version?id={vid}&itag={itag}&local=true"
        if not _verify_stream_url(url):
            logger.info(f"invidious {base}: verify FAIL (HTML/error) → {vid}")
            return None                    # broken instance — tier pe chhodo
        ext = "m4a" if "mp4" in str(audio[0].get("type", "")) else "webm"
        return {
            "title": title,
            "format_id": f"inv-{itag}",
            "ext": ext,
            "resolution": "audio",
            "direct_url": url,
            "is_live": False,
            "duration": duration,
            "via": f"invidious:{base.split('//')[1]}",
        }
    return None


def _from_piped(base: str, vid: str) -> Optional[dict]:
    """Piped API se proxied audio URL."""
    data = _http_json(f"{base}/streams/{vid}")
    title = data.get("title") or ""
    streams = data.get("audioStreams") or []
    streams.sort(key=lambda s: int(s.get("bitrate") or 0), reverse=True)
    if not streams:
        return None
    best = streams[0]
    url = best.get("url")
    if not url:
        return None
    if not _verify_stream_url(url):
        logger.info(f"piped {base}: verify FAIL → download broken")
        return None
    mime = str(best.get("mimeType", ""))
    return {
        "title": title,
        "format_id": f"piped-{best.get('itag', 'x')}",
        "ext": "m4a" if "mp4" in mime else "webm",
        "resolution": "audio",
        "direct_url": url,
        "is_live": bool(data.get("livestream")),
        "duration": data.get("duration"),
        "via": f"piped:{base.split('//')[1]}",
    }


def resolve_audio_sync(video_id: str) -> Optional[dict]:
    """⚡ PARALLEL: saare instances ek saath hit — pehli jeet turant.
    (Pehle serial tha: ek dead instance hi 8s kha jata tha.)"""
    if not FALLBACK_ENABLED:
        return None
    vid = (video_id or "").strip()
    if len(vid) != 11:
        vid = vid.split("v=")[-1].split("&")[0]

    from concurrent.futures import ThreadPoolExecutor, wait as _fwait

    tasks = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for base in _ordered(INVIDIOUS_INSTANCES):
            tasks[ex.submit(_from_invidious, base, vid)] = ("invidious", base)
        for base in _ordered(PIPED_INSTANCES):
            tasks[ex.submit(_from_piped, base, vid)] = ("piped", base)
        if not tasks:
            return None
        done, pending = _fwait(tasks, timeout=INSTANCE_TIMEOUT + 4)
        for fut in pending:
            fut.cancel()
        # ✅ healthy-jeeta-hua instance pehle (deterministic pick)
        results = []
        for fut in done:
            kind, base = tasks[fut]
            try:
                res = fut.result()
            except Exception:
                _record(base, False)
                continue
            if res:
                _record(base, True)
                results.append(res)
            else:
                _record(base, False)
        if not results:
            logger.warning(f"fallback: sab instances fail → {vid}")
            return None
        results.sort(key=lambda r: 0 if str(r.get("via", "")).startswith("invidious") else 1)
        logger.info(f"fallback HIT {results[0].get('via')} → {vid}")
        return results[0]


async def resolve_audio(video_id: str) -> Optional[dict]:
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, resolve_audio_sync, video_id)


def get_status() -> dict:
    with _lock:
        health = {k: dict(v) for k, v in _health.items()}
        last = _last_used
    now = time.time()
    return {
        "enabled": FALLBACK_ENABLED,
        "instances": {
            b.split("//")[1]: {
                "ok": h.get("ok", 0),
                "fail": h.get("fail", 0),
                "cooldown_min": max(0, round((h.get("cooldown_until", 0) - now) / 60, 1)),
            } for b, h in health.items()
        },
        "last_used": last,
    }
