"""
ytdlp.py
--------
Async-ready yt-dlp wrapper with:
  - Smart stream cache — googlevideo URL ki apni expiry tak (repeat = instant)
  - In-memory TTL cache (8 min) + SQLite metadata cache (7 din)
  - Negative cache — deleted/private videos 10 min instant-fail
  - Adaptive client ordering — jo client jeeta use pehle
  - Health-aware proxy rotation + circuit breaker + hourly budget
  - Cookie health monitor — stale cookies ki warning
  - PO Token + visitor data + JS runtime support
"""

import os
import re
import random
import time
import logging
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List, Dict, Any

import yt_dlp
from cachetools import TTLCache
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

# ── Logger ──────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ── Thread pool for blocking yt-dlp calls ───────────────────────────────────
_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(os.getenv("YTDLP_WORKERS", "4")),
    thread_name_prefix="ytdlp",
)

# ── In-memory cache (url+format → result, TTL = 8 min) ──────────────────────
_CACHE_TTL  = int(os.getenv("YTDLP_CACHE_TTL", "480"))   # seconds
_CACHE_SIZE = int(os.getenv("YTDLP_CACHE_SIZE", "256"))   # max entries
_cache      = TTLCache(maxsize=_CACHE_SIZE, ttl=_CACHE_TTL)
_cache_lock = threading.Lock()

# Share identical concurrent extractions instead of hitting YouTube repeatedly.
_INFLIGHT: Dict[str, asyncio.Future] = {}
_inflight_lock = threading.Lock()

# ── Smart stream cache (googlevideo expiry-aware) ────────────────────────────
# googlevideo URLs me `expire=<unix_ts>` hota hai (~6 hrs). TTLCache ki fixed
# TTL ke bajaye URL ki apni expiry tak cache karte hain — 30s safety margin.
# Result: repeat request = INSTANT, zero YouTube hits.
_STREAM_TTL_CAP_MIN = int(os.getenv("YTDLP_STREAM_TTL_CAP_MIN", "300"))  # max 5h
_stream_lock = threading.Lock()
_STREAM: Dict[str, tuple] = {}   # key → (payload, expires_at_ts)


def _parse_gv_expiry(direct_url: str) -> float:
    """googlevideo URL se expire timestamp parse karo; fail → cap TTL."""
    try:
        m = re.search(r"[?&]expire=(\d+)", direct_url or "")
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return time.time() + _STREAM_TTL_CAP_MIN * 60


def _stream_cache_get(key: str):
    with _stream_lock:
        item = _STREAM.get(key)
        if not item:
            return None
        payload, exp = item
        if exp <= time.time() + 30:          # 30s safety margin
            _STREAM.pop(key, None)
            return None
        return payload


def _stream_cache_set(key: str, payload, expires_at: float):
    with _stream_lock:
        # cache size guard — 2048 entries se zyada to sabse jald expire wali hatao
        if len(_STREAM) >= 2048:
            oldest = min(_STREAM, key=lambda k: _STREAM[k][1])
            _STREAM.pop(oldest, None)
        _STREAM[key] = (payload, expires_at)


def stream_cache_stats() -> dict:
    now = time.time()
    with _stream_lock:
        live = sum(1 for _, exp in _STREAM.values() if exp > now)
        next_exp = min((exp for _, exp in _STREAM.values()), default=0)
    return {
        "entries":           len(_STREAM),
        "live":              live,
        "next_expiry_in_sec": max(0, int(next_exp - now)) if next_exp else 0,
    }


# ── Negative cache — unavailable videos 10 min instant-fail ────────────────
# Deleted/private videos baar-baar mangwana = wasted YT hits + slow errors.
NEG_TTL_SEC = int(os.getenv("YTDLP_NEG_TTL_SEC", "600"))
_neg_lock = threading.Lock()
_NEG: Dict[str, float] = {}   # normalized url → until_ts


def _neg_key(url: str) -> str:
    return (url or "").strip().lower()


def negative_cache_get(url: str) -> bool:
    with _neg_lock:
        until = _NEG.get(_neg_key(url))
        if until and until > time.time():
            return True
        if until:
            _NEG.pop(_neg_key(url), None)
    return False


def negative_cache_set(url: str, ttl: int = NEG_TTL_SEC):
    with _neg_lock:
        _NEG[_neg_key(url)] = time.time() + ttl


def negative_cache_clear(url: str):
    """False-negative hatao (jaise geo-blocked proxy ne 'unavailable' bola)."""
    with _neg_lock:
        _NEG.pop(_neg_key(url), None)


_neg_probe_at: Dict[str, float] = {}
_neg_probe_lock = threading.Lock()


def _neg_probe_ok(url: str) -> bool:
    """Negative-cached video ko proxy se zinda karne ki koshish
    har 10 min me sirf ek baar — deleted videos slow na hon."""
    now = time.time()
    with _neg_probe_lock:
        last = _neg_probe_at.get(_neg_key(url), 0)
        if now - last < 600:
            return False
        _neg_probe_at[_neg_key(url)] = now
        return True


def negative_cache_stats() -> dict:
    now = time.time()
    with _neg_lock:
        active = sum(1 for v in _NEG.values() if v > now)
    return {"active": active, "ttl_sec": NEG_TTL_SEC}


# ── Cookie health monitor ────────────────────────────────────────────────────
# Stale/rotated cookies = bot-check errors. File age dekh ke warn karo.
COOKIE_STALE_DAYS = int(os.getenv("YTDLP_COOKIE_STALE_DAYS", "14"))


def get_cookie_health() -> dict:
    path = _cookie_file()
    if not path:
        return {"configured": False}
    if not os.path.isfile(path):
        return {"configured": True, "exists": False,
                "warning": f"cookie file not found: {path}"}
    age_days = (time.time() - os.path.getmtime(path)) / 86400
    healthy = age_days <= COOKIE_STALE_DAYS
    return {
        "configured":        True,
        "exists":            True,
        "source":            os.path.basename(path),
        "age_days":          round(age_days, 1),
        "stale_limit_days":  COOKIE_STALE_DAYS,
        "healthy":           healthy,
        "warning":           None if healthy else
                             f"cookies {int(age_days)} din purani — incognito se fresh export karo (cookies/youtube.txt replace karo)",
    }


def _cache_get(key: str):
    with _cache_lock:
        return _cache.get(key)


def _cache_set(key: str, value):
    with _cache_lock:
        _cache[key] = value


def cache_stats() -> dict:
    with _cache_lock:
        return {"size": len(_cache), "maxsize": _CACHE_SIZE, "ttl": _CACHE_TTL}


def cache_clear():
    with _cache_lock:
        _cache.clear()


# ── User-agent pool ──────────────────────────────────────────────────────────
try:
    from fake_useragent import UserAgent as _FUA
    _fua = _FUA()
    def _random_ua() -> str:
        try:
            return _fua.random
        except Exception:
            return _random_ua_static()
except Exception:
    _fua = None
    def _random_ua() -> str:
        return _random_ua_static()

_STATIC_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.122 Mobile Safari/537.36",
]

def _random_ua_static() -> str:
    return random.choice(_STATIC_UAS)


# ── Proxy pool (health-aware rotation — dead/cooling proxies auto-skip) ────
_proxy_lock  = threading.Lock()
_proxy_index = 0

# Fail hone pe kitne second ke liye wo proxy cooldown par jayega.
# Blocked proxy ke liye exponential: 60s → 120s → 240s ...
PROXY_FAIL_COOLDOWN = int(os.getenv("PROXY_FAIL_COOLDOWN_SEC", "60"))


# Proxy pool module-load se pehle .env zaroor load ho (import-order safe)
try:
    from dotenv import load_dotenv as _ldotenv
    _ldotenv()
except Exception:
    pass


def _load_proxies() -> List[str]:
    raw = os.getenv("PROXY_LIST", "").strip()
    if not raw:
        return []
    proxies = [p.strip() for p in raw.split(",") if p.strip()]
    logger.info(f"Loaded {len(proxies)} proxies.")
    return proxies


_PROXY_POOL: List[str] = _load_proxies()

# proxy → {"fails": int, "cooldown_until": float, "last_used": float, "ok": int}
_proxy_health: Dict[str, dict] = {}


def _ph_new() -> dict:
    return {"fails": 0, "cooldown_until": 0.0, "last_used": 0.0, "ok": 0}


def reload_proxies() -> int:
    global _PROXY_POOL, _proxy_index
    with _proxy_lock:
        _PROXY_POOL = _load_proxies()
        _proxy_index = 0
        _proxy_health.clear()          # fresh start after reload
    return len(_PROXY_POOL)


def get_proxy_pool() -> List[str]:
    with _proxy_lock:
        return list(_PROXY_POOL)


def _get_proxy() -> Optional[str]:
    """Health-aware selection: pehle healthy proxies, cooling-down wale avoid."""
    global _proxy_index
    with _proxy_lock:
        if not _PROXY_POOL:
            return None
        now = time.time()
        healthy = [p for p in _PROXY_POOL
                   if _proxy_health.get(p, {}).get("cooldown_until", 0) <= now]
        if healthy:
            proxy = healthy[_proxy_index % len(healthy)]
            _proxy_index += 1
        else:
            # Sab cooldown par — jiska cooldown sabse jald khatam hoga wala try karo
            proxy = min(_PROXY_POOL,
                        key=lambda p: _proxy_health.get(p, {}).get("cooldown_until", 0))
        rec = _proxy_health.setdefault(proxy, _ph_new())
        rec["last_used"] = now
        return proxy


def mark_proxy_result(proxy: Optional[str], ok: bool, blocked: bool = False):
    """Extraction ke baad proxy ki health record karo."""
    if not proxy:
        return
    with _proxy_lock:
        rec = _proxy_health.setdefault(proxy, _ph_new())
        now = time.time()
        rec["last_used"] = now
        if ok:
            rec["ok"] += 1
            rec["fails"] = 0
            rec["cooldown_until"] = 0.0
        else:
            rec["fails"] += 1
            mult = 2 ** min(rec["fails"] - 1, 5)        # 1x, 2x, 4x, 8x ... (max 32x)
            rec["cooldown_until"] = now + PROXY_FAIL_COOLDOWN * mult
            if blocked:
                logger.warning(f"Proxy BLOCKED by YouTube, cooldown {PROXY_FAIL_COOLDOWN * mult}s: {proxy.split('@')[-1][:30]}")


def get_proxy_health() -> List[dict]:
    """Admin: per-proxy health (credentials masked)."""
    with _proxy_lock:
        now = time.time()
        return [{
            "proxy":             p.split("@")[-1][:40],
            "healthy":           _proxy_health.get(p, {}).get("cooldown_until", 0) <= now,
            "consecutive_fails": _proxy_health.get(p, {}).get("fails", 0),
            "cooldown_left_sec": max(0, int(_proxy_health.get(p, {}).get("cooldown_until", 0) - now)),
            "successes":         _proxy_health.get(p, {}).get("ok", 0),
        } for p in _PROXY_POOL]


# ── Direct-extraction dead switch (IP flagged → fallback-first fast path) ──
_direct_dead_until = 0.0
_direct_dead_lock = threading.Lock()


def mark_direct_dead(minutes: int = 30):
    global _direct_dead_until
    with _direct_dead_lock:
        _direct_dead_until = time.time() + minutes * 60
    logger.warning(f"⚡ direct extraction DEAD for {minutes}m — fallback-first mode ON")


def is_direct_dead() -> bool:
    with _direct_dead_lock:
        return time.time() < _direct_dead_until


def mark_direct_alive():
    global _direct_dead_until
    with _direct_dead_lock:
        _direct_dead_until = 0.0


# IP-flag error pe pehle client ke baad ruk jao — baaki clients bhi waise hi fail
# honge (block IP-level hai, client-level nahi). Fail-fast = seconds bachte hain.
_FAIL_FAST_IPBLOCK = os.getenv("YTDLP_FAIL_FAST_IPBLOCK", "1").strip().lower() in (
    "1", "true", "yes", "on")


# ── Adaptive client ordering ────────────────────────────────────────────────
# Default client hamesha pehla. Fallbacks me: jo last pass hua wo aage,
# jis se baar-baar fail ho raha wo piche — kam wasted YouTube hits.
_client_lock = threading.Lock()
_client_fails: Dict[str, int] = {}
_last_good_client: Optional[list] = None


def _record_client(client: Optional[list], ok: bool):
    global _last_good_client
    if client is None:
        return
    name = client[0]
    with _client_lock:
        if ok:
            _client_fails[name] = 0
            _last_good_client = client
        else:
            _client_fails[name] = _client_fails.get(name, 0) + 1


def _ordered_clients() -> List[Optional[list]]:
    with _client_lock:
        lg    = _last_good_client
        fails = dict(_client_fails)
    fallbacks = [c for c in _YT_CLIENTS if c is not None]
    if lg in fallbacks:
        fallbacks.remove(lg)
        fallbacks.insert(0, lg)             # proven winner sabse pehle
    # chronic failures (3+) stable-sort se sabse piche
    fallbacks.sort(key=lambda c: fails.get(c[0], 0) >= 3)
    return [None] + fallbacks


