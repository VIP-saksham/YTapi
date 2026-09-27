#!/usr/bin/env bash
# HellAPI v2 — VPS one-shot setup: zip download, deploy, systemd services
# Usage: bash vps_setup.sh
set -e

echo "[1/7] Downloading HellAPI v2..."
cd /root
wget -q https://files.catbox.moe/REPLACE_ME.zip -O hellapi_v2.zip || {
  echo "!! UPLOAD hellapi_v2.zip to /root/ manually, then re-run this script with SKIP_DL=1"
  [ "${SKIP_DL}" = "1" ] || exit 1
}
[ -f hellapi_v2.zip ] && unzip -oq hellapi_v2.zip -d /root

echo "[2/7] HellAPI venv + deps..."
cd /root/hellapi
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

echo "[3/7] .env (skip if already exists)"
if [ ! -f .env ]; then
  cat > .env <<'EOF'
APP_NAME=HellAPI
PORT=8000
APP_URL=http://localhost:8000
API_BASE_URL=http://localhost:8000
ADMIN_KEY=hellapi_admin_2026_strong
ALLOW_SEARCH=true
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN_HERE
BOT_NAME=HellAPI
ADMIN_IDS=
YTDLP_JS_RUNTIME=node
YTDLP_HOURLY_LIMIT=250
YT_CB_THRESHOLD=3
YT_CB_COOLDOWN_SEC=120
PROXY_LIST=
YTDLP_COOKIE_FILE=
EOF
fi

echo "[4/7] Seed API key..."
./venv/bin/python - <<'PYEOF'
import database as db
db.upsert_user(8538341106, "yash", "Yash")
with db.get_conn() as conn:
    conn.execute("""
        INSERT INTO api_keys (key, user_id, plan, expires_at, is_active)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(key) DO UPDATE SET is_active=1, expires_at=excluded.expires_at
    """, ("YOUR_ADMIN_KEY_HERE", 8538341106, "ultra", "2027-09-19 00:00:00"))
print("key seeded OK")
PYEOF

echo "[5/7] Kill old processes + stop old services..."
pkill -9 -f "uvicorn main:app" 2>/dev/null || true
pkill -9 -f SHUKLAMUSIC 2>/dev/null || true
sleep 2

echo "[6/7] systemd services install..."
cp hellapi-api.service /etc/systemd/system/
cp hellapi-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hellapi-api
sleep 6
systemctl enable --now hellapi-bot

echo "[7/7] Health checks..."
curl -s -o /dev/null -w "HellAPI: %{http_code}\n" http://localhost:8000/
KEY=YOUR_ADMIN_KEY_HERE
curl -s -o /dev/null -w "Song: %{http_code}\n" "http://localhost:8000/song?url=dQw4w9WgXcQ&api_key=$KEY"
curl -s -o /dev/null -w "Search: %{http_code}\n" "http://localhost:8000/search?q=test&api_key=$KEY"
systemctl is-active hellapi-api hellapi-bot

echo ""
echo "=== DONE ==="
echo "API logs:    journalctl -u hellapi-api -f"
echo "Bot logs:    journalctl -u hellapi-bot -f"
echo "Music bot ko group me add karke /play test karo"
echo ""
echo "=== Optional: Telegram DB channel (VPS-death-proof cache) ==="
echo "1. Private channel banao, bot (@youtubeapiiiibot) ko admin banao"
echo "2. Channel me ek message bhejo"
echo "3. cd /root/hellapi && ./venv/bin/python db_channel_setup.py"
echo "4. systemctl restart hellapi-api"
