#!/usr/bin/env python3
"""
autoagent.py — KeyaraAPI Self-Upgrade Agent (always-alive)

Kya karta hai (har 30 min):
  1. API ka health snapshot collect karta hai (uptime, cache size, library, latency).
  2. OpenAI-compatible LLM ko (localhost:3000 → fallback nvapi → hellupdates)
     performance suggestions mangta hai — STRICT JSON schema me.
  3. SAFE suggestions auto-apply karta hai (cache TTL, concurrency, worker count).
  4. Report Telegram log group me bhejta hai + local history me save.

Env:
  LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
  TELEGRAM_LOG_TOKEN / TELEGRAM_LOG_CHAT
  AGENT_APPLY=1 (safe-apply on) / AGENT_INTERVAL (default 1800s)
"""
import os, sys, time, json, sqlite3, traceback, subprocess
from pathlib import Path

sys.path.insert(0, "/root/nakshu/keyaraapi")
os.chdir("/root/nakshu/keyaraapi")

# ── env ─────────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

LLM_URL   = os.getenv("LLM_BASE_URL", "http://localhost:3000/v1").rstrip("/")
LLM_KEY   = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "claude-fable-5")
TG_TOKEN  = os.getenv("TELEGRAM_LOG_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT   = os.getenv("TELEGRAM_LOG_CHAT", "") or os.getenv("LOGGER_ID", "")
APPLY     = os.getenv("AGENT_APPLY", "1") == "1"
INTERVAL  = int(os.getenv("AGENT_INTERVAL", "1800"))
HIST      = Path("/root/nakshu/keyaraapi/agent_history.log")
ENV_PATH  = "/root/nakshu/keyaraapi/.env"

import requests

# ── LLM clients (fallback chain) ────────────────────────────────────────────
PROVIDERS = [
    ("local", LLM_URL, LLM_KEY, LLM_MODEL),
    ("nvapi", "https://integrate.api.nvidia.com/v1",
     os.getenv("NVIDIA_API_KEY", ""), "meta/llama-3.3-70b-instruct"),
    ("hellupdates", "https://api.hellupdates.in/v1",
     os.getenv("HELLUPDATES_API_KEY", ""), "gpt-4o-mini"),
]

def llm_chat(system: str, user: str, timeout: int = 45) -> str:
    for name, base, key, model in PROVIDERS:
        if not key or not base:
            continue
        try:
            r = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "temperature": 0.3, "max_tokens": 500,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user}]},
                timeout=timeout)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
        except Exception:
            continue
    return ""

# ── snapshot ────────────────────────────────────────────────────────────────
def snapshot() -> dict:
    s = {"ts": time.strftime("%F %T"), "api": "up"}
    try:
        t0 = time.time()
        r = requests.get("http://localhost:8181/info",
                         params={"url": "https://www.youtube.com/watch?v=kJQP7kiw5Fk",
                                 "api_key": os.getenv("HELLAPI_KEY", "")}, timeout=15)
        s["info_ms"] = round((time.time()-t0)*1000)
        s["info_ok"] = r.status_code == 200
    except Exception as e:
        s["api"] = f"down: {e}"
    try:
        c = sqlite3.connect("bot_data.db")
        s["lib_songs"] = c.execute("SELECT COUNT(*) FROM song_library").fetchone()[0]
        s["active_keys"] = c.execute(
            "SELECT COUNT(*) FROM api_keys WHERE expires_at > datetime('now')").fetchone()[0]
        c.close()
    except Exception:
        try:
            c = sqlite3.connect("bot_data.db")
            tabs = [x[0] for x in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            s["tables"] = tabs[:10]
            c.close()
        except Exception:
            pass
    try:
        s["cache_entries"] = len([
            l for l in open("logs/api.log", errors="ignore").readlines()[-5000:]
            if "200 OK" in l])
    except Exception:
        pass
    return s

# ── safe-apply engine ───────────────────────────────────────────────────────
SAFE_ENV = {
    "YTDLP_CACHE_TTL": (120, 3600),
    "YTDLP_CACHE_SIZE": (128, 4096),
    "YTDLP_STREAM_TTL_CAP_MIN": (30, 720),
    "YTDLP_NEG_TTL_SEC": (60, 3600),
    "AGENT_INTERVAL": (600, 7200),
}

def apply_suggestion(k: str, v) -> str:
    if k not in SAFE_ENV:
        return f"skip {k} (not whitelisted)"
    lo, hi = SAFE_ENV[k]
    try:
        v = int(v)
    except Exception:
        return f"skip {k} (non-int)"
    if not (lo <= v <= hi):
        return f"skip {k}={v} (out of range {lo}-{hi})"
    lines = open(ENV_PATH, encoding="utf-8").read().splitlines()
    out, found = [], False
    for l in lines:
        if l.startswith(f"{k}="):
            out.append(f"{k}={v}")
            found = True
        else:
            out.append(l)
    if not found:
        out.append(f"{k}={v}")
    open(ENV_PATH, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    return f"APPLIED {k}={v}"

def needs_restart(new_vals: dict) -> bool:
    restart_keys = {"YTDLP_CACHE_TTL", "YTDLP_CACHE_SIZE",
                    "YTDLP_STREAM_TTL_CAP_MIN", "YTDLP_NEG_TTL_SEC"}
    return any(k in restart_keys for k in new_vals)

def restart_api():
    subprocess.run("pkill -9 -f 'uvicorn main:app'", shell=True, capture_output=True)
    time.sleep(8)

# ── telegram log ────────────────────────────────────────────────────────────
def tg_log(text: str):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text[:3500],
                  "parse_mode": "HTML"}, timeout=15)
    except Exception:
        pass

