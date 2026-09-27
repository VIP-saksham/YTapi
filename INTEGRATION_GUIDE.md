# HellAPI — Dusre Bots ke liye Integration Guide

YouTube stream API jo **bina cookies, bina proxy** chalti hai. Free plan me
50 req/day, paid plans up to unlimited.

---

## 1. API Server start karo

```bash
cd youtube.api-main
python -m venv venv                              # pehli baar
venv\Scripts\pip install -r requirements.txt     # pehli baar
venv\Scripts\python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

- Swagger docs: `http://localhost:8000/docs`
- Server start hote hi khud `.env` load hota hai. `ADMIN_KEY` zaroor set karo.
- Status check: `curl http://localhost:8000/`

---

## 2. API key lo

Bot user Telegram bot se `/start` karta hai → free key milta hai:

```
HellAPIxxxxxxxxxxxxxxxxxxxxxxxx
```

Testing ke liye DB me seed kar sakte ho:

```bash
venv\Scripts\python -c "import database as db; db.upsert_user(123,'x','X'); print(db.create_key(123,'ultra'))"
```

---

## 3. youtube.py client use karo (recommended — drop-in)

Apne music bot ke folder me `youtube.py` copy karo (ye file hai `youtube.api-main/youtube.py`)
aur bot ke `.env` me add karo:

```env
HELLAPI_URL=http://localhost:8000
HELLAPI_KEY=HellAPIxxxxxxxxxxxxxxxxxxxxxxxx
```

Bas! Client khud handle karta hai: retries, connection pool, error mapping.

### YukkiMusic / AnonXMusic style (YouTubeAPI class):

```python
from youtube import YouTube

link = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# details
title, duration_min, duration_sec, thumb, vidid = await YouTube.details(link)

# stream URL (VC play ke liye)
path, success = await YouTube.download(link, mystic=None)        # audio file
path, success = await YouTube.download(link, mystic=None, video=True)  # video

# sirf stream URL chahiye?
data = await YouTube._api_get("/api/stream", {"url": link, "type": "audio"})
stream_url = data["stream_url"]
```

### Direct functions (simple bots):

```python
from youtube import download_song, download_video

audio_path = await download_song(link)   # → downloads/<videoid>.mp3
video_path = await download_video(link)  # → downloads/<videoid>.mp4
```

### Raw HTTP (koi bhi language/framework):

```
GET /song?url=VIDEO_URL&api_key=KEY
→ {"results":[{"title":"...","streamLink":"https://...googlevideo...","duration":"3:33",...}]}

GET /api/stream?url=VIDEO_URL&type=audio&api_key=KEY
→ {"ok":true,"stream_url":"https://...","title":"..."}
```

429 aaye to `Retry-After` header me seconds mile — uske baad hi retry karo.

---

## 4. Error handling (client khud karta hai, phir bhi jaan lo)

| HTTP | Matlab | Kya karo |
|------|--------|----------|
| 401 | Key invalid/expired | Naya key lo |
| 429 | Rate limit / YouTube block | `Retry-After` wait karo |
| 400 | Video unavailable/private | Song skip karo |
| 503 | Server busy | Thodi der baad retry |

---

## 5. Kya aapko cookies/proxy chahiye?

**Nahi** — default stack bina dono ke chalta hai:
- Smart caching → repeat song = instant, zero YouTube hit
- Hourly budget limiter (250/hr, YouTube ke 300 limit se neeche)
- Circuit breaker — block hone pe fail-fast, IP rest milta hai
- Adaptive clients + node JS runtime (already enabled)

Traffic badhne par (500+ req/day): cookies add karo (`.env` → `YTDLP_COOKIE_FILE=cookies.txt`).
Still blocked? Tabhi paid proxy consider karo (`PROXY_LIST`).
