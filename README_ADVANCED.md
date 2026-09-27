# HellAPI — Advanced yt-dlp (2026) Setup

Tumhari VPS ke liye yt-dlp ka **pura 2026-grade anti-block stack**:

```
Request flow:
  /song → RAM cache → SQLite cache → Telegram store → [MISS] →
  yt-dlp (mweb/tv/web_embedded_safari clients → deno JS-challenge solver →
  bgutil PO-token provider) → proxy rotation → circuit breaker →
  IP-watchdog restart
```

## Kya-kya lagaya gaya (VPS pe already)

| Layer | Kya karta hai |
|---|---|
| **yt-dlp auto-update** (hourly) | YouTube har hafte kuch toota deta hai — latest yt-dlp sabse pehle fix karta hai |
| **bgutil PO-token provider** | Har request ke liye fresh PO token (bot-check ke bina stream URLs unlock) |
| **bgutil deno server** (tmux `pot`) | Token generation engine — deno 2.9 pe chalta hai |
| **deno JS runtime** | yt-dlp ke JS challenges solve karta hai |
| **Google Chrome 153** | `--cookies-from-browser` fallback ready |
| **Client fallback chain** | mweb → tv → web_embedded_safari → ios → default |
| **Adaptive client ordering** | Jo client last pass hua wo aage; chronic-fail piche |
| **Health-aware proxy rotation** | Dead/cooling proxies auto-skip, exponential cooldown |
| **Circuit breaker** | Consecutive blocks → fail-fast + Retry-After |
| **IP-watchdog** | Bar-bar block → runner.sh auto-restart (fresh connections) |
| **3-layer cache** | RAM 8min → SQLite 7din → Telegram DB channel (restart-proof) |
| **Smart stream cache** | googlevideo URL expiry-aware — repeat = instant, zero YT hits |

## VPS pe files/layout

```
/root/hellapi/
  runner.sh          # supervisor (flock single-instance, 5s loop)
  autoupdate.py      # hourly pip update + IP-watchdog
  oauth_setup.py     # (absolete — OAuth band ho gaya YT ne)
  cookies.txt        # ← TUMHE DENA HAI (browser se export)
  logs/api.log, logs/bot.log, logs/runner.log
tmux sessions: pot (bgutil server), + tumhare purane bots
```

## Cookies kaise dena hain (SABSE ZAROORI STEP)

VPS ka IP datacenter IP hai — YouTube wahan bina login ke bot-check karta hi hai.
Cookies = yt-dlp ka official recommended fix.

1. Apne PC pe Chrome me **youtube.com** login karo (chhota/secondary account best)
2. Extension install: **"Get cookies.txt LOCALLY"**
3. youtube.com pe extension icon → Export (Netscape) → `cookies.txt`
4. File mujhe de do → main `/root/hellapi/cookies.txt` pe daal kar activate kar dunga

Cookies ke bina bhi PO-token stack kaafi blocks khol dega, lekin cookies ke saath
yeh setup **near-bulletproof** ho jata hai.

## Admin endpoints (ADMIN_KEY se)

```
GET  /admin/update-status    # yt-dlp version, OAuth health, watchdog, last update
POST /admin/update-now       # force update (+ restart agar naya version aaya)
POST /admin/restart-services # manual restart (fresh IP state)
GET  /admin/proxy-status     # per-proxy health, circuit, hourly budget
POST /admin/proxy-reload     # .env me naye proxies add karne ke baad (bina restart)
```

## Speed tuning (already applied)

- `YTDLP_WORKERS=4` parallel extractions
- Stream URLs cached till their own expiry (~6h) — repeat songs ZERO YT hits
- In-flight coalescing — same video ke parallel requests ek hi extraction share
- Negative cache — dead videos 10 min instant-fail
- Search cache 24h DB me

## Agar future me phir block ho

1. `curl http://localhost:8000/admin/proxy-status?api_key=...` → circuit/hourly budget dekho
2. `tmux attach -t pot` → bgutil server logs dekho
3. `cd /root/hellapi && ./venv/bin/pip install -U yt-dlp` → manual update
4. Sab fail → cookies fresh export karo (14 din purani = stale)
