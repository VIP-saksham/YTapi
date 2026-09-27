"""
autoupdate.py
-------------
HellAPI — Auto-update + Auth watchdog daemon.

Background daemon (hourly) jo ye karta hai:
  1. yt-dlp auto-update — pip se latest version. YouTube extraction tootta hai
     jab YT UI/JS change hota hai; latest yt-dlp usse fastest fix karta hai.
  2. OAuth2 token health check — token file missing/stale to warning log.
  3. IP-block watchdog — YouTube server IP baar-baar block kar raha ho
     (circuit breaker repeatedly open) to services restart karta hai
     (YT_UPDATE_RESTART_CMD se) — fresh connections, fresh IP-side state.

Env vars:
  YTDLP_AUTO_UPDATE          = 1/0   (default 1)
  YTDLP_UPDATE_INTERVAL      = 3600  (seconds)
  YTDLP_PIN_VERSION          = specific version pe pin (khaali = latest)
  YT_AUTO_RESTART_ON_BLOCK   = 1/0   (default 1)
  YT_RESTART_BLOCK_THRESHOLD = 2     (consecutive circuit-opens before restart)
  YT_RESTART_COOLDOWN_SEC    = 900   (min seconds between restarts)
  YT_UPDATE_RESTART_CMD      = restart command (default: systemctl restart hellapi-api hellapi-bot)
"""

import os
import sys
import time
import re
import logging
import threading
import subprocess
from datetime import datetime

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# ── Config ───────────────────────────────────────────────────────────────────
UPDATE_ENABLED      = _env_bool("YTDLP_AUTO_UPDATE", "1")
UPDATE_INTERVAL_SEC = int(os.getenv("YTDLP_UPDATE_INTERVAL", "3600"))
PIN_VERSION         = os.getenv("YTDLP_PIN_VERSION", "").strip()
RESTART_ON_BLOCK    = _env_bool("YT_AUTO_RESTART_ON_BLOCK", "1")
RESTART_THRESHOLD   = int(os.getenv("YT_RESTART_BLOCK_THRESHOLD", "2"))
RESTART_COOLDOWN    = int(os.getenv("YT_RESTART_COOLDOWN_SEC", "900"))

_DEFAULT_RESTART_CMD = (
    "systemctl restart hellapi-api hellapi-bot"
    if os.path.exists("/etc/systemd/system/hellapi-api.service")
    else "touch /root/hellapi/.restart_flag"   # container: runner.sh flag dekh ke restart karega
)
RESTART_CMD = os.getenv("YT_UPDATE_RESTART_CMD", _DEFAULT_RESTART_CMD)

# ── State ────────────────────────────────────────────────────────────────────
_lock              = threading.Lock()
_daemon_started    = False
_last_update_check = 0.0
_last_update_result: dict = {}
_restart_history: list = []     # timestamps
_block_events      = 0