# ── YouTube circuit breaker (server-IP protection) ──────────────────────────
# Bina proxy ke saare requests server ke IP se jaate hain. Jab YouTube ne IP
# block kar diya ho, baar-baar hammer karna block aur lamba kar deta hai.
# Isliye: threshold tak consecutive blocks → circuit open → fail-fast with
# Retry-After → cooldown ke baad ek probe request se recovery check.
CB_THRESHOLD    = int(os.getenv("YT_CB_THRESHOLD", "3"))
CB_COOLDOWN_SEC = int(os.getenv("YT_CB_COOLDOWN_SEC", "120"))

_cb_lock        = threading.Lock()
_cb_consec      = 0          # consecutive IP-block errors
_cb_open_until  = 0.0        # 0 = circuit closed
_cb_last_reason = ""


def get_circuit_state() -> dict:
    with _cb_lock:
        now = time.time()
        return {
            "open":                now < _cb_open_until,
            "retry_after_sec":     max(0, int(_cb_open_until - now)),
            "consecutive_blocks":  _cb_consec,
            "threshold":           CB_THRESHOLD,
            "cooldown_sec":        CB_COOLDOWN_SEC,
            "last_reason":         _cb_last_reason,
        }


def _cb_check():
    """Circuit open ho to YouTube ko bilkul hit mat karo — turant fail karo."""
    st = get_circuit_state()
    if st["open"]:
        raise IPBlockedError(
            f"circuit_open: YouTube ne server IP temporarily limit kiya hai. "
            f"{st['retry_after_sec']}s me retry karo. Last error: {st['last_reason']}"
        )


def _cb_record_success():
    global _cb_consec, _cb_open_until
    with _cb_lock:
        _cb_consec     = 0
        _cb_open_until = 0.0


def _cb_record_block(reason: str):
    global _cb_consec, _cb_open_until, _cb_last_reason
    with _cb_lock:
        _cb_consec     += 1
        _cb_last_reason = (reason or "")[:160]
        if _cb_consec >= CB_THRESHOLD:
            _cb_open_until = time.time() + CB_COOLDOWN_SEC
            logger.warning(
                f"⚡ Circuit OPEN {CB_COOLDOWN_SEC}s ke liye — "
                f"{_cb_consec} consecutive YouTube blocks. Server IP ko rest do."
            )
            # IP-block watchdog — bar-bar block ho raha hai to services restart
            # (fresh connections + naya IP side-state; proxy rotation bhi reset).
            try:
                import autoupdate
                autoupdate.notify_ip_block_event(reason)
            except Exception:
                pass


# ── Guest-session hourly budget limiter ──────────────────────────────────────
# YouTube wiki (official): guest session limit ≈ 300 videos/hour. Cross kiya to
# guest session throttle hota hai aur "This content isn't available, try again
# later" aata hai. Isliye sliding 1-hour window me kitne YouTube hits hue, wo
# count karte hain — limit ke paas aate hi fail-fast with Retry-After.
GUEST_HOURLY_LIMIT = int(os.getenv("YTDLP_HOURLY_LIMIT", "250"))   # 300 ke neeche raho

_hourly_lock   = threading.Lock()
_hourly_hits: List[float] = []


def get_hourly_budget() -> dict:
    """Admin/current usage: sliding 1-hour window me YouTube hits."""
    now = time.time()
    with _hourly_lock:
        while _hourly_hits and now - _hourly_hits[0] >= 3600:
            _hourly_hits.pop(0)
        used = len(_hourly_hits)
    return {
        "used_last_hour": used,
        "limit":          GUEST_HOURLY_LIMIT,
        "remaining":      max(0, GUEST_HOURLY_LIMIT - used),
    }


def _oauth_enabled() -> bool:
    return os.getenv("YTDLP_OAUTH", "").strip().lower() in ("1", "true", "yes", "on")


def _budget_check():
    # OAuth login = account session (~2000/hr limit), guest 250/hr nahi.
    # Isliye OAuth on hone par guest budget skip — fail-fast galat hogi.
    if _oauth_enabled():
        return
    st = get_hourly_budget()
    if st["remaining"] <= 0:
        raise IPBlockedError(
            f"hourly_budget_exhausted: guest session limit ({GUEST_HOURLY_LIMIT}/hr) "
            f"ke paas/ paar. {int(3600 - (time.time() - _hourly_hits[0]))}s me retry. "
            f"(YTDLP_HOURLY_LIMIT env se tune karo, ya YTDLP_OAUTH=1 lagao)"
        )


def _budget_record():
    now = time.time()
    with _hourly_lock:
        _hourly_hits.append(now)


# ── Sleep between calls ──────────────────────────────────────────────────────
_MIN_SLEEP = float(os.getenv("YTDLP_MIN_SLEEP", "0"))
_MAX_SLEEP = float(os.getenv("YTDLP_MAX_SLEEP", "0"))


def _sleep():
    if _MAX_SLEEP > 0:
        time.sleep(random.uniform(max(0, _MIN_SLEEP), _MAX_SLEEP))


# ── Custom exceptions ────────────────────────────────────────────────────────
class IPBlockedError(Exception):
    """YouTube IP block / 429 / bot-check."""

class ExtractionError(Exception):
    """Genuine yt-dlp extraction failure."""


# ── Error classifiers ────────────────────────────────────────────────────────
def _is_ip_block(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in [
        "429", "too many requests", "ip block", "blocked",
        "bot", "captcha", "nsig extraction failed",
        "sign in to confirm", "confirm you're not a bot",
        "http error 403", "forbidden",
        # IP-flagged server pe YouTube khali player response deta hai:
        "failed to extract any player response",
        "no title found in player responses",
    ])


def _is_unavailable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in [
        "video unavailable", "is unavailable", "private video",
        "has been removed", "no longer available",
        "does not exist", "account has been terminated",
        "copyright",
    ])


# ── Cookie auto-detect (🍪 LOGIN COOKIES) ────────────────────────────────
# YTDLP_COOKIE_FILE env → cookies/*.txt → cookies.txt (API folder me).
# Login cookies = bot-check/IP-block nahi aate = extraction 2-5x FAST.
# Har 60s re-check — fresh export bina restart pick ho jata hai.
_COOKIE_CACHE = {"path": None, "checked": 0.0}


def _cookie_file() -> Optional[str]:
    now = time.time()
    if _COOKIE_CACHE["path"] and now - _COOKIE_CACHE["checked"] < 60:
        return _COOKIE_CACHE["path"]
    _COOKIE_CACHE["checked"] = now
    path = None
    env = os.getenv("YTDLP_COOKIE_FILE", "").strip()
    if env and os.path.isfile(env):
        path = env
    else:
        import glob as _g
        base = os.path.dirname(os.path.abspath(__file__))
        for c in (sorted(_g.glob(os.path.join(base, "cookies", "*.txt")))
                  + [os.path.join(base, "cookies.txt")]):
            try:
                if os.path.isfile(c) and os.path.getsize(c) > 50:
                    path = c
                    break
            except Exception:
                continue
    _COOKIE_CACHE["path"] = path
    return path


# ── yt-dlp options builder ───────────────────────────────────────────────────
def _build_opts(client: Optional[list], extra: Optional[Dict] = None,
                proxy: Optional[str] = None) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet":          True,
        "no_warnings":    True,
        "skip_download":  True,
        "noplaylist":     True,
        "socket_timeout": 20,
        "retries":        3,
        "extractor_retries": 2,
        "nocheckcertificate": True,
        "geo_bypass":     True,
        "geo_bypass_country": "US",
    }

    if client:
        # Forced/fallback clients ke liye hi custom headers lagao.
        # Default client (None) ko yt-dlp ke apne headers chalne do —
        # random User-Agent / Referer web client ke bot-check ko trigger karte hain.
        ua = _random_ua()
        opts["http_headers"] = {
            "User-Agent":                ua,
            "Accept-Language":           "en-US,en;q=0.9",
            "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding":           "gzip, deflate, br",
            "Referer":                   "https://www.google.com/",
            "DNT":                       "1",
            "Cache-Control":             "max-age=0",
        }
        opts["extractor_args"] = {
            "youtube": {
                "player_client": client,
                "skip":          ["hls"] if client[0] not in ("tv_embedded",) else ["dash"],
            }
        }

    if proxy:
        opts["proxy"] = proxy

    # ── OAuth2 login (cookies ka modern replacement) ─────────────────────────
    # YTDLP_OAUTH=1 → yt-dlp ko 'oauth2' credentials do. Token ek baar
    # oauth_setup.py se login karke cache hota hai, phir auto-refresh hota hai.
    # Login = YouTube guest-session limit (250/hr) nahi lagti, IP-block risk kam.
    if os.getenv("YTDLP_OAUTH", "").strip().lower() in ("1", "true", "yes", "on"):
        opts["username"] = "oauth2"
        opts["password"] = os.getenv("YTDLP_OAUTH_PASSWORD", "")

    # 🍪 LOGIN COOKIES — DIRECT + PROXY DONO pe lagao (env-gated):
    # Sep-2026: server-IP restricted hone par proxy-akela bhi bot-checked —
    # PROXY+COOKIES combo hi video formats dega (616+251 confirmed).
    # IP-mismatch flag risk lekin UNPLAYABLE se better. Env: PROXY_COOKIES=0
    _pc = os.getenv("PROXY_COOKIES", "1").strip().lower() in ("1", "true", "yes", "on")
    cookie_file = _cookie_file()
    if cookie_file and (not proxy or _pc):
        opts["cookiefile"] = cookie_file

    # Free anti-block: Visitor Data (cookies ke bina Innertube requests personalize)
    # Usage: yt-dlp wiki — --extractor-args "youtube:player_skip=webpage,configs;visitor_data=..."
    visitor_data = os.getenv("YTDLP_VISITOR_DATA", "").strip()
    if visitor_data:
        ea = opts.setdefault("extractor_args", {}).setdefault("youtube", {})
        ea["visitor_data"] = [visitor_data]
        ea["player_skip"] = ["webpage", "configs"]

    # JS runtime (yt-dlp >= 2025.10): node/deno etc. — JS challenges solve karta hai.
    # Python API me format dict hai: {"node": {}} (empty config dict zaroori).
    # Example: YTDLP_JS_RUNTIME=node
    js_runtime = os.getenv("YTDLP_JS_RUNTIME", "").strip()
    if js_runtime:
        opts["js_runtimes"] = {js_runtime: {}}

    # 🔓 EJS remote-components (yt-dlp wiki: EJS) — n-challenge solver script
    # download karta hai. ISKE BINA VPS IP PE "page needs to be reloaded" /
    # "sign in to confirm you're not a bot" aata hai (Sep 2026 YouTube change).
    # CLI ka --remote-components ejs:github yahan YTDLP_REMOTE_COMPONENTS env se.
    _rc = os.getenv("YTDLP_REMOTE_COMPONENTS", "ejs:github").strip()
    if _rc and _rc.lower() not in ("0", "off", "false", "no"):
        opts["remote_components"] = [_rc]

    po_token = os.getenv("YTDLP_PO_TOKEN", "").strip()
    if po_token:
        opts.setdefault("extractor_args", {}).setdefault("youtube", {})["po_token"] = [f"web+{po_token}"]

    if extra:
        opts.update(extra)

    return opts


# ── Multi-client fallback ────────────────────────────────────────────────────
# None  = yt-dlp defaults (sabse reliable — forced clients aksar block ho jate hain)
# Baaki fallbacks sirf tab jab default fail ho.
_YT_CLIENTS = [
    None,               # yt-dlp default clients
    ["web_safari"],
    ["tv"],
    ["ios"],
    ["tv_embedded"],
    ["mweb"],
]


def _try_extract(url: str, extra_opts: Optional[Dict] = None) -> Dict[str, Any]:
    last_exc: Optional[Exception] = None

    _cb_check()      # circuit open hai to YouTube ko hit hi mat karo
    _budget_check()  # hourly guest budget khatam to bhi mat karo

    for client in _ordered_clients():   # adaptive: proven client pehle
        proxy = _get_proxy()   # per-attempt — taaki fail pe penalize kar sakein
        try:
            _sleep()
            _budget_record()
            opts = _build_opts(client, extra_opts, proxy=proxy)
            logger.debug(f"Client {client} proxy={bool(proxy)} → {url[:60]}")
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info is None:
                raise ExtractionError("yt-dlp returned no data.")
            logger.debug(f"OK with client {client}")
            _record_client(client, ok=True)
            _cb_record_success()
            mark_proxy_result(proxy, ok=True)
            return info
        except yt_dlp.utils.DownloadError as e:
            _record_client(client, ok=False)
            if _is_unavailable(e):
                raise ExtractionError(str(e)) from e
            if _is_ip_block(e):
                mark_proxy_result(proxy, ok=False, blocked=True)
                if _FAIL_FAST_IPBLOCK:
                    last_exc = e
                    break      # IP-flag: baaki clients futile — turant fallback pe jao
                time.sleep(random.uniform(2.0, 4.0))
            else:
                mark_proxy_result(proxy, ok=False)
            last_exc = e
        except Exception as e:
            _record_client(client, ok=False)
            if _is_unavailable(e):
                raise ExtractionError(str(e)) from e
            if _is_ip_block(e) and _FAIL_FAST_IPBLOCK:
                mark_proxy_result(proxy, ok=False, blocked=True)
                last_exc = e
                break
            mark_proxy_result(proxy, ok=False)
            last_exc = e

    if last_exc and _is_ip_block(last_exc):
        _cb_record_block(str(last_exc))
        raise IPBlockedError(str(last_exc)) from last_exc
    raise ExtractionError(str(last_exc or "All clients failed."))


