"""
proxyrefresh.py
---------------
HellAPI — free proxy pool refresher (self-reliant extraction ke liye).

Public sources se HTTP proxies fetch karo, har ek ko yt-dlp simulate pe test
karo (extraction bot-check ke bina chalni chahiye), jo paas ho unhe PROXY_LIST
me daal ke runtime pool reload karo. Runtime-only: .env file touch nahi hota.

Admin:  POST /admin/refresh-proxies
"""
import concurrent.futures as cf
import os
import subprocess
import sys

import httpx

_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=4000&ssl=yes&anonymity=elite",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://proxylist.geonode.com/api/proxy-list?limit=50&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps&filter_up_time=60",
]
_TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"   # "Me at the zoo" — chhota, hamesha live
_WORKERS = int(os.getenv("PROXY_REFRESH_WORKERS", "14"))
_MAX_CANDIDATES = int(os.getenv("PROXY_REFRESH_MAX", "50"))


def _fetch_candidates() -> list:
    out, seen = [], set()
    for src in _SOURCES:
        try:
            r = httpx.get(src, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if src.startswith("https://proxylist.geonode"):
                for p in r.json().get("data", []):
                    out.append(f'{p["ip"]}:{p["port"]}')
            else:
                out += [x.strip().replace("http://", "") for x in r.text.splitlines()
                        if x.strip() and ":" in x and not x.startswith("#")]
        except Exception:
            continue
    uniq = []
    for p in out:
        p = p.strip().split("://")[-1]
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq[:_MAX_CANDIDATES]


def _ytdlp_bin() -> str:
    return os.path.join(os.path.dirname(sys.executable), "yt-dlp")


def _test(proxy: str):
    try:
        r = subprocess.run(
            [_ytdlp_bin(), "--proxy", f"http://{proxy}", "--simulate",
             "--print", "%(title)s", "--no-warnings", "--socket-timeout", "8",
             "--retries", "0", _TEST_URL],
            capture_output=True, text=True, timeout=35)
        t = (r.stdout or "").strip()
        return (proxy, t[:70]) if (t and r.returncode == 0) else (proxy, None)
    except Exception:
        return proxy, None


def refresh(max_candidates: int = 0) -> dict:
    """Fetch → test → pool update. Stats dict return karo."""
    import ytdlp
    candidates = _fetch_candidates()
    if max_candidates:
        candidates = candidates[:max_candidates]
    tested, working = 0, []
    with cf.ThreadPoolExecutor(max_workers=_WORKERS) as ex:
        for proxy, title in ex.map(_test, candidates):
            tested += 1
            if title:
                working.append(proxy)
    if working:
        current = ytdlp.get_proxy_pool()
        # fresh proven pehle, phir purane (jo abhi working me nahi the)
        merged = list(dict.fromkeys(working + [p for p in current if p not in working]))
        os.environ["PROXY_LIST"] = ",".join(merged[:40])
        # downloads bhi working proxy se jayen (signed-URL problem)
        os.environ["DOWNLOAD_PROXY"] = f"http://{working[0]}"
        ytdlp.reload_proxies()
    return {
        "tested": tested,
        "working": len(working),
        "pool_size": len(ytdlp.get_proxy_pool()),
        "sources": len(_SOURCES),
    }


# ── Hourly auto-refresh daemon ──────────────────────────────────────────
_daemon_started = False


def _daemon_loop():
    import time as _time
    import logging
    lg = logging.getLogger("proxyrefresh")
    interval = int(os.getenv("PROXY_REFRESH_INTERVAL_SEC", "3600"))
    _time.sleep(90)   # startup ke thodi der baad pehla refresh
    while True:
        try:
            stats = refresh()
            lg.info(f"proxy auto-refresh: {stats}")
        except Exception as e:
            lg.warning(f"proxy auto-refresh fail: {e}")
        _time.sleep(interval)


def start_daemon():
    """Background thread — har ghante pool refresh (env se on/off)."""
    global _daemon_started
    if _daemon_started or os.getenv("PROXY_AUTO_REFRESH", "1").lower() in ("0", "false", "off"):
        return
    import threading
    threading.Thread(target=_daemon_loop, daemon=True,
                     name="proxy-refresh").start()
    _daemon_started = True
