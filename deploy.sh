#!/usr/bin/env bash
# deploy.sh — VPS PE CHALNE WALA deploy script
# Local se upload karke chalao:  DEPLOY_DIR=/root/hellapi bash deploy.sh
# Kya karta hai:
#   1. hellapi_source.zip → DEPLOY_DIR me unzip (purani .env / bot_data.db / cookies SAFE)
#   2. venv + deps (yt-dlp-oauth2 plugin included)
#   3. chromium + nodejs + deno install (speed + JS challenges)
#   4. systemd services install + start (auto-restart on)
#   5. Health checks
set -e

DEPLOY_DIR="${DEPLOY_DIR:-/root/hellapi}"
SRC_ZIP="${SRC_ZIP:-hellapi_source.zip}"

echo "=== HellAPI deploy → $DEPLOY_DIR ==="

# ── 1. System packages ─────────────────────────────────────────────
echo "[1/6] chromium + nodejs + deno + tools..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq >/dev/null 2>&1 || true
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chromium chromium-driver nodejs unzip curl wget >/dev/null 2>&1 || true
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q chromium nodejs unzip curl wget >/dev/null 2>&1 || true
elif command -v yum >/dev/null 2>&1; then
  yum install -y -q chromium nodejs unzip curl wget >/dev/null 2>&1 || true
fi

# Deno (yt-dlp default JS runtime)
if [ ! -f /root/.deno/bin/deno ]; then
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/root/.deno sh -s -- -y >/dev/null 2>&1 || true
fi
[ -f /root/.deno/bin/deno ] && ln -sf /root/.deno/bin/deno /usr/local/bin/deno
export PATH="/root/.deno/bin:/usr/local/bin:$PATH"
echo "  chromium: $(command -v chromium || echo MISSING) | node: $(command -v node || echo MISSING) | deno: $(command -v deno || echo MISSING)"

# ── 2. Old services band karo (deploy ke time conflict na ho) ──────
echo "[2/6] Purani services stop..."
systemctl stop hellapi-bot hellapi-api 2>/dev/null || true
pkill -9 -f "uvicorn main:app" 2>/dev/null || true
sleep 2

# ── 3. Source unzip (safe overwrite) ────────────────────────────────
echo "[3/6] Source deploy..."
mkdir -p "$DEPLOY_DIR"
cd "$DEPLOY_DIR"
unzip -oq "$SRC_ZIP" -d /tmp/hellapi_unzip
# Zip ke andar files root me hain (folder nahi) — contents copy karo
cp -r /tmp/hellapi_unzip/. "$DEPLOY_DIR"/
rm -rf /tmp/hellapi_unzip
echo "  deployed to $DEPLOY_DIR (.env, bot_data.db, cookies preserve hain)"

# ── 4. venv + deps ──────────────────────────────────────────────────
echo "[4/6] venv + dependencies..."
cd "$DEPLOY_DIR"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q --upgrade pip 2>&1 | tail -1
./venv/bin/pip install -q -r requirements.txt 2>&1 | tail -1

# ── 5. .env check ───────────────────────────────────────────────────
echo "[5/6] .env..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "  >> NAYA .env banaya (.env.example se). Ye values fill karo: TELEGRAM_BOT_TOKEN, ADMIN_KEY"
else
  echo "  existing .env preserved"
  # Naye feature flags missing hon to add karo (existing values ko touch kiye bina)
  grep -q "^YTDLP_OAUTH=" .env || echo "YTDLP_OAUTH=0" >> .env
  grep -q "^YTDLP_AUTO_UPDATE=" .env || echo "YTDLP_AUTO_UPDATE=1" >> .env
  grep -q "^YTDLP_UPDATE_INTERVAL=" .env || echo "YTDLP_UPDATE_INTERVAL=3600" >> .env
  grep -q "^YT_AUTO_RESTART_ON_BLOCK=" .env || echo "YT_AUTO_RESTART_ON_BLOCK=1" >> .env
  grep -q "^YT_RESTART_BLOCK_THRESHOLD=" .env || echo "YT_RESTART_BLOCK_THRESHOLD=2" >> .env
  grep -q "^YT_RESTART_COOLDOWN_SEC=" .env || echo "YT_RESTART_COOLDOWN_SEC=900" >> .env
fi

# ── 6. systemd services (DEPLOY_DIR ke hisaab se path fix) ────────
echo "[6/6] systemd services..."
sed "s|/root/hellapi|$DEPLOY_DIR|g" hellapi-api.service > /etc/systemd/system/hellapi-api.service
sed "s|/root/hellapi|$DEPLOY_DIR|g" hellapi-bot.service > /etc/systemd/system/hellapi-bot.service
systemctl daemon-reload
systemctl enable --now hellapi-api
sleep 6
systemctl enable --now hellapi-bot
sleep 3

# ── Health checks ───────────────────────────────────────────────────
echo ""
echo "=== Health ==="
curl -s -o /dev/null -w "API root:      %{http_code}\n" http://localhost:8000/
ADMIN_KEY=$(grep -E "^ADMIN_KEY=" .env | cut -d= -f2-)
if [ -n "$ADMIN_KEY" ]; then
  curl -s "http://localhost:8000/admin/update-status?api_key=$ADMIN_KEY" | head -c 300; echo
fi
systemctl is-active hellapi-api hellapi-bot

echo ""
echo "=== DEPLOY DONE ==="
echo "Logs:    journalctl -u hellapi-api -f   |   journalctl -u hellapi-bot -f"
echo "OAuth login (ONE-TIME, recommended):"
echo "  cd $DEPLOY_DIR && ./venv/bin/python oauth_setup.py"
echo "  → phone pe google.com/device kholo, code daalo"
echo "  → phir .env me YTDLP_OAUTH=1 karke: systemctl restart hellapi-api hellapi-bot"