def _tg_file_stored(vid: str, kind: str = "audio") -> bool:
    """Kya ye video TG file-archive (audio/video) me hai?"""
    try:
        from database import tg_file_get
        row = tg_file_get(vid, kind=kind)
        return bool(row and row[0])
    except Exception:
        return False


def _tg_file_id(vid: str) -> Optional[str]:
    """Archive me stored MP3 ka file_id (ya None)."""
    try:
        from database import tg_file_get
        row = tg_file_get(vid)
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _archive_payload(vid: str, kind: str = "audio") -> tuple:
    """
    📼 ARCHIVE-FIRST: HellAPI ka apna song-archive (TG channel MP3s)
    permanent source hai — ek baar store hua gaana HAMESHA chalega,
    YouTube ki reputation se independent.
    """
    base = (os.getenv("APP_URL", "") or "http://localhost:8000").rstrip("/")
    title, dur, thumb, up = vid, None, f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "HellAPI archive"
    size_mb = 0.0
    try:
        from database import library_get, get_cached_video_metadata, tg_file_get
        lib = library_get(vid) or {}
        vc = get_cached_video_metadata(f"https://www.youtube.com/watch?v={vid}") or {}
        title = lib.get("title") or vc.get("title") or title
        dur = lib.get("duration_sec") or vc.get("duration_seconds")
        thumb = lib.get("thumbnail") or vc.get("thumbnail") or thumb
        up = lib.get("uploader") or vc.get("uploader") or up
        tf = tg_file_get(vid, kind=kind)
        if tf and tf[1]:
            title = title if title != vid else tf[1]
        if tf and tf[2]:
            size_mb = float(tf[2])
    except Exception:
        pass
    info_result = {
        "id":               vid,
        "title":            title,
        "uploader":         up,
        "channel_url":      None,
        "duration_seconds": dur,
        "view_count":       None,
        "like_count":       None,
        "upload_date":      None,
        "thumbnail":        thumb,
        "description":      "",
        "webpage_url":      f"https://www.youtube.com/watch?v={vid}",
        "is_live":          False,
        "tags":             [],
        "via_fallback":     True,
    }
    dl_result = {
        "title":      title,
        "format_id":  "tgfile",
        "ext":        "mp4" if kind == "video" else "mp3",
        "resolution": "video" if kind == "video" else "audio",
        "direct_url": f"{base}/file/{vid}" + ("?kind=video" if kind == "video" else ""),
        # 📏 exact stored size — bot purani corrupt/adhuri cache auto-heal karta hai
        "filesize":   int(size_mb * 1024 * 1024) if size_mb else None,
        "is_live":    False,
        "via_fallback": True,
    }
    return info_result, dl_result


async def _fb_archive(info_result: dict, dl_result: dict, vid: str) -> bool:
    """
    Fallback result ko TG song-archive me store karo. Bool return —
    fail hua to caller video-archive se serve karega, raw URL kabhi nahi.
    """
    import tgstore
    if not tgstore.is_enabled():
        return False
    du = dl_result.get("direct_url") or ""
    if "latest_version" in du or "invidious." in du or "piped" in du:
        return False   # instance URLs broken/unreliable hain — archive me skip
    ext = dl_result.get("ext") or "mp3"
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _EXECUTOR,
            lambda: asyncio.run(tgstore.tg_store_file(
                vid, dl_result.get("title") or vid, du,
                ext=ext, local_file=dl_result.get("local_file") or "")),
        )
    except Exception as e:
        logger.debug(f"fb archive fail: {e}")
        return False


def _fb_result_to_payload(info: dict, dl: dict, vid: str) -> tuple:
    """fallback.py ka result → (info_result, dl_result) HellAPI format."""
    info_result = {
        "id":               vid,
        "title":            dl.get("title") or "",
        "uploader":         (info or {}).get("uploader") or (dl.get("via") or "fallback"),
        "channel_url":      None,
        "duration_seconds": dl.get("duration"),
        "view_count":       None,
        "like_count":       None,
        "upload_date":      None,
        "thumbnail":        f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "description":      "",
        "webpage_url":      f"https://www.youtube.com/watch?v={vid}",
        "is_live":          bool(dl.get("is_live")),
        "tags":             [],
        "via_fallback":     True,
    }
    dl_result = {
        "title":      dl.get("title") or "",
        "format_id":  dl.get("format_id"),
        "ext":        dl.get("ext"),
        "resolution": dl.get("resolution"),
        "direct_url": dl.get("direct_url"),
        "is_live":    bool(dl.get("is_live")),
        "via_fallback": True,
        "local_file": dl.get("local_file") or "",
    }
    return info_result, dl_result


def _pc_video_url(info: dict) -> Optional[str]:
    """Progressive MP4 (video+audio) URL nikaalo — archive upload ke liye."""
    try:
        fmts = info.get("formats") or []
        best = None
        for f in fmts:
            if f.get("vcodec") not in (None, "none") and \
               f.get("acodec") not in (None, "none") and \
               f.get("protocol", "https") == "https" and f.get("url"):
                if best is None or (f.get("height") or 0) > (best.get("height") or 0):
                    best = f
        return (best or {}).get("url")
    except Exception:
        return None


async def _pc_vurl(vid: str, url: str, info: dict) -> Optional[str]:
    """Progressive URL scan + (nahi mila to) itag-18 dedicated re-extract."""
    vurl = _pc_video_url(info)
    if vurl:
        return vurl
    # kai videos me progressive format pehle extraction me nahi aata —
    # itag 18 (360p mp4, video+audio) ke saath ek aur extraction try karo
    try:
        loop = asyncio.get_running_loop()
        info2 = await loop.run_in_executor(
            _EXECUTOR,
            lambda: _extract_sync(
                url, {"format": "best[vcodec!=none][acodec!=none]/bestvideo+bestaudio/best"}),
        )
        return _pc_video_url(info2)
    except Exception as e:
        logger.debug(f"itag-18 re-extract fail {vid}: {e}")
        return None


def _proxy_alive(proxy: str, timeout: float = 4.0) -> bool:
    """Quick 4s check — dead proxy pe extraction ka time waste na ho."""
    try:
        import httpx
        with httpx.Client(proxy=f"http://{proxy}", timeout=timeout) as c:
            r = c.head("https://www.youtube.com")
            return r.status_code < 500
    except Exception:
        return False


def _download_to_file(vid: str, url: str, mode: str) -> tuple:
    """
    🎬 yt-dlp DOWNLOADER — proxy ke saath final file banao.
    Multi-proxy retry: har attempt naya proxy (dead pe 403 na aaye).
    mode: 'video' → mp4 (merge), returns (path, tmpdir) ya (None, None).
    """
    import tempfile
    import glob as _g
    for p_attempt in range(3):
        tmpdir = tempfile.mkdtemp(prefix="hellapi_dl_")
        outtmpl = os.path.join(tmpdir, f"{vid}.%(ext)s")
        # video: progressive → bestvideo+bestaudio merge (ffmpeg) → best
        fmt = ("best[vcodec!=none][acodec!=none]/bestvideo+bestaudio/best"
               if mode == "video" else "bestaudio/best")
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": False,
            "format": fmt, "outtmpl": outtmpl, "noplaylist": True,
            "socket_timeout": 25, "retries": 1, "nocheckcertificate": True,
            "merge_output_format": "mp4",
        }
        proxy = _get_proxy()
        if proxy:
            opts["proxy"] = proxy
        else:
            # direct (server-IP) download → cookies lagao (bot-check nahi)
            _cf = _cookie_file()
            if _cf:
                opts["cookiefile"] = _cf
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = ydl.prepare_filename(info)
                if not os.path.exists(path):
                    cands = _g.glob(os.path.join(tmpdir, f"{vid}.*"))
                    path = cands[0] if cands else ""
                if path and os.path.exists(path) and os.path.getsize(path) > 100_000:
                    mark_proxy_result(proxy, ok=True)
                    return path, tmpdir
                mark_proxy_result(proxy, ok=False)
        except Exception as e:
            mark_proxy_result(proxy, ok=False)
            logger.debug(f"yt-dlp download fail {vid} {mode} (p{p_attempt + 1}): {e}")
        import shutil as _sh
        _sh.rmtree(tmpdir, ignore_errors=True)
    return None, None


async def _pc_video(vid: str, info: dict, info_result: dict,
                    url: str = ""):
    """Video file (progressive mp4) TG archive me store — background thread."""
    import tgstore
    try:
        from database import tg_file_get
        if tg_file_get(vid, kind="video"):
            return True
    except Exception:
        pass
    vurl = await _pc_vurl(vid, url or f"https://www.youtube.com/watch?v={vid}", info)
    if not vurl:
        # 🎬 PROGRESSIVE nahi mila → yt-dlp downloader se file banao
        loop = asyncio.get_running_loop()
        path, tdir = await loop.run_in_executor(
            _EXECUTOR,
            lambda: _download_to_file(
                vid, url or f"https://www.youtube.com/watch?v={vid}", "video"),
        )
        if not path:
            return False
        try:
            return await tgstore.tg_store_file(
                vid, info_result.get("title") or vid, "",
                ext="mp4", kind="video", local_file=path,
            )
        finally:
            import shutil as _sh
            _sh.rmtree(tdir, ignore_errors=True)
    height = 0
    try:
        fmts = info.get("formats") or []
        for f in fmts:
            if f.get("url") == vurl:
                height = f.get("height") or 0
                break
    except Exception:
        pass
    ext = "mp4" if (info.get("ext") == "mp4" or height) else "mp4"
    try:
        return await tgstore.tg_store_file(
            vid, info_result.get("title") or vid, vurl,
            ext=ext, kind="video",
        )
    except Exception as e:
        logger.debug(f"video archive fail {vid}: {e}")
        return False


# ── ⚡ RELAY MAP — proxy-signed URLs ka instant stream ────────────────
_RELAYS: Dict[str, dict] = {}
_RELAYS_LOCK = threading.Lock()
_INFLIGHT_STREAMS: Dict[str, object] = {}
_INFLIGHT_SLOCK = threading.Lock()
_alive_cache: dict = {"ts": 0.0, "list": []}
_ALIVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_alive_proxies.json")


def _alive_cache_boot():
    """🔥 RESTART-WARM: pichhli baar ke zinda proxies disk se load —
    restart ke baad pehli request bhi bina health-check-wait ke fast."""
    try:
        import json as _json
        st = os.stat(_ALIVE_FILE)
        if time.time() - st.st_mtime < 7200:   # 2h tak valid
            data = _json.load(open(_ALIVE_FILE))
            if data:
                _alive_cache["ts"] = st.st_mtime
                _alive_cache["list"] = list(data)
    except Exception:
        pass


_alive_cache_boot()


_ALIVE_TTL = int(os.getenv("PROXY_ALIVE_TTL_SEC", "120"))


# ── 💾 VPS LOCAL-DISK CACHE (song files disk pe — /file/ pehle serve kare) ──
# /file/ TG round-trip (get_file URL + proxy stream) ki jagah seedha disk se
# FileResponse — bot VC start me 1-3s ki bachat + TG downtime se azadi.
# Size-cap: default 3GB, sabse purani (mtime) file auto-evict. Kind-aware
# naming: audio=m4a/mp3, video=mp4 — same vid dono kind cache ho sakta hai.
_LOCAL_CACHE_DIR = os.getenv("HELLAPI_LOCAL_CACHE",
                             "/root/hellapi/downloads")
_LOCAL_CACHE_MAX_MB = int(os.getenv("HELLAPI_LOCAL_CACHE_MAX_MB", "3072"))


def local_cache_path(vid: str, kind: str = "audio") -> Optional[str]:
    """Local cached file ka path (exist + sane size) ya None."""
    try:
        exts = ("mp4",) if kind == "video" else ("m4a", "mp3", "opus", "webm")
        for ext in exts:
            p = os.path.join(_LOCAL_CACHE_DIR, f"{vid}.{ext}")
            if os.path.isfile(p) and os.path.getsize(p) > 100_000:
                return p
    except Exception:
        pass
    return None


def _local_cache_evict():
    """Cache-cap cross → sabse purani files delete (best-effort, bg-safe)."""
    try:
        files = []
        total = 0
        for fn in os.listdir(_LOCAL_CACHE_DIR):
            p = os.path.join(_LOCAL_CACHE_DIR, fn)
            try:
                if os.path.isfile(p):
                    st = os.stat(p)
                    files.append((st.st_mtime, st.st_size, p))
                    total += st.st_size
            except Exception:
                continue
        cap = _LOCAL_CACHE_MAX_MB * 1024 * 1024
        if total <= cap:
            return
        files.sort()                              # sabse purane pehle
        for _m, size, p in files:
            if total <= cap * 0.90:               # 10% headroom tak hatao
                break
            try:
                os.remove(p)
                total -= size
            except Exception:
                break
        logger.info(f"💾 local-cache evict: now {total // 1048576}MB")
    except Exception:
        pass


