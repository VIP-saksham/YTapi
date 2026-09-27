"""
oauth_setup.py
--------------
ONE-TIME YouTube OAuth2 device login for yt-dlp (ip-block-free extraction).

Plugin ka internal polling loop current Google endpoint pe toota hai
(HTTP 400 body vs status confusion) — isliye ye script device flow khud
chalati hai (same TV client_id/secret) aur token yt-dlp cache me
direct store karti hai. Plugin wahi cache padhta hai → automatic login.

Kaise chalaye (VPS pe, tmux me):
    cd /root/hellapi
    tmux new -s oauth
    ./venv/bin/python oauth_setup.py
1. Device code dikhega:  XXX-YYY-ZZZ
2. Phone/laptop pe https://www.google.com/device kholo
3. Code daalo → Google account choose karo (chhota/naya account best) → Allow
4. Script khud token cache kar dega aur 75s ke andar SUCCESS dikha dega
5. .env me: YTDLP_OAUTH=1 → services restart
"""

import os
import sys
import json
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

# yt-dlp-youtube-oauth2 plugin ke constants (YouTube TV client)
CLIENT_ID     = "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com"
CLIENT_SECRET = "SboVhoG9s0rNafixCSGGKXAT"
SCOPES        = "https://gdata.youtube.com https://www.googleapis.com/auth/youtube"

DEVICE_URL = "https://www.youtube.com/o/oauth2/device/code"
TOKEN_URL  = "https://www.youtube.com/o/oauth2/token"


def yt_dlp_cachedir() -> str:
    """yt-dlp ka default cache dir (Linux: ~/.cache/yt-dlp)."""
    xdg = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    return os.path.join(xdg, "yt-dlp")


def store_token(token_data: dict):
    """Token yt-dlp cache me store — plugin isi ko padhta hai."""
    import yt_dlp
    opts = {"cachedir": yt_dlp_cachedir(), "quiet": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.cache.store("youtube-oauth2", "token_data", token_data)


def main():
    print("=" * 60)
    print("YouTube OAuth2 Device Login — yt-dlp (direct device flow)")
    print("=" * 60)
    print()

    # 1. Device code request
    r = requests.post(DEVICE_URL, timeout=30, json={
        "client_id":    CLIENT_ID,
        "scope":        SCOPES,
        "device_id":    uuid.uuid4().hex,
        "device_model": "ytlr::",
    })
    r.raise_for_status()
    code = r.json()

    print(f"  1. Phone/laptop pe kholo:  {code['verification_url']}")
    print(f"  2. Ye code daalo:          {code['user_code']}")
    print(f"  3. Google account choose karo → Allow")
    print()
    print(f"(Code {code.get('expires_in', 1800)//60} min valid — main approve hone ka wait kar raha hoon...)")
    print()

    # 2. Poll — jab tak approve na ho
    interval = int(code.get("interval", 5))
    deadline = time.time() + int(code.get("expires_in", 1800))
    token_data = None
    while time.time() < deadline:
        time.sleep(interval)
        try:
            pr = requests.post(TOKEN_URL, timeout=30, json={
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "device_code":   code["device_code"],   # Google ab 'device_code' maangta hai, 'code' nahi
                "grant_type":    "urn:ietf:params:oauth:grant-type:device_code",
            })
            data = pr.json()          # 400 body me error hota hai — .json() safe
        except Exception as e:
            print(f"   network hiccup: {e} — retry", flush=True)
            continue

        err = data.get("error")
        if err == "authorization_pending":
            print(f"   ...waiting approval ({time.strftime('%H:%M:%S')})", flush=True)
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err == "expired_token":
            print("❌ Code expire ho gaya — script dobara chalao.")
            return
        if err:
            print(f"❌ Login fail: {err}")
            return

        token_data = {
            "access_token":  data["access_token"],
            "expires":       time.time() + int(data["expires_in"]),
            "refresh_token": data["refresh_token"],
            "token_type":    data["token_type"],
        }
        break

    if not token_data:
        print("⏳ Time up — script dobara chalao.")
        return

    # 3. Token cache
    store_token(token_data)
    print()
    print("✅ SUCCESS — OAuth token cache ho gaya!")
    print(f"   File: {yt_dlp_cachedir()}/youtube-oauth2/token_data.json")
    print()
    print("Ab ye commands chalao (ya mujhe bolo — main kar dunga):")
    print("   sed -i 's/^YTDLP_OAUTH=.*/YTDLP_OAUTH=1/' /root/hellapi/.env")
    print("   pkill -f 'uvicorn main:app'; pkill -f 'python tgbot.py'")


if __name__ == "__main__":
    main()