# ── main cycle ──────────────────────────────────────────────────────────────
SYSTEM = """You are the self-tuning performance agent of KeyaraAPI — a self-hosted
YouTube extraction API. Given a metrics snapshot, respond with STRICT JSON only:
{"analysis": "<one short line>", "suggestions": [{"key": "<ENV_VAR>", "value": <int>,
"reason": "<short>"}]}
Allowed keys: YTDLP_CACHE_TTL, YTDLP_CACHE_SIZE, YTDLP_STREAM_TTL_CAP_MIN,
YTDLP_NEG_TTL_SEC, AGENT_INTERVAL. Only suggest when a metric looks off. Max 3 suggestions.
No prose outside JSON."""

def cycle():
    snap = snapshot()
    prompt = ("KeyaraAPI metrics snapshot:\n" + json.dumps(snap, indent=1) +
              "\n\nSuggest safe tuning (JSON only). If all healthy, return "
              '{"analysis":"healthy","suggestions":[]}')
    raw = llm_chat(SYSTEM, prompt)
    applied, notes = [], []
    if raw:
        try:
            m = json.loads(raw[raw.find("{"):raw.rfind("}")+1])
            analysis = m.get("analysis", "")
            for s in (m.get("suggestions") or [])[:3]:
                k, v = s.get("key"), s.get("value")
                res = apply_suggestion(k, v) if APPLY else f"DRY {k}={v}"
                applied.append(res)
        except Exception as e:
            analysis = f"parse-fail: {e}"
    else:
        analysis = "no LLM reachable (local:3000 down, no fallback keys)"

    restart = needs_restart({a.split()[1].split("=")[0]: 0
                             for a in applied if a.startswith("APPLIED")})
    if restart and APPLY:
        restart_api()

    rep = (f"🤖 <b>KeyaraAPI Self-Upgrade Agent</b>\n"
           f"⏰ {snap['ts']}\n"
           f"📊 <b>Snapshot:</b> api={snap.get('api')}, info={snap.get('info_ms','?')}ms, "
           f"lib={snap.get('lib_songs','?')} songs, keys={snap.get('active_keys','?')}\n"
           f"🧠 <b>AI:</b> {analysis}\n"
           f"⚙️ <b>Applied:</b> {'; '.join(applied) if applied else 'nothing (all healthy)'}\n"
           f"🔄 Restart: {'YES (settings reloaded)' if restart else 'no'}")
    tg_log(rep)
    with open(HIST, "a", encoding="utf-8") as f:
        f.write(f"\n[{snap['ts']}]\n{rep}\n")

if __name__ == "__main__":
    tg_log("🤖 <b>KeyaraAPI Self-Upgrade Agent</b> started — always-alive, "
           f"interval {INTERVAL}s, apply={APPLY}, local LLM: {LLM_URL}")
    while True:
        try:
            cycle()
        except Exception:
            with open(HIST, "a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%F %T')}] CYCLE CRASH:\n{traceback.format_exc()}\n")
        time.sleep(INTERVAL)