def _alive_refresh_sync() -> list:
    """Poora pool health-check — SIRF bg-thread ya khali-cache se bulao."""
    import concurrent.futures as _cf
    pool_all = get_proxy_pool()
    if not pool_all:
        return []
    with _cf.ThreadPoolExecutor(max_workers=min(16, len(pool_all))) as ex:
        alive = [p for p, ok in zip(pool_all, ex.map(_proxy_alive, pool_all)) if ok]
    for _p in pool_all:
        if _p not in alive:
            mark_proxy_result(_p, ok=False)
    _alive_cache["ts"] = time.time()
    _alive_cache["list"] = alive
    try:
        import json as _json
        _json.dump(alive, open(_ALIVE_FILE, "w"))   # 🔥 disk-warm
    except Exception:
        pass
    return alive


def _alive_refresh_bg():
    try:
        _alive_refresh_sync()
    except Exception:
        pass


def _alive_proxies() -> list:
    """⚡ Zinda proxies — REQUEST-PATH PE KABHI HEALTH-CHECK-WAIT NAHI:
      fresh cache (<TTL) → turant
      stale cache → stale list TURANT + fresh check BACKGROUND me
      khali (restart/pehli baar) → tabhi ek sync check
    (pehle stale-cache wali request 4-15s health-check me atak jati thi!)"""
    now = time.time()
    cached = _alive_cache["list"]
    if cached and now - _alive_cache["ts"] < _ALIVE_TTL:
        return list(cached)
    if cached:
        import threading as _th
        _th.Thread(target=_alive_refresh_bg, daemon=True).start()
        return list(cached)
    return _alive_refresh_sync()


def _relay_set(vid: str, orig_url: str, dl_fmt: str, direct_url: str,
               proxy: Optional[str], want_video: bool, title: str = ""):
    with _RELAYS_LOCK:
        _RELAYS[vid] = {
            "url": direct_url, "orig": orig_url, "fmt": dl_fmt,
            "proxy": proxy, "video": want_video, "title": title,
            "ts": time.time(), "archiving": False,
        }


def _relay_get(vid: str) -> Optional[dict]:
    with _RELAYS_LOCK:
        r = _RELAYS.get(vid)
        if r and time.time() - r["ts"] < 1800:
            return r
        if r:
            _RELAYS.pop(vid, None)
    return None


# 🎬 KIND-AWARE RELAY GUARD: relay-map sirf vid se keyed hai — audio relay
# video relay ko OVERWRITE kar deta tha → vplay me VIDEO ki jagah AUDIO baj
# jata tha (ct:audio/mpeg video request pe!). Ye helpers kind check karte hain:


def _relay_get_kind(vid: str, want_video: bool) -> Optional[dict]:
    """Sirf WAHI kind ka relay do jo request manga hai (ya None)."""
    r = _relay_get(vid)
    if r and bool(r.get("video")) == bool(want_video):
        return r
    return None


def _relay_set_keep(vid: str, orig_url: str, dl_fmt: str, direct_url: str,
                    proxy: Optional[str], want_video: bool, title: str = ""):
    """Same-kind relay set, PAR doosri kind ka relay OVERWRITE MAT karo
    (audio+video dono parallel chal sakte hain — vplay+play saath)."""
    # existing doosri-kind relay preserve karo
    _other = _relay_get(vid)
    _RELAYS[vid] = {
        "url": direct_url, "orig": orig_url, "fmt": dl_fmt,
        "proxy": proxy, "video": want_video, "title": title,
        "ts": time.time(), "archiving": False,
    }
    if _other and bool(_other.get("video")) != bool(want_video):
        with _RELAYS_LOCK:
            _RELAYS[f"{vid}::{'video' if _other.get('video') else 'audio'}"] = _other


def _relay_get_any(vid: str, want_video: bool):
    """Pehle same-kind relay, phir kind-suffixed store se doosri copy."""
    r = _relay_get_kind(vid, want_video)
    if r:
        return r
    with _RELAYS_LOCK:
        r2 = _RELAYS.get(f"{vid}::{'video' if want_video else 'audio'}")
        if r2 and time.time() - r2["ts"] < 1800:
            return r2
    return None


def _extract_with_proxy(url: str, fmt: str, proxy: Optional[str]):
    opts = _build_opts(None, {"format": fmt},
                       proxy=f"http://{proxy}" if proxy else None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


RACE_TIMEOUT = float(os.getenv("PROXY_RACE_TIMEOUT", "14"))   # 8s me weak-proxies ke slow-but-working racers cut ho jate the

# 🍪 POOL KAMZOR (0-2 zinda) ho to direct+cookies ko PEHLE solo try karo —
# VPS me cookies fresh hain (2.5s proven) aur dead proxies 8s race-wait
# kara dete hain. Env: DIRECT_FIRST_WEAK_POOL=0 se band.
_DIRECT_FIRST_WEAK = os.getenv("DIRECT_FIRST_WEAK_POOL", "1").strip().lower() in (
    "1", "true", "yes", "on")


def _extract_race(url: str, fmt: str, proxies: list) -> tuple:
    """N proxies pe PARALLEL extraction — pehli safal wapas (info, proxy).
    🍪 COOKIES RACE: apna server-IP (login cookies ke saath) bhi SAATH me
    race karta hai — direct bhi jeet sakta hai (proxy latency bachi).
    ⏱ Deadline (PROXY_RACE_TIMEOUT, 8s): sab slow/dead ho to turant fail —
    fallback path jawab de deta hai.
    🚀 WEAK-POOL DIRECT-FIRST: zinda proxies <=2 ho to pehle SIRF direct+
    cookies try (2.5s me jeet) — fail tabhi proxy-race pe jao.
    🎯 PROVEN-FIRST: curl-pass ≠ extraction-pass (bot-check). PROVEN_PROXY
    env (comma-list) ke proxies race me SABSE PEHLE aate hain — proven
    header alphabetically-aage sorted dead racers ki wajah se kabhi race
    me aaye hi nahi the."""
    import concurrent.futures as _cf
    _proven = [p.strip() for p in os.getenv("PROVEN_PROXY", "").split(",") if p.strip()]
    _proven_set = set(_proven)
    _ordered = [p for p in _proven if p in set(proxies)] \
        + [p for p in proxies if p not in _proven_set]
    racers = _ordered[:4]
    _has_direct = any(not p for p in racers)   # None = direct racer pehle se
    if not _has_direct and _cookie_file():
        racers.append("")                       # direct+cookies racer jodo

    # 🚀 Weak pool + cookies → solo direct attempt pehle (fast, proven)
    if _DIRECT_FIRST_WEAK and _cookie_file() and len([p for p in racers if p]) <= 2:
        try:
            info = _extract_with_proxy(url, fmt, "")
            if info:
                logger.info(f"🚀 weak-pool direct+cookies jeeta ({len([p for p in racers if p])} alive proxies skip)")
                return info, ""
        except Exception as e:
            logger.debug(f"weak-pool direct-first miss: {str(e)[:80]}")

    ex = _cf.ThreadPoolExecutor(max_workers=min(5, len(racers)))
    futs = {ex.submit(_extract_with_proxy, url, fmt, p): p for p in racers}
    try:
        for fut in _cf.as_completed(futs, timeout=RACE_TIMEOUT):
            p = futs[fut]
            try:
                info = fut.result()
            except Exception:
                if p:
                    mark_proxy_result(p, ok=False)
                continue
            if info:
                if p:
                    mark_proxy_result(p, ok=True)
                return info, p
        raise IPBlockedError("race: sab racers fail")
    except _cf.TimeoutError:
        raise IPBlockedError(f"race deadline {RACE_TIMEOUT}s — racers slow/dead")
    finally:
        ex.shutdown(wait=False)


_BG_INFLIGHT: set = set()
_BG_INFLIGHT_LOCK = threading.Lock()


def _bg_fill(vid: str, orig_url: str, dl_fmt: str, proxy: str,
             want_video: bool):
    """Relay chalate waqt background archive-fill — agli request instant.
    🛡 CUT-PROOF: ek proxy ka download kata to FRESH proxy se dobara
    (max 3 attempts) — pehle 'received 1.5MB of 3.8MB' pe archive skip
    ho jata tha aur song kabhi store nahi hota.
    🔒 IN-FLIGHT GUARD: relay+prearchive+direct-path TIGON se ek hi video
    ke parallel downloads chalte the (double/triple download) — ab sirf
    pehla chalega, baaki turant skip."""
    _key = f"{vid}::{want_video}"
    with _BG_INFLIGHT_LOCK:
        if _key in _BG_INFLIGHT:
            logger.debug(f"bg fill DUPLICATE skip: {_key}")
            return
        _BG_INFLIGHT.add(_key)
    try:
        return _bg_fill_inner(vid, orig_url, dl_fmt, proxy, want_video)
    finally:
        with _BG_INFLIGHT_LOCK:
            _BG_INFLIGHT.discard(_key)


def _bg_fill_inner(vid: str, orig_url: str, dl_fmt: str, proxy: str,
                   want_video: bool):
    try:
        from database import tg_file_get
        kind = "video" if want_video else "audio"
        if tg_file_get(vid, kind=kind):
            return
        ok, _ = _fetch_store_one(vid, orig_url, dl_fmt, proxy or "", want_video)
        if not ok:
            # 🛡 winner-proxy fail → FRESH proxies se 2 aur attempts
            try:
                _extra = [p for p in _alive_proxies() if p != (proxy or "")]
            except Exception:
                _extra = []
            for _px in _extra[:2]:
                ok, _ = _fetch_store_one(vid, orig_url, dl_fmt, _px,
                                         want_video)
                if ok:
                    break
        if not ok and proxy:
            # last resort: direct (cookies) retry
            ok, _ = _fetch_store_one(vid, orig_url, dl_fmt, "", want_video)
        if ok:
            logger.info(f"📼 BG archive fill done: {vid} [{kind}]")
        else:
            logger.warning(f"📼 BG archive fill FAILED sab attempts: {vid}")
    except Exception as e:
        logger.debug(f"bg fill fail {vid}: {e}")


def _pool_refresh_bg():
    """Background pool refresh (pool dead hone par trigger)."""
    try:
        import proxyrefresh
        proxyrefresh.refresh()
    except Exception:
        pass


def _fetch_store_one(vid: str, url: str, dl_fmt: str, proxy: str,
                     want_video: bool) -> tuple:
    """
    🎬 Ek proxy se: yt-dlp DOWNLOAD → TG archive upload.
    Download aur extraction SAME proxy se — signed-URL 403 problem khatam.
    Returns (ok: bool, info_result: dict).
    """
    import tempfile
    import glob as _g
    import concurrent.futures as _cf2
    import tgstore
    tmpdir = tempfile.mkdtemp(prefix="hellapi_dl_")
    try:
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": False,
            "format": dl_fmt, "outtmpl": os.path.join(tmpdir, f"{vid}.%(ext)s"),
            "noplaylist": True, "socket_timeout": 20, "retries": 1,
            "nocheckcertificate": True,
            "proxy": (f"http://{proxy}" if proxy else None),
        }
        # 🍪 COOKIES: direct + proxy dono pe (proxy bhi bot-checked Sep-2026)
        _cf = _cookie_file()
        if _cf:
            opts["cookiefile"] = _cf
        if not proxy:
            # 🛡 direct me mid-download cut aam hai — yt-dlp ka apna
            # fragment-retry use karo (proxy me ye pehle se theek hai)
            opts["retries"] = 3
            opts["file_access_retries"] = 2
            opts["fragment_retries"] = 5
        if want_video:
            opts["merge_output_format"] = "mp4"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
            if not os.path.exists(path):
                cands = _g.glob(os.path.join(tmpdir, f"{vid}.*"))
                path = cands[0] if cands else ""
            if not (path and os.path.exists(path)
                    and os.path.getsize(path) > 100_000):
                raise ExtractionError("download empty")
        kind = "video" if want_video else "audio"
        ext = "mp4" if want_video else (info.get("ext") or "m4a")
        # 💾 VPS LOCAL-DISK CACHE — download hua file disk pe bhi rakho;
        # /file/ ise pehle serve karega (TG roundtrip latency zero)
        try:
            import shutil as _sh2
            os.makedirs(_LOCAL_CACHE_DIR, exist_ok=True)
            _dst = os.path.join(_LOCAL_CACHE_DIR, f"{vid}.{ext}")
            _sh2.copyfile(path, _dst)
        except Exception as _ce:
            logger.debug(f"local cache save fail {vid}: {_ce}")
        ok = asyncio.run(tgstore.tg_store_file(
            vid, info.get("title") or vid, "",
            ext=ext, kind=kind, local_file=path))
        return bool(ok), _normalise_info(info)
    finally:
        import shutil as _sh
        _sh.rmtree(tmpdir, ignore_errors=True)