def _run(cmd, shell: bool = False, timeout: int = 300):
    """Subprocess run karke (ok, combined_output) wapas do."""
    try:
        r = subprocess.run(
            cmd, shell=shell, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        return r.returncode == 0, ((r.stdout or "") + (r.stderr or ""))
    except Exception as e:
        return False, str(e)


# ── yt-dlp version helpers ──────────────────────────────────────────────────

def _installed_version() -> str:
    try:
        import yt_dlp
        return yt_dlp.version.__version__
    except Exception:
        return "unknown"


def _norm_ver(v: str):
    """'2026.08.19' aur '2026.8.19' ko same banao (pip zero-padding alag likhta hai)."""
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return v


def _latest_version() -> str:
    """PyPI se latest yt-dlp version (pip index — halka network hit)."""
    ok, out = _run(
        [sys.executable, "-m", "pip", "index", "versions", "yt-dlp"],
        timeout=60,
    )
    if ok:
        m = re.search(r"yt-dlp \(([^)]+)\)", out)
        if m:
            return m.group(1)
    return ""


def update_ytdlp(force: bool = False) -> dict:
    """
    yt-dlp ko latest (ya pinned) version pe update karo.
    Return: {updated, old_version, new_version, message, at}
    """
    old = _installed_version()

    if PIN_VERSION:
        if not force and _norm_ver(old) == _norm_ver(PIN_VERSION):
            result = {"updated": False, "old_version": old, "new_version": old,
                      "message": f"pinned at {old} — skip"}
        else:
            result = _pip_install(f"yt-dlp=={PIN_VERSION}", old)
    else:
        target = _latest_version()
        if not target:
            result = {"updated": False, "old_version": old, "new_version": old,
                      "message": "latest version check fail — skip"}
        elif not force and _norm_ver(old) == _norm_ver(target):
            result = {"updated": False, "old_version": old, "new_version": old,
                      "message": f"already latest ({old})"}
        else:
            result = _pip_install("yt-dlp" + (f"=={target}" if target else ""), old)

    result["at"] = datetime.now().isoformat(timespec="seconds")
    with _lock:
        global _last_update_check, _last_update_result
        _last_update_check = time.time()
        _last_update_result = result
    logger.info(f"yt-dlp update check: {result['message']}")
    return result


def _pip_install(spec: str, old_version: str) -> dict:
    logger.info(f"yt-dlp update: {old_version} → {spec}...")
    ok, out = _run(
        [sys.executable, "-m", "pip", "install", "--upgrade", spec, "-q"],
        timeout=600,
    )
    new = _installed_version()
    if ok and new != old_version:
        msg = f"updated: {old_version} → {new}"
        logger.info(f"✅ yt-dlp {msg}")
    elif ok:
        msg = f"pip ok, version same ({new})"
    else:
        msg = f"update FAILED: {out[-300:]}"
        logger.warning(f"yt-dlp {msg}")
    return {
        "updated":     bool(ok and new != old_version),
        "old_version": old_version,
        "new_version": new,
        "message":     msg,
    }


# ── IP-block watchdog (bot auto-restart) ─────────────────────────────────────

def notify_ip_block_event(reason: str = ""):
    """
    ytdlp.py circuit-open hone par call karta hai.
    Threshold consecutive events → services restart (cooldown ke saath).
    """
    global _block_events
    if not RESTART_ON_BLOCK:
        return False
    with _lock:
        _block_events += 1
        events = _block_events
        if _restart_history and time.time() - _restart_history[-1] < RESTART_COOLDOWN:
            return False        # cooldown me — skip
        if events < RESTART_THRESHOLD:
            return False        # abhi itne events nahi hue
        _block_events = 0
        _restart_history.append(time.time())
        del _restart_history[:-20]      # in-place trim (global rebind se bacho)
    _do_restart(f"{RESTART_THRESHOLD} consecutive circuit-opens. Last: {reason[:120]}")
    return True


def _do_restart(reason: str):
    logger.warning(f"♻ AUTO-RESTART: {reason}")
    logger.warning(f"   cmd: {RESTART_CMD}")
    ok, out = _run(RESTART_CMD, shell=True, timeout=60)
    logger.warning(f"   restart ok={ok} out={out[:200]}")
    # Container mode: flag file lag gayi — runner.sh 5s ke andar restart karega.
    # API process marega to ye process bhi saath me jayegi (expected).
    if ".restart_flag" in RESTART_CMD:
        logger.warning("   container mode — runner.sh restart karega (5s)")


# ── OAuth2 token health ──────────────────────────────────────────────────────

def oauth_health() -> dict:
    """OAuth2 token cache file ka status — missing/stale to warning."""
    oauth_on = os.getenv("YTDLP_OAUTH", "").strip().lower() in ("1", "true", "yes", "on")
    base = os.getenv(
        "XDG_CONFIG_HOME",
        os.path.expanduser("~/.config"),
    )
    cache_file = os.path.join(base, "yt-dlp", "youtube_oauth2.json")
    if not os.path.isfile(cache_file):
        return {
            "configured": oauth_on,
            "has_token":  False,
            "warning": ("OAuth enabled but token file nahi mili — "
                        "oauth_setup.py ek baar chalao")
                       if oauth_on else None,
        }
    age_days = (time.time() - os.path.getmtime(cache_file)) / 86400
    return {
        "configured":     oauth_on,
        "has_token":      True,
        "token_age_days": round(age_days, 1),
        "cache_file":     cache_file,
        "healthy":        age_days < 60,
        "warning": (f"token {int(age_days)} din purana — oauth_setup.py dobara chalao")
                   if age_days >= 60 else None,
    }


# ── Status (admin endpoint ke liye) ──────────────────────────────────────────

def get_update_status() -> dict:
    with _lock:
        return {
            "auto_update_enabled": UPDATE_ENABLED,
            "update_interval_sec": UPDATE_INTERVAL_SEC,
            "installed_version":   _installed_version(),
            "pin_version":         PIN_VERSION or None,
            "last_check":          (datetime.fromtimestamp(_last_update_check).isoformat()
                                    if _last_update_check else None),
            "last_result":         _last_update_result,
            "ip_watchdog": {
                "enabled":        RESTART_ON_BLOCK,
                "block_events":   _block_events,
                "threshold":      RESTART_THRESHOLD,
                "cooldown_sec":   RESTART_COOLDOWN,
                "restart_count":  len(_restart_history),
                "last_restart":   (datetime.fromtimestamp(_restart_history[-1]).isoformat()
                                   if _restart_history else None),
            },
            "oauth":               oauth_health(),
        }


def force_update_and_restart() -> dict:
    """Admin endpoint: force update + (updated ho to) services restart."""
    res = update_ytdlp(force=True)
    if res["updated"]:
        _do_restart("admin-triggered update restart")
        res["restarted"] = True
    else:
        res["restarted"] = False
    return res


# ── Daemon loop ──────────────────────────────────────────────────────────────

def _daemon():
    logger.info(f"autoupdate daemon started — interval {UPDATE_INTERVAL_SEC}s, "
                f"ip-watchdog={'on' if RESTART_ON_BLOCK else 'off'}")
    # Startup pe ek turant check (ye thread halka hai — pip index sirf)
    try:
        if UPDATE_ENABLED:
            update_ytdlp(force=False)
    except Exception as e:
        logger.error(f"autoupdate initial check fail: {e}")
    while True:
        time.sleep(UPDATE_INTERVAL_SEC)
        try:
            if UPDATE_ENABLED:
                update_ytdlp(force=False)
            oh = oauth_health()
            if oh.get("warning"):
                logger.warning(f"OAuth health: {oh['warning']}")
        except Exception as e:
            logger.error(f"autoupdate daemon error: {e}")


def start_daemon():
    """Background thread start (idempotent — main.py aur tgbot.py dono se safe)."""
    global _daemon_started
    with _lock:
        if _daemon_started:
            return
        _daemon_started = True
    threading.Thread(target=_daemon, name="autoupdate", daemon=True).start()
