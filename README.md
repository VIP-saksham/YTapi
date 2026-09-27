# HellAPI ⚡

Self-hosted YouTube extraction & streaming API — powers [KeyaraMusic](https://github.com/VIP-saksham/KeyaraMusic) with **~20ms cached stream URLs** and **sub-second search**.

## Features

- **`/api/stream`** — instant audio/video stream URLs (cached, race-extracted)
- **`/search`** — ⚡ innertube fast-path search (~0.5s cold, 20ms cached) + race fallback
- **`/info`**, **`/formats`**, **`/file/{id}`** — metadata, formats, proxied file streaming
- **Song library + search cache** — restart-proof SQLite persistence
- **Login cookies** — auto-detected from `cookies/`, no IP rate-limits
- **API key system** — Telegram bot (@HellAPI) se keys, plans, expiry, usage tracking
- **OAuth2 visitor data** rotation for extraction resilience
- **Auto-update** heartbeat + systemd service files included

## Quick start

```bash
bash vps_setup.sh        # one-shot: venv + .env + systemd + keys
# ya manually:
pip install -r requirements.txt
cp .env.example .env     # fill ADMIN_KEY, TELEGRAM_BOT_TOKEN
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Usage

```
GET /api/stream?url=YOUTUBE_URL&type=audio&api_key=YOUR_KEY
GET /search?q=song+name&limit=5&api_key=YOUR_KEY
GET /info?url=YOUTUBE_URL&api_key=YOUR_KEY
```

## Files

| File | Purpose |
|:---|:---|
| `main.py` | FastAPI app — all endpoints |
| `ytdlp.py` | yt-dlp extraction engine + innertube fast search |
| `database.py` | SQLite — keys, usage, caches, song library |
| `tgbot.py` | Telegram key-management bot |
| `fallback.py` / `speedup.py` | resilience + speed helpers |
| `autoupdate.py` | self-update + OAuth health |
| `vps_setup.sh` | one-shot VPS deployment |

---

© 2026 Saksham Swaroop (@truenakshu) · [GitHub: VIP-saksham](https://github.com/VIP-saksham) · All rights reserved.