async def _relay_first_proxy_serve(vid: str, url: str,
                                   format_id: Optional[str]):
    """
    ⚡ RELAY-FIRST (proxy tier) — user ka naya flow:
      extraction (1.5-3s) ke TURANT baad /relay/ URL return — bot download
      + stream shuru kar deta hai, aur TG-channel archive BG me chalti rahti
      hai. Blocking store-then-serve (10-60s wait) KHATAM.
    """
    loop = asyncio.get_running_loop()
    _ck = f"stream::v3::{url}::{format_id or 'best'}"
    _cached = _stream_cache_get(_ck)
    if _cached and _cached_result_ok(_cached):
        return _cached["info"], _cached["result"]
    alive = await loop.run_in_executor(_EXECUTOR, _alive_proxies)
    fmt = format_id or "bestaudio/best"
    want_video = bool("video" in fmt)
    # 🎬 itag-18 har video pe nahi hota ('Requested format is not available'
    # pura race maar deta tha) — progressive/combo-tolerant selector:
    dl_fmt = ("best[vcodec!=none][acodec!=none]/bestvideo+bestaudio/best"
              if want_video else "bestaudio[ext=m4a]/bestaudio/best")

    info = None
    proxy = None

    # 🚀 PHASE 1 — DIRECT-FIRST jab POOL WEAK/DEAD (0-2 zinda) — cookies
    # fresh hain to 2.5s me jeet; dead-proxy 8s race-wait bachta hai.
    if _cookie_file() and len(alive) <= 2:
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(
                    _EXECUTOR, lambda: _extract_with_proxy(url, dl_fmt, "")),
                timeout=float(os.getenv("DIRECT_FIRST_TIMEOUT", "14")))
            if info:
                proxy = ""
                logger.info(f"🚀 relay-direct: {vid} — cookies-direct extraction (fast pipe)")
        except Exception as e:
            logger.info(f"direct-first miss {vid}: {str(e)[:90]} — proxy-race pe")
            info = None

    # 🏁 PHASE 2 — PROXY RACE (direct fail hua to)
    if info is None:
        if not alive:
            logger.warning(f"proxy-tier: pool dead — direct/fallback pe guzara ({vid})")
            loop.run_in_executor(_EXECUTOR, _pool_refresh_bg)
            return None
        try:
            info, proxy = await loop.run_in_executor(
                _EXECUTOR, lambda: _extract_race(url, dl_fmt, alive))
        except Exception as e:
            logger.warning(f"relay-first race fail {vid}: {e}")
            loop.run_in_executor(_EXECUTOR, _pool_refresh_bg)
            return None
    direct_url = _direct_url_from_info(info, allow_dash=not want_video)
    if not direct_url:
        return None
    negative_cache_clear(url)
    info_result = _normalise_info(info)
    try:
        _cache_set(f"info::{url}", info_result)
        from database import cache_video_metadata as _cvm
        _cvm(url, info_result)
    except Exception:
        pass
    base = (os.getenv("APP_URL", "") or "http://localhost:8000").rstrip("/")
    ext = "mp4" if want_video else "mp3"
    _relay_set_keep(vid, url, dl_fmt, direct_url, proxy, want_video,
               info_result.get("title") or vid)
    dl_result = {
        "title":      info_result.get("title") or vid,
        "format_id":  "hellrelay",
        "ext":        ext,
        "resolution": "video" if want_video else "audio",
        "direct_url": f"{base}/relay/{vid}",
        "is_live":    False,
        "via_fallback": True,
    }
    # 🔥 BG: download + TG-channel upload — bot stream karte waqt hi song
    # channel me pahunch jata hai (agli baar /file/ = instant + permanent)
    # ⚡ BG fill WINNER-PROXY se (same-proxy download = 403-free, fast);
    # fail hua to _bg_fill khud direct retry karta hai.
    import threading as _th
    _th.Thread(target=_bg_fill,
               args=(vid, url, dl_fmt, proxy or "", want_video),
               daemon=True).start()
    exp = time.time() + 1800
    _stream_cache_set(_ck, {"info": info_result, "result": dl_result}, exp)
    try:
        from database import cache_stream
        cache_stream(url, format_id or "best", info_result, dl_result, exp)
    except Exception:
        pass
    _cb_record_success()
    logger.info(f"⚡ RELAY-FIRST (proxy): {vid} via {proxy} — stream ab, archive bg me")
    return info_result, dl_result


async def _coalesce_store_serve(vid: str, url: str,
                                format_id: Optional[str]):
    """Ek hi url+fmt ke parallel _proxy_store_serve calls COALESCE —
    /search pre-warm aur /song ka race ek hi baar chalega."""
    key = f"{url}::{format_id or 'best'}"
    loop = asyncio.get_running_loop()
    with _INFLIGHT_SLOCK:
        fut = _INFLIGHT_STREAMS.get(key)
        if fut is None:
            fut = loop.create_task(_proxy_store_serve(vid, url, format_id))
            _INFLIGHT_STREAMS[key] = fut
    try:
        return await fut
    finally:
        with _INFLIGHT_SLOCK:
            if _INFLIGHT_STREAMS.get(key) is fut:
                _INFLIGHT_STREAMS.pop(key, None)


async def _proxy_store_serve(vid: str, url: str,
                             format_id: Optional[str] = None) -> Optional[tuple]:
    """
    📥 STORE-THEN-SERVE (user ka naya flow):
      1. Proxy IP se extract + DOWNLOAD (yt-dlp, same proxy = 403-free)
      2. MP3/MP4 file → APNE TG archive channel me upload
      3. Uske BAAD user ko /file/ permanent URL (archive-first instant)
    ⚡ RACE extraction (4 proxies parallel) + bounded budget.
    """
    loop = asyncio.get_running_loop()
    # ⚡ RELAY/STREAM-CACHE: isi url+fmt ka result abhi bana tha →
    # dobara extract/health-check kuch nahi — pure instant repeat
    _ck = f"stream::v3::{url}::{format_id or 'best'}"
    _cached = _stream_cache_get(_ck)
    if _cached and _cached_result_ok(_cached):
        return _cached["info"], _cached["result"]
    alive = await loop.run_in_executor(_EXECUTOR, _alive_proxies)
    if not alive:
        logger.warning(f"proxy-tier: pool dead — bg refresh trigger ({vid})")
        loop.run_in_executor(_EXECUTOR, _pool_refresh_bg)
        return None
    fmt = format_id or "bestaudio/best"
    want_video = bool("video" in fmt)
    dl_fmt = ("best[vcodec!=none][acodec!=none]/bestvideo+bestaudio/best"
              if want_video else "bestaudio[ext=m4a]/bestaudio/best")
    # ⚡ RACE: 4 zinda proxies pe PARALLEL extraction — pehli jeet turant
    try:
        info, proxy = await loop.run_in_executor(
            _EXECUTOR, lambda: _extract_race(url, dl_fmt, alive))
    except Exception as e:
        logger.warning(f"proxy-tier race fail {vid}: {e}")
        # 🔥 auto-heal: pool shayad purana — TURANT bg refresh (agli request
        # fresh pool pe chalegi)
        loop.run_in_executor(_EXECUTOR, _pool_refresh_bg)
        return None
    direct_url = _direct_url_from_info(info, allow_dash=not ("video" in (format_id or "")))
    if not direct_url:
        return None
    negative_cache_clear(url)
    try:
        from database import negative_clear
        negative_clear(url)
    except Exception:
        pass
    info_result = _normalise_info(info)
    # ℹ️ INFO bhi cache — /song ka get_video_info dobara extract na kare
    try:
        _cache_set(f"info::{url}", info_result)
        from database import cache_video_metadata as _cvm
        _cvm(url, info_result)
    except Exception:
        pass

    # 📥 DOWNLOAD-FIRST (user ka flow): file poora download + TG channel
    # upload — uske BAAD hi stream URL dete hain (playback fail-proof)
    logger.info(f"📥 downloading {vid} via {proxy} ({dl_fmt[:24]}...)")
    ok = False
    try:
        ok, info_result = await loop.run_in_executor(
            _EXECUTOR,
            lambda: _fetch_store_one(vid, url, dl_fmt, proxy, want_video),
        )
    except Exception as e:
        logger.warning(f"proxy-tier store fail {vid}: {e}")
    if ok:
        # ✅ channel me file pahunch gayi — ab /file/ se instant playback
        kind = "video" if want_video else "audio"
        info_result2, dl_result = _archive_payload(vid, kind=kind)
        info_result2["title"] = info_result2.get("title") or info_result.get("title") or vid
        if not info_result2.get("duration_seconds"):
            info_result2["duration_seconds"] = info_result.get("duration_seconds") \
                or info_result.get("duration")
        info_result2["uploader"] = info_result2.get("uploader") or info_result.get("uploader")
        exp = time.time() + 86400
        cache_key = f"stream::v3::{url}::{format_id or 'best'}"
        _stream_cache_set(cache_key, {"info": info_result2, "result": dl_result}, exp)
        try:
            from database import cache_stream
            cache_stream(url, format_id or "best", info_result2, dl_result, exp)
        except Exception:
            pass
        _cb_record_success()
        logger.info(f"📼 STORE-THEN-SERVE: {vid} → archive + /file/ ({kind})")
        return info_result2, dl_result

    # ⚠️ download fail — EMERGENCY relay (stream start ho jaye, archive
    # bg me phir bhi try hogi relay ke pehle byte pe)
    _relay_set_keep(vid, url, dl_fmt, direct_url, proxy, want_video,
               info_result.get("title") or vid)
    base = (os.getenv("APP_URL", "") or "http://localhost:8000").rstrip("/")
    ext = "mp4" if want_video else "mp3"
    dl_result = {
        "title":      info_result.get("title") or vid,
        "format_id":  "hellrelay",
        "ext":        ext,
        "resolution": "video" if want_video else "audio",
        "direct_url": f"{base}/relay/{vid}",
        "is_live":    False,
        "via_fallback": True,
    }
    info_result2 = dict(info_result)
    info_result2["title"] = info_result2.get("title") or info_result.get("title") or vid
    if not info_result2.get("duration_seconds"):
        info_result2["duration_seconds"] = info_result.get("duration_seconds") \
            or info_result.get("duration")
    info_result2["uploader"] = info_result2.get("uploader") or info_result.get("uploader")
    exp = time.time() + 86400
    cache_key = f"stream::v3::{url}::{format_id or 'best'}"
    _stream_cache_set(cache_key, {"info": info_result2, "result": dl_result}, exp)
    try:
        from database import cache_stream
        cache_stream(url, format_id or "best", info_result2, dl_result, exp)
    except Exception:
        pass
    _cb_record_success()
    logger.info(f"⚡ PROXY RELAY (store fail fallback): {vid}")
    return info_result2, dl_result


async def _ensure_archived(vid: str, url: str, want_video: bool) -> Optional[tuple]:
    """
    🔒 FINAL GATE: IP-bound googlevideo URL kabhi client ko mat do.
    Archive me file hai? serve karo. Nahi? proxy se extract → store → serve.
    Store fail → None (caller proper error de, raw URL kabhi nahi).
    """
    kind = "video" if want_video else "audio"
    try:
        if _tg_file_stored(vid, kind=kind):
            return _archive_payload(vid, kind=kind)
    except Exception:
        pass
    import tgstore
    try:
        loop = asyncio.get_running_loop()
        if want_video:
            def _exv():
                path, tdir = _download_to_file(vid, url, "video")
                if not path:
                    return None
                try:
                    return asyncio.run(tgstore.tg_store_file(
                        vid, vid, "", ext="mp4", kind="video",
                        local_file=path))
                finally:
                    import shutil as _sh
                    _sh.rmtree(tdir, ignore_errors=True)
            ok = await loop.run_in_executor(_EXECUTOR, _exv)
        else:
            def _exa():
                info = _extract_sync(url, {"format": "bestaudio/best"})
                return _direct_url_from_info(info)
            aurl = await loop.run_in_executor(_EXECUTOR, _exa)
            if not aurl:
                return None
            ok = await loop.run_in_executor(
                _EXECUTOR, lambda: asyncio.run(tgstore.tg_store_file(
                    vid, vid, aurl, ext="m4a")))
        if ok:
            logger.info(f"🔒 GATE: {vid} archived → /file/ ({kind})")
            return _archive_payload(vid, kind=kind)
    except Exception as e:
        logger.warning(f"ensure-archived fail {vid}: {e}")
    return None


# ── Playlist/Mix URL resolver ──────────────────────────────────────────────
# Bot kabhi-kabhi poora playlist URL bhej deta hai:
#   https://youtube.com/playlist?list=RD...&playnext=1&si=...
# Ye VIDEO nahi hai — isko video ki tarah extract karne par yt-dlp playlist-
# page maangta hai (403/bot-check) → 17-19s hang → 429 → circuit breaker khul
# jata hai → SAARI requests fail ("failed to fetch track").
# Fix: playlist/Mix URL ko pehle uske FIRST VIDEO pe normalise karo.
_PLAYLIST_RESOLVED: Dict[str, str] = {}          # list_id -> video_id (TTL bhi)
_PLAYLIST_RESOLVED_TS: Dict[str, float] = {}


def _is_playlist_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    if "list=" in u and "v=" not in u:
        return True
    # youtu.be/xxx?list=...  → v= nahi hota URL-encoded form me bhi
    if "/playlist" in u and "list=" in u:
        return True
    return False


def _extract_list_id(url: str) -> str:
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else ""


