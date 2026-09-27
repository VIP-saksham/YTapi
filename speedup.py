"""
speedup.py
----------
HellAPI — PO-token **warm session pre-generator**.

Kya karta hai (background daemon, har 40 min):
  - bgutil server (127.0.0.1:4416) ko hit karke ek token pre-generate karta hai
    → jab real request aaye, server ka BotGuard session already warm hota hai
    → token generation ~1-2s hota hai pehli baar, warm pe <300ms
  - Server down ho to dobara launch try karta hai (deno, tmux ke bina)
  - Token generation ka latency + success/fail stats track karta hai

Env:
  POT_SERVER_URL   = http://127.0.0.1:4416 (default)
  POT_WARM_INTERVAL= 2400 (seconds, 40 min)
"""

import os
import time
import logging
import threading

logger = logging.getLogger(__name__)

POT_SERVER_URL    = os.getenv("POT_SERVER_URL", "http://127.0.0.1:4416").rstrip("/")
POT_WARM_INTERVAL = int(os.getenv("POT_WARM_INTERVAL", "2400"))

_state = {
    "last_warm_at":   None,
    "last_warm_ms":   None,
    "last_ok":        None,
    "total_ok":       0,
    "total_fail":     0,
    "server_restarts": 0,
}
_lock = threading.Lock()
_started = False


def warm_once() -> dict:
    """bgutil server ko hit karke warm session + latency measure."""
    import httpx
    t0 = time.time()
    ok = False
    ms = 0
    try:
        r = httpx.get(f"{POT_SERVER_URL}/ping", timeout=5)
        if r.status_code == 200:
            t1 = time.time()
            # Warm session: token generation trigger (server ka in-memory
            # minter cache warm rehta hai — real request <300ms token payegi)
            tr = httpx.post(f"{POT_SERVER_URL}/get_pot",
                            headers={"Content-Type": "application/json"},
                            content="{}", timeout=30)
            ok = tr.status_code == 200
            ms = int((time.time() - t1) * 1000)
        else:
            ms = int((time.time() - t0) * 1000)
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        logger.warning(f"pot_warm fail: {e}")
    with _lock:
        _state["last_warm_at"] = time.strftime("%H:%M:%S")
        _state["last_warm_ms"] = ms
        _state["last_ok"] = ok
        if ok:
            _state["total_ok"] += 1
        else:
            _state["total_fail"] += 1
    return {"ok": ok, "ms": ms}


def _ensure_server() -> bool:
    """bgutil server down ho to deno se wapas launch karo."""
    import httpx
    try:
        r = httpx.get(f"{POT_SERVER_URL}/ping", timeout=3)
        return r.status_code == 200
    except Exception:
        pass
    # down hai — deno server relaunch (nohup, tmux ki zaroorat nahi)
    logger.warning("bgutil server down — relaunching...")
    import subprocess
    subprocess.Popen(
        ["deno", "run", "--allow-env", "--allow-net", "--allow-ffi",
         "--allow-write", "--allow-read", "src/main.ts"],
        cwd="/root/bgutil-ytdlp-pot-provider/server",
        stdout=open("/root/hellapi/logs/pot_server.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(6)
    with _lock:
        _state["server_restarts"] += 1
    try:
        r = httpx.get(f"{POT_SERVER_URL}/ping", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _daemon():
    logger.info(f"pot_warm daemon started — every {POT_WARM_INTERVAL}s → {POT_SERVER_URL}")
    while True:
        try:
            if _ensure_server():
                warm_once()
        except Exception as e:
            logger.error(f"pot_warm daemon error: {e}")
        time.sleep(POT_WARM_INTERVAL)


def start_daemon():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_daemon, name="pot_warm", daemon=True).start()


def get_status() -> dict:
    with _lock:
        s = dict(_state)
    s["server_url"] = POT_SERVER_URL
    s["interval_sec"] = POT_WARM_INTERVAL
    s["server_up"] = None
    try:
        import httpx
        r = httpx.get(f"{POT_SERVER_URL}/ping", timeout=2)
        s["server_up"] = r.status_code == 200
        if r.status_code == 200:
            s["server_version"] = r.json().get("version")
            s["server_uptime"] = round(r.json().get("server_uptime", 0))
    except Exception:
        s["server_up"] = False
    return s