def _cache_playlist_resolution(list_id: str, vid: str):
    _PLAYLIST_RESOLVED[list_id] = vid
    _PLAYLIST_RESOLVED_TS[list_id] = time.time()


def _cached_playlist_resolution(list_id: str) -> str:
    ts = _PLAYLIST_RESOLVED_TS.get(list_id, 0)
    if ts and time.time() - ts < 3600:            # 1h — Radio/Mix rotate hote hain
        return _PLAYLIST_RESOLVED.get(list_id, "")
    _PLAYLIST_RESOLVED.pop(list_id, None)
    return ""


def _resolve_playlist_first_video(url: str) -> str:
    """playlist?list=X → watch?v=<first-video>&...  (flat, entries only).
    Returns ORIGINAL url agar resolve na ho sake (caller waise hi chalega)."""
    list_id = _extract_list_id(url)
    if not list_id:
        return url
    hit = _cached_playlist_resolution(list_id)
    if hit:
        return f"https://www.youtube.com/watch?v={hit}"
    # 🎯 YouTube MIX/Radio: list-id = "RD" + seed-video-id (11 chars) —
    #   RDeeNv4wf2D1U → seed eeNv4wf2D1U. Mix playlist-page se fetch HOTE HI
    #   NAHI (YouTube support nahi karta — 403/bot-check) par seed video
    #   bina kisi network call ke mil jata hai. Ye hi bot ka main case hai.
    m = re.match(r"^RD(?:AMVM|MM|GM|CLAK5uy_[A-Za-z0-9_-]{20,})?([A-Za-z0-9_-]{11})$", list_id)
    if m:
        vid = m.group(1)
        _cache_playlist_resolution(list_id, vid)
        logger.info(f"📋 MIX resolve (seed-id, zero-network): {list_id[:20]}… → {vid}")
        return f"https://www.youtube.com/watch?v={vid}"
    import concurrent.futures as _cfp
    _flat = {
        "quiet": True, "no_warnings": True,
        "extract_flat": "in_playlist",   # playlist page hi kaafi — video extract NAHI
        "playlist_items": "1",
        "playlistend": 1,
        "skip_download": True,
        "socket_timeout": 12,
        "retries": 1,
    }
    # 🍪 Playlist page pe BHI bot-check (403) lagta hai — cookies zaroori
    _pcf = _cookie_file()
    if _pcf:
        _flat["cookiefile"] = _pcf
    ex = _cfp.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(
            lambda: yt_dlp.YoutubeDL(_flat).extract_info(
                f"https://www.youtube.com/playlist?list={list_id}",
                download=False)
        )
        info = fut.result(timeout=20)
        entries = (info or {}).get("entries") or []
        first = next(iter(entries), None)
        if isinstance(first, dict):
            vid = first.get("id") or ""
            if vid:
                _cache_playlist_resolution(list_id, vid)
                logger.info(f"📋 Playlist resolve: {list_id[:20]}… → {vid}")
                return f"https://www.youtube.com/watch?v={vid}"
    except Exception as e:
        logger.warning(f"playlist resolve fail {list_id[:24]}: {str(e)[:120]}")
    finally:
        ex.shutdown(wait=False)
    return url


async def _prearchive(url: str):
    """🔥 SEARCH-PREFETCH: /search ke top results BG me race-extract +
    download+TG-archive turant shuru — user play kare tab tak /file/ ready.
    (PHASE-1 direct ka 12-16s waste bhi skip hota hai — sirf proxy-race.)
   """
    try:
        vid = (url or "").split("v=")[-1].split("&")[0].split("/")[-1]
        if not vid:
            return
        if _tg_file_stored(vid, kind="audio"):
            return                      # pehle se archive — kuch nahi karna
        loop = asyncio.get_running_loop()
        alive = await loop.run_in_executor(_EXECUTOR, _alive_proxies)
        if not alive:
            return
        dl_fmt = "bestaudio[ext=m4a]/bestaudio/best"
        try:
            info, proxy = await loop.run_in_executor(
                _EXECUTOR,
                lambda: _extract_race(url, dl_fmt, alive))
        except Exception:
            return
        du = _direct_url_from_info(info)
        if not du:
            return
        title = ""
        try:
            title = (info.get("title") or vid)
        except Exception:
            pass
        _relay_set_keep(vid, url, dl_fmt, du, proxy, False, title)
        logger.info(f"🔥 PRE-ARCHIVE ready: {vid} via {proxy} — BG download shuru")
        import threading as _th
        _th.Thread(target=_bg_fill,
                   args=(vid, url, dl_fmt, proxy or "", False),
                   daemon=True).start()
    except Exception as e:
        logger.debug(f"prearchive {url}: {e}")


async def get_stream_data_with_fallback(url: str, format_id: Optional[str] = None):
    """
    get_stream_data + IP-block pe Invidious/Piped fallback.
    Speed logic:
      - Direct pehli baar fail → mark_direct_dead(30m) → agli requests
        seedha fallback (zero wasted YT hits, ~5-10s total)
      - Har 30 min me ek PROBE request direct try hoti hai — YouTube agar
        IP unblock kar de to wapas direct mode (mark_direct_alive)
    """
    # 📋 PLAYLIST/MIX URL? pehle first-video pe normalise karo (bot playnext=1
    # URLs bhejta hai — unhe video samajhna = bot-check + 429 + breaker khulna)
    if _is_playlist_url(url):
        url = await asyncio.get_event_loop().run_in_executor(
            None, _resolve_playlist_first_video, url)

    vid = (url or "").split("v=")[-1].split("&")[0].split("/")[-1]
    import fallback as fb

    # 📼 ARCHIVE-FIRST: pehle apna song-archive check karo (direct status
    # kuch bhi ho) — ek baar store hua gaana hamesha instant milega.
    _want_video = bool(format_id and "video" in (format_id or ""))
    if _want_video and _tg_file_stored(vid, kind="video"):
        info_result, dl_result = _archive_payload(vid, kind="video")
        logger.info(f"📼 ARCHIVE HIT (video): {vid}")
        _cb_record_success()
        return info_result, dl_result
    if not _want_video and _tg_file_stored(vid, kind="audio"):
        info_result, dl_result = _archive_payload(vid, kind="audio")
        logger.info(f"📼 ARCHIVE HIT: {vid}")
        _cb_record_success()
        return info_result, dl_result
    # 🎬 VIDEO-manga-par-sirf-AUDIO-archive: BG me VIDEO download+cache
    # trigger karo (agli vplay instant) — aur video-guard (local-cache pehle)
    if _want_video and not _tg_file_stored(vid, kind="video"):
        _lp = local_cache_path(vid, "video")
        if _lp:
            # local-disk me mp4 hai — TG archive me nahi, par serve-ready
            import tgstore as _tgs
            try:
                info_result = dict(_archive_payload(vid, kind="video")[0])
                dl_result = {
                    "title": info_result.get("title") or vid,
                    "format_id": "tgfile", "ext": "mp4",
                    "resolution": "video",
                    "direct_url": f"{(os.getenv('APP_URL', '') or 'http://localhost:8000').rstrip('/')}/file/{vid}?kind=video",
                    "filesize": os.path.getsize(_lp),
                    "is_live": False, "via_fallback": True,
                }
                logger.info(f"💾 LOCAL-CACHE HIT (video): {vid}")
                _cb_record_success()
                # BG: TG-archive me bhi upload (permanent)
                import threading as _th
                def _tgv(_p=_lp):
                    try:
                        asyncio.run(_tgs.tg_store_file(
                            vid, info_result.get("title") or vid, "",
                            ext="mp4", kind="video", local_file=_p))
                    except Exception:
                        pass
                _th.Thread(target=_tgv, daemon=True).start()
                return info_result, dl_result
            except Exception:
                pass
        # local-cache nahi — 🎬 SYNCHRONOUS video-fill: audio-archive hai =
        # gaana valid hai. Video-download PROXY+COOKIES se BLOCKING —
        # direct-IP restricted hai (UNPLAYABLE), working proxy use karo.
        import concurrent.futures as _cfx
        try:
            _alive_v = _alive_proxies()
            _vok = False
            with _cfx.ThreadPoolExecutor(max_workers=1) as _vx:
                for _px in (_alive_v[:2] + [""]):
                    try:
                        _vok, _vinfo = _vx.submit(
                            _fetch_store_one, vid, url,
                            "best[vcodec!=none][acodec!=none]/bestvideo+bestaudio/best",
                            _px, True).result(timeout=140)
                        if _vok:
                            break
                    except Exception:
                        continue
            if _vok:
                _lp = local_cache_path(vid, "video")
                if _lp:
                    info_result = dict(_archive_payload(vid, kind="video")[0])
                    dl_result = {
                        "title": info_result.get("title") or vid,
                        "format_id": "tgfile", "ext": "mp4",
                        "resolution": "video",
                        "direct_url": f"{(os.getenv('APP_URL', '') or 'http://localhost:8000').rstrip('/')}/file/{vid}?kind=video",
                        "filesize": os.path.getsize(_lp),
                        "is_live": False, "via_fallback": True,
                    }
                    logger.info(f"🎬 SYNC video-fill done: {vid}")
                    _cb_record_success()
                    return info_result, dl_result
        except Exception as _ve:
            logger.warning(f"🎬 SYNC video-fill fail {vid}: {str(_ve)[:90]}")

    # ⚡ PROXY-FIRST: pool zinda hai to DIRECT extraction skip hi karo —
    # datacenter-IP har baar bot-checked (16s timeout baithana bekaar).
    # Race-extraction 1.5-3s me URL de deta hai → relay instantly.
    if get_proxy_pool():
        ps = await _relay_first_proxy_serve(vid, url, format_id)
        if ps:
            return ps

    # Fallback-first fast path (direct dead ho chuka hai)
    # Order: PROXY TIER (self-reliant) → Invidious/Piped → direct probe
    if is_direct_dead():
        if get_proxy_pool():
            ps = await _proxy_store_serve(vid, url, format_id)
            if ps:
                return ps
        if fb.FALLBACK_ENABLED:
            fb_res = await fb.resolve_audio(vid)
            if fb_res and not _want_video:
                info_result, dl_result = _fb_result_to_payload({}, fb_res, vid)
                exp = time.time() + 1800
                cache_key = f"stream::v3::{url}::{format_id or 'best'}"
                _stream_cache_set(cache_key, {"info": info_result, "result": dl_result}, exp)
                try:
                    from database import cache_stream
                    cache_stream(url, format_id or "best", info_result, dl_result, exp)
                except Exception:
                    pass
                _cb_record_success()     # user ko answer mil gaya — breaker reset
                # 📼 song-archive — MP3 fail to VIDEO se serve, raw URL kabhi nahi
                if not _tg_file_stored(vid):
                    ok_a = await _fb_archive(info_result, dl_result, vid)
                    if not ok_a:
                        vf = None
                        try:
                            from database import tg_file_get
                            vf = tg_file_get(vid, kind="video")
                        except Exception:
                            pass
                        if vf and vf[0]:
                            info_result, dl_result = _archive_payload(vid, kind="video")
                        else:
                            return None   # dead instance URL serve karna = "failed track"
                return info_result, dl_result
            # fallback bhi fail — direct try karne me hi bhalai (kuch to mile)

    try:
        result = await get_stream_data(url, format_id)
        mark_direct_alive()          # direct wapas zinda — probe successful
        # 🔒 IP-GATE (ab HAMESHA — proxy pool khali ho tab bhi): googlevideo
        # URL extraction-IP se signed hota hai, bot ke IP se 403 deta hai.
        # Raw URL KABHI mat do — extraction ke TURANT baad /relay/ URL:
        #   bot download+stream TURANT shuru, TG-channel archive BG me.
        #   (user ka flow: download + stream + channel-send SAB PARALLEL)
        du = (result[1].get("direct_url") or "") if isinstance(result, tuple) else ""
        if "googlevideo.com" in du and isinstance(result, tuple):
            base = (os.getenv("APP_URL", "") or "http://localhost:8000").rstrip("/")
            ext = "mp4" if _want_video else "mp3"
            dl_fmt = ("best[vcodec!=none][acodec!=none]/bestvideo+bestaudio/best"
                      if _want_video else "bestaudio[ext=m4a]/bestaudio/best")
            title = (result[0].get("title") or vid)
            _relay_set_keep(vid, url, dl_fmt, du, "", _want_video, title)
            dl_result = {
                "title":      (result[1].get("title") or title),
                "format_id":  "hellrelay",
                "ext":        ext,
                "resolution": "video" if _want_video else "audio",
                "direct_url": f"{base}/relay/{vid}",
                "is_live":    False,
                "via_fallback": True,
            }
            info_result = dict(result[0])
            # 🔥 BG archive — relay ke pehle byte pe bhi fill trigger hota hai,
            # ye double-insurance hai (dono tg_file_get check karke skip karte)
            import threading as _th
            _th.Thread(target=_bg_fill,
                       args=(vid, url, dl_fmt, "", _want_video),
                       daemon=True).start()
            exp = time.time() + 1800
            _ck = f"stream::v3::{url}::{format_id or 'best'}"
            _stream_cache_set(_ck, {"info": info_result, "result": dl_result}, exp)
            try:
                from database import cache_stream
                cache_stream(url, format_id or "best", info_result, dl_result, exp)
            except Exception:
                pass
            _cb_record_success()
            logger.info(f"⚡ RELAY-FIRST: {vid} — stream ab, archive bg me")
            return info_result, dl_result
        return result
    except IPBlockedError:
        mark_direct_dead(30)         # agli baar seedha proxy/fallback
        # ⚡ proxy-first ne already try kiya — dobara DOUBLE-RACE mat karo
        if not get_proxy_pool():
            raise
        if not fb.FALLBACK_ENABLED:
            raise
        fb_res = await fb.resolve_audio(vid)
        if not fb_res:
            raise                    # proxy fail + fallback fail — koi retry nahi
        if _want_video:
            raise ExtractionError(
                "Video stream unavailable — thodi der baad try karo.")
        info_result, dl_result = _fb_result_to_payload({}, fb_res, vid)
        # 30 min cache (proxied URLs ki expiry instance pe depend karti hai)
        exp = time.time() + 1800
        cache_key = f"stream::v3::{url}::{format_id or 'best'}"
        _stream_cache_set(cache_key, {"info": info_result, "result": dl_result}, exp)
        try:
            from database import cache_stream
            cache_stream(url, format_id or "best", info_result, dl_result, exp)
        except Exception:
            pass
        _cb_record_success()         # fallback ne bacha liya — breaker reset (false 429 nahi)
        logger.warning(f"♻ FALLBACK used for {vid} via {fb_res.get('via')}")
        # 📼 song-archive — MP3 fail to VIDEO se serve, raw URL kabhi nahi
        if not _tg_file_stored(vid):
            ok_a = await _fb_archive(info_result, dl_result, vid)
            if not ok_a:
                vf = None
                try:
                    from database import tg_file_get
                    vf = tg_file_get(vid, kind="video")
                except Exception:
                    pass
                if vf and vf[0]:
                    info_result, dl_result = _archive_payload(vid, kind="video")
                else:
                    raise ExtractionError(
                        "Stream temporarily unavailable — thodi der baad try karo.")
        return info_result, dl_result


@retry(
    reraise=True,
    stop=stop_after_attempt(int(os.getenv("YTDLP_MAX_RETRIES", "2"))),
    wait=wait_exponential(multiplier=2, min=3, max=20),
    retry=retry_if_exception_type(IPBlockedError),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def _extract_sync(url: str, extra_opts: Optional[Dict] = None) -> Dict[str, Any]:
    return _try_extract(url, extra_opts)


# ── Async wrappers ───────────────────────────────────────────────────────────
async def _extract_async(url: str, extra_opts: Optional[Dict] = None) -> Dict[str, Any]:
    """Run extraction without blocking FastAPI and coalesce duplicate work."""
    loop = asyncio.get_running_loop()
    opts_key = repr(sorted((extra_opts or {}).items()))
    inflight_key = f"{url}::{opts_key}"

    with _inflight_lock:
        future = _INFLIGHT.get(inflight_key)
        owner = future is None
        if owner:
            future = loop.run_in_executor(
                _EXECUTOR, lambda: _extract_sync(url, extra_opts)
            )
            _INFLIGHT[inflight_key] = future

    try:
        # ⏱ HARD TIMEOUT: bot-checked VPS-IP direct-extract 2-3 min tak hang
        # karta tha → bot "fetch failed" dikhata tha. Ab 15s me verdict:
        # wait nahi, turant proxy-tier failover (wahi first-try me 90s bachta hai)
        return await asyncio.wait_for(asyncio.shield(future), timeout=15.0)
    finally:
        if owner:
            with _inflight_lock:
                if _INFLIGHT.get(inflight_key) is future:
                    _INFLIGHT.pop(inflight_key, None)


def _normalise_info(info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id":               info.get("id"),
        "title":            info.get("title"),
        "uploader":         info.get("uploader"),
        "channel_url":      info.get("channel_url"),
        "duration_seconds": info.get("duration"),
        "view_count":       info.get("view_count"),
        "like_count":       info.get("like_count"),
        "upload_date":      info.get("upload_date"),
        "thumbnail":        info.get("thumbnail"),
        "description":      (info.get("description") or "")[:500],
        "webpage_url":      info.get("webpage_url"),
        "is_live":          info.get("is_live", False),
        "tags":             (info.get("tags") or [])[:10],
    }


def _direct_url_from_info(info: Dict[str, Any], allow_dash: bool = True) -> Optional[str]:
    """Best stream URL nikaalo.
    🎬 allow_dash=False (video requests): SIRF progressive (video+audio ek
    URL) — DASH-only video-track VC me 'NoAudioSourceFound' deta tha.
    allow_dash=True (audio): progressive nahi to DASH bhi chalega."""
    # 1️⃣ PROGRESSIVE pehle (requested_formats me video+audio combo)
    for fmt in (info.get("requested_formats") or []):
        if fmt.get("url") and fmt.get("vcodec", "none") != "none" \
                and fmt.get("acodec", "none") != "none":
            return fmt["url"]
    if info.get("url"):
        _u = info.get("url")
        _proto = (info.get("protocol") or "https")
        if "m3u8" in _proto or "m3u8" in _u:
            return _u                      # HLS (live) — as-is theek hai
        if info.get("acodec") not in ("none", None) and \
                info.get("vcodec") not in ("none", None):
            return _u
        # audio-only top-level URL (bestaudio extraction)
        if info.get("vcodec") in ("none", None):
            if allow_dash:
                return _u
        else:
            return _u   # top-level video+audio = progressive
    # 1.5️⃣ FORMATS SCAN — requested_formats DASH ho sakta hai, par formats
    # list me progressive (18/22) mojood hota hai — highest height wala lo
    _prog = [f for f in (info.get("formats") or [])
             if f.get("url") and f.get("vcodec", "none") != "none"
             and f.get("acodec", "none") != "none"
             and (f.get("protocol") or "https") == "https"]
    if _prog:
        _best = max(_prog, key=lambda f: f.get("height") or 0)
        return _best["url"]
    # 2️⃣ DASH fallback — sirf audio ke liye (video pe NoAudioSourceFound)
    if allow_dash:
        for fmt in (info.get("requested_formats") or []):
            if fmt.get("url"):
                return fmt["url"]
        for fmt in (info.get("formats") or []):
            if fmt.get("url"):
                return fmt["url"]
    return None


# ── Public API ───────────────────────────────────────────────────────────────

async def get_video_info(url: str) -> Dict[str, Any]:
    # 0. Negative cache — deleted/private video instant fail (no YT hit)
    #    (proxy pool ho to verdict transient ho sakta hai — proxy tier try)
    if negative_cache_get(url):
        vid_neg = (url or "").split("v=")[-1].split("&")[0].split("/")[-1]
        if get_proxy_pool() and _neg_probe_ok(url):
            ps = await _proxy_store_serve(vid_neg, url, None)
            if ps:
                negative_cache_clear(url)
                return ps[0]
        negative_cache_set(url)   # proxy bhi fail — verdict confirm, TTL refresh
        raise ExtractionError("Video unavailable (negative-cached).")

    # 1. RAM cache check
    cache_key = f"info::{url}"
    cached = _cache_get(cache_key)
    if cached:
        logger.debug(f"RAM cache HIT info: {url[:50]}")
        return cached

    # 2. DB cache check (survives restarts)
    try:
        from database import get_cached_video_metadata, cache_video_metadata
        db_cached = get_cached_video_metadata(url)
        if db_cached:
            logger.debug(f"DB cache HIT info: {url[:50]}")
            _cache_set(cache_key, db_cached)   # warm RAM cache too
            return db_cached
    except Exception:
        pass

    # 2.5 ⚡ LIBRARY FAST PATH — pehle play hua gaana (song_library me hai)
    # metadata ke liye YouTube pe hit nahi khayega. Bot ka /info instant.
    vid = (url or "").split("v=")[-1].split("&")[0].split("/")[-1]
    if len(vid) == 11:
        try:
            from database import library_get as _lg
            lib = _lg(vid) or {}
            if int(lib.get("duration_sec") or 0) > 0:
                result = {
                    "id":               vid,
                    "title":            lib.get("title") or vid,
                    "uploader":         lib.get("uploader") or "",
                    "channel_url":      None,
                    "duration_seconds": int(lib.get("duration_sec") or 0),
                    "view_count":       None,
                    "like_count":       None,
                    "upload_date":      None,
                    "thumbnail":        lib.get("thumbnail")
                                        or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                    "description":      "",
                    "webpage_url":      f"https://www.youtube.com/watch?v={vid}",
                    "is_live":          False,
                    "tags":             [],
                }
                _cache_set(cache_key, result)   # RAM cache warm
                logger.debug(f"LIBRARY HIT info: {url[:50]}")
                return result
        except Exception:
            pass

    # 3. Fetch from YouTube
    try:
        info = await _extract_async(url)
    except (ExtractionError, IPBlockedError) as e:
        # Direct blocked → oEmbed fallback (bot-check nahi hota, metadata-only)
        vid = (url or "").split("v=")[-1].split("&")[0].split("/")[-1]
        if len(vid) == 11:
            try:
                _title = None
                try:
                    import httpx as _hx
                    _r = _hx.get("https://www.youtube.com/oembed",
                                 params={"url": f"https://www.youtube.com/watch?v={vid}",
                                         "format": "json"}, timeout=6)
                    if _r.status_code == 200:
                        _title = (_r.json() or {}).get("title")
                except Exception:
                    _title = None
                if _title:
                    logger.info(f"/info via oEmbed (direct blocked): {vid}")
                    result = {
                        "id":               vid,
                        "title":            _title,
                        "uploader":         "",
                        "channel_url":      None,
                        "duration_seconds": None,
                        "view_count":       None,
                        "like_count":       None,
                        "upload_date":      None,
                        "thumbnail":        f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                        "description":      "",
                        "webpage_url":      f"https://www.youtube.com/watch?v={vid}",
                        "is_live":          False,
                        "tags":             [],
                    }
                    _cache_set(cache_key, result)
                    try:
                        from database import cache_video_metadata as _cvm
                        _cvm(url, result)
                    except Exception:
                        pass
                    return result
            except Exception:
                pass
        if _is_unavailable(e):
            negative_cache_set(url)
        raise
    result = _normalise_info(info)
    # 🖼 thumbnail guarantee — khali thumbnail bot ke photo-send todta tha
    if not result.get("thumbnail"):
        result["thumbnail"] = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    # 4. Save to both caches
    _cache_set(cache_key, result)
    try:
        cache_video_metadata(url, result)
    except Exception:
        pass

    return result


async def get_available_formats(url: str) -> List[Dict[str, Any]]:
    cache_key = f"formats::{url}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    info    = await _extract_async(url)
    formats = info.get("formats", [])
    result  = []
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") == "none":
            continue
        if (f.get("format_id") or "").startswith("sb"):
            continue
        result.append({
            "format_id":       f.get("format_id"),
            "ext":             f.get("ext"),
            "resolution":      f.get("resolution") or f.get("format_note", ""),
            "fps":             f.get("fps"),
            "vcodec":          f.get("vcodec"),
            "acodec":          f.get("acodec"),
            "filesize_approx": f.get("filesize") or f.get("filesize_approx"),
            "tbr":             f.get("tbr"),
        })
    _cache_set(cache_key, result)
    return result


async def get_direct_download_url(url: str, format_id: Optional[str] = None) -> Dict[str, Any]:
    fmt       = format_id or "best"
    cache_key = f"dl::{url}::{fmt}"
    if negative_cache_get(url):
        vid_neg = (url or "").split("v=")[-1].split("&")[0].split("/")[-1]
        if get_proxy_pool() and _neg_probe_ok(url):
            ps = await _proxy_store_serve(vid_neg, url, fmt)
            if ps:
                negative_cache_clear(url)
                return ps[1]
        negative_cache_set(url)
        raise ExtractionError("Video unavailable (negative-cached).")
    cached    = _cache_get(cache_key)
    if cached:
        logger.debug(f"Cache HIT dl: {url[:50]} fmt={fmt}")
        return cached

    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(
            _EXECUTOR, lambda: _race_extract(url, fmt))
        if not info:
            raise ExtractionError("direct timeout — proxy failover")
    except ExtractionError as e:
        if get_proxy_pool():
            vid_err = (url or "").split("v=")[-1].split("&")[0].split("/")[-1]
            ps = await _proxy_store_serve(vid_err, url, fmt)
            if ps:
                return ps[1]
        if _is_unavailable(e) and not get_proxy_pool():
            negative_cache_set(url)
        raise
    direct_url = _direct_url_from_info(info)

    if not direct_url:
        raise ExtractionError("Could not resolve a direct stream URL for this format.")

    result = {
        "title":      info.get("title"),
        "format_id":  info.get("format_id"),
        "ext":        info.get("ext"),
        "resolution": info.get("resolution"),
        "direct_url": direct_url,
        "is_live":    info.get("is_live", False),
    }
    _cache_set(cache_key, result)
    return result


def _cached_result_ok(cached: dict) -> bool:
    """
    Purane fallback URLs (dead instances / Anubis HTML) wali cache entries
    ko reject karo — self-healing. Instance-URLs hamesha fresh-verify hote
    hain (64KB probe, ~100-300ms); googlevideo/normal URLs as-is OK.
    """
    try:
        res = (cached or {}).get("result") or {}
        du = res.get("direct_url") or ""
    except Exception:
        return False
    if not du:
        return False
    if "latest_version" in du or "invidious." in du or "piped" in du:
        try:
            import fallback as _fbm
            return _fbm._verify_stream_url(du)
        except Exception:
            return True
    # IP-bound googlevideo URLs hamare IP se 403 dete hain — kabhi serve na karo
    if "googlevideo.com" in du and "/file/" not in du and "localhost" not in du:
        return False
    return True


async def get_stream_data(url: str, format_id: Optional[str] = None):
    """Return metadata and a direct stream URL from one yt-dlp extraction."""
    fmt = format_id or "best"
    cache_key = f"stream::v3::{url}::{fmt}"

    if negative_cache_get(url):
        vid_neg = (url or "").split("v=")[-1].split("&")[0].split("/")[-1]
        if get_proxy_pool() and _neg_probe_ok(url):
            ps = await _proxy_store_serve(vid_neg, url, fmt)
            if ps:
                negative_cache_clear(url)
                return ps
        negative_cache_set(url)
        raise ExtractionError("Video unavailable (negative-cached).")

    cached = _stream_cache_get(cache_key)
    if cached and _cached_result_ok(cached):
        return cached["info"], cached["result"]

    # DB stream cache (restart-proof) — same song dobara = zero YouTube hit
    # 🎬 KIND-CHECK: video manga aur cached ext mp3 hai (ya ulta) → REJECT
    # (DB video_cache sirf video_id se keyed hai — audio result video request
    #  ko mil jata tha → vplay me AUDIO bajta tha!)
    try:
        from database import get_cached_stream
        db_stream = get_cached_stream(url, fmt)
        if db_stream and _cached_result_ok(db_stream):
            _want_v2 = "video" in (fmt or "")
            _c_ext = str((db_stream.get("result") or {}).get("ext") or "").lower()
            if _want_v2 and _c_ext in ("mp3", "m4a"):
                logger.info(f"🎬 DB cache KIND-MISMATCH (video manga, { _c_ext } cached) — skip {url[:40]}")
                db_stream = None
            if db_stream:
                logger.debug(f"DB stream HIT: {url[:50]}")
                _stream_cache_set(cache_key, db_stream,
                                  time.time() + min(_STREAM_TTL_CAP_MIN, 60) * 60)
                return db_stream["info"], db_stream["result"]
    except Exception:
        pass

    # Telegram DB store (VPS-death-proof) — DB miss pe channel se wapas lao
    try:
        import tgstore
        if tgstore.is_enabled():
            tg_data = await tgstore.tg_load_stream(url, fmt)
            if tg_data and _cached_result_ok(tg_data):
                logger.info(f"TG store HIT: {url[:50]}")
                _stream_cache_set(cache_key, tg_data,
                                  time.time() + min(_STREAM_TTL_CAP_MIN, 60) * 60)
                try:
                    from database import cache_stream
                    info_d, res_d = tg_data["info"], tg_data["result"]
                    exp = _parse_gv_expiry(res_d.get("direct_url") or "")
                    cache_stream(url, fmt, info_d, res_d,
                                 min(exp, time.time() + _STREAM_TTL_CAP_MIN * 60))
                except Exception:
                    pass
                return tg_data["info"], tg_data["result"]
    except Exception:
        pass

    try:
        # ⚡ PROXY-FIRST: pool zinda hai to DIRECT attempt skip hi —
        # datacenter-IP bot-checked hai, 16s timeout baithana bekaar
        if get_proxy_pool():
            raise IPBlockedError("proxy-first — direct skip")
        # ⏱ BOUNDED direct attempt (16s) — hang nahi, turant failover
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(
            _EXECUTOR, lambda: _race_extract(url, fmt))
        if not info:
            raise IPBlockedError("direct-extract timeout — proxy failover")
    except ExtractionError as e:
        if get_proxy_pool():
            vid_err = (url or "").split("v=")[-1].split("&")[0].split("/")[-1]
            ps = await _proxy_store_serve(vid_err, url, fmt)
            if ps:
                return ps
        if _is_unavailable(e) and not get_proxy_pool():
            # proxy pool hai to ye verdict transient ho sakta hai —
            # video ko negative-cache me mat phansao
            negative_cache_set(url)
        raise
    direct_url = _direct_url_from_info(info)
    if not direct_url:
        raise ExtractionError("Could not resolve a direct stream URL for this format.")

    info_result = _normalise_info(info)
    dl_result = {
        "title":      info.get("title"),
        "format_id":  info.get("format_id"),
        "ext":        info.get("ext"),
        "resolution": info.get("resolution"),
        "direct_url": direct_url,
        "is_live":    info.get("is_live", False),
    }
    _cache_set(f"info::{url}", info_result)
    _cache_set(f"dl::{url}::{fmt}", dl_result)
    # Smart cache: googlevideo URL ki apni expiry tak (max cap tak)
    expires_at = min(
        _parse_gv_expiry(direct_url),
        time.time() + _STREAM_TTL_CAP_MIN * 60,
    )
    _stream_cache_set(cache_key, {"info": info_result, "result": dl_result},
                      expires_at)
    try:
        from database import cache_video_metadata, cache_stream
        cache_video_metadata(url, info_result)
        cache_stream(url, fmt, info_result, dl_result, expires_at)
    except Exception:
        pass
    # 📼 FILE archive only — URL-text posts (purana tg_store_stream) BAND,
    # kyunki DB channel me file ki jagah URL posts jaati thin
    try:
        import tgstore
        if tgstore.is_enabled():
            # MP3 FILE store (download+convert background thread me)
            vid = (info_result.get("id") or "").strip()
            if vid and len(vid) == 11 and not dl_result.get("is_live"):
                asyncio.get_event_loop().run_in_executor(
                    _EXECUTOR,
                    lambda: asyncio.run(
                        tgstore.tg_store_file(
                            vid, info_result.get("title") or "",
                            dl_result.get("direct_url") or "",
                            dl_result.get("ext") or "",
                        )
                    ),
                )
    except Exception:
        pass
    return info_result, dl_result


def _race_extract(url: str, fmt: str) -> dict:
    """⏱ TIMEOUT-DOGGY: bounded wait for direct extraction (thread join).
    Direct hang ho raha ho to None — proxy-tier ko turant chance milega."""
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
        try:
            return _ex.submit(_try_extract, url, {"format": fmt}).result(timeout=16)
        except Exception:
            return {}


def _race_extract(url: str, fmt: str) -> dict:
    """⏱ BOUNDED direct-extract: 16s me jawab nahi to {} — turant proxy
    failover. (Direct hang = bot pe 2-3 min ka 'fetch failed' tha.)"""
    import concurrent.futures as _cf
    _ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        return _ex.submit(_try_extract, url, {"format": fmt}).result(timeout=16)
    except Exception:
        return {}
    finally:
        _ex.shutdown(wait=False)


def _innertube_search_sync(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Fast search via YouTube innertube API (~0.3s, yt-dlp ke bina)."""
    import json as _json
    body = {
        "context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00",
                               "hl": "en", "gl": "IN"}},
        "query": query,
    }
    _sess = _innertube_session()
    if _sess is not None:
        with _sess.post(
            "https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
            json=body, timeout=6) as resp:
            data = resp.json()
    else:
        import urllib.request as _ureq
        req = _ureq.Request(
            "https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
            data=_json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                     "Origin": "https://www.youtube.com"},
            method="POST")
        with _ureq.urlopen(req, timeout=6) as resp:
            data = _json.loads(resp.read().decode())
    out = []

    def _walk(o):
        if isinstance(o, dict):
            vr = o.get("videoRenderer")
            if vr:
                vid = (vr.get("videoId") or "")[:11]
                if vid:
                    dur = None
                    try:
                        parts = [int(x) for x in
                                 vr.get("lengthText", {}).get("simpleText", "").split(":")
                                 if x.isdigit()]
                        if parts:
                            dur = 0
                            for p_ in parts:
                                dur = dur * 60 + p_
                    except Exception:
                        pass
                    try:
                        title = (vr.get("title", {}).get("runs", [{}])[0].get("text") or "")[:120]
                    except Exception:
                        title = ""
                    try:
                        up = (vr.get("ownerText", {}).get("runs", [{}])[0].get("text") or "")
                    except Exception:
                        up = ""
                    out.append({"id": vid, "title": title, "uploader": up,
                                "duration": dur,
                                "url": f"https://www.youtube.com/watch?v={vid}",
                                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"})
            for v in o.values():
                _walk(v)
        elif isinstance(o, list):
            for v in o:
                _walk(v)

    _walk(data)
    return out[:max_results]


async def search_youtube(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search YouTube and return simplified result list."""
    cache_key = f"search::{query}::{max_results}"
    cached    = _cache_get(cache_key)
    if cached:
        return cached

    # DB search cache (restart-proof) — same query dobara = zero YouTube hit
    try:
        from database import get_cached_search
        db_results = get_cached_search(query, max_results)
        if db_results:
            logger.debug(f"DB search HIT: {query[:50]}")
            _cache_set(cache_key, db_results)
            return db_results
    except Exception:
        pass

    opts = {
        "quiet":        True,
        "no_warnings":  True,
        "extract_flat": True,
        "noplaylist":   True,
        "socket_timeout": 15,
        "retries":      2,
        "extractor_retries": 1,
    }
    # 🍪 login cookies — search bhi guest-rate-limit/bot-check se bacha rahe
    _sck = _cookie_file()
    if _sck:
        opts["cookiefile"] = _sck

    def _do_search():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
            return info.get("entries", []) if info else []

    def _do_search_fallback():
        # 🌐 Fallback-1: web page search (different code-path — Innertube API
        # rate-limited ho to bhi results milte hain)
        fopts = dict(opts)
        fopts["extract_flat"] = "discard_in_playlist"
        with yt_dlp.YoutubeDL(fopts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/results?search_query={query}",
                download=False)
            return (info or {}).get("entries", []) or []

    loop    = asyncio.get_event_loop()

    # ⚡ Fast-path-1: innertube direct API (~0.3s) — yt-dlp ko touch hi nahi karta
    try:
        entries = await asyncio.wait_for(
            loop.run_in_executor(_EXECUTOR, _innertube_search_sync, query, max_results),
            timeout=6.0)
        if entries:
            logger.info(f"innertube fast search OK: {query[:40]}")
    except Exception as e:
        logger.warning(f"innertube search fail ({str(e)[:80]}) — race fallback")
        entries = None

    # ⚡ Fast-path-2: primary + fallback RACE — dono saath, jo pehle results de
    if not entries:
        async def _race():
            t1 = asyncio.ensure_future(loop.run_in_executor(_EXECUTOR, _do_search))
            t2 = asyncio.ensure_future(loop.run_in_executor(_EXECUTOR, _do_search_fallback))
            done, pending = await asyncio.wait({t1, t2}, timeout=14.0,
                                               return_when=asyncio.FIRST_COMPLETED)
            first_ok = None
            for t in done:
                try:
                    r = t.result()
                    if r and (first_ok is None or len(r) > len(first_ok)):
                        first_ok = r
                except Exception:
                    pass
            if not first_ok and pending:
                for p in pending:
                    try:
                        r = await asyncio.wait_for(p, timeout=8.0)
                        if r:
                            first_ok = r
                            break
                    except Exception:
                        pass
            for p in pending:
                p.cancel()
            return first_ok
        try:
            entries = await _race()
        except Exception as e:
            logger.warning(f"search race fail: {str(e)[:90]}")
    if not entries:
        raise ExtractionError(f"search failed for: {query[:80]}")

    results = []
    for e in entries:
        vid_id = e.get("id") or ""
        if not vid_id:
            continue
        results.append({
            "id":       vid_id,
            "title":    e.get("title", ""),
            "uploader": e.get("uploader") or e.get("channel", ""),
            "duration": e.get("duration"),
            "url":      f"https://www.youtube.com/watch?v={vid_id}",
            "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
        })

    _cache_set(cache_key, results)
    try:
        from database import cache_search
        cache_search(query, max_results, results)
    except Exception:
        pass
    return results



# ── Persistent innertube session (TCP+TLS reuse → search ~50ms) ──────────────
import threading as _th
_INNERTUBE_LOCK = _th.Lock()
_INNERTUBE_SESS = None


def _innertube_session():
    """Lazy singleton requests.Session (keep-alive pool) for innertube API."""
    global _INNERTUBE_SESS
    if _INNERTUBE_SESS is None:
        with _INNERTUBE_LOCK:
            if _INNERTUBE_SESS is None:
                try:
                    import requests as _requests
                    s = _requests.Session()
                    s.headers.update({
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                        "Origin": "https://www.youtube.com",
                        "Referer": "https://www.youtube.com/",
                    })
                    a = _requests.adapters.HTTPAdapter(
                        pool_connections=2, pool_maxsize=4, max_retries=1)
                    s.mount("https://", a)
                    _INNERTUBE_SESS = s
                except Exception:
                    _INNERTUBE_SESS = False
    return _INNERTUBE_SESS or None

