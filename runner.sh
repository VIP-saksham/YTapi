#!/usr/bin/env bash
# runner.sh — container-friendly supervisor (systemd nahi hai is VPS pe)
# KeyaraAPI: API + Key-Bot dono chalata hai; koi mare ya .restart_flag mile to dono restart.
DEPLOY_DIR="${DEPLOY_DIR:-/root/nakshu/keyaraapi}"
cd "$DEPLOY_DIR" || exit 1

mkdir -p logs

# ── Single-instance lock (flock) ─────────────────────────────────
exec 9>/tmp/keyaraapi_runner.lock
if ! flock -n 9; then
  echo "[runner] already running — exit ($$) at $(date)" >> logs/runner.log
  exit 0
fi

echo "[runner] started $$ at $(date)" >> logs/runner.log

restart_all() {
  echo "[runner] restarting API+BOT at $(date) — reason: $1" >> logs/runner.log
  pkill -9 -f "uvicorn main:app" 2>/dev/null
  pkill -9 -f "python tgbot.py"  2>/dev/null
  sleep 2
  rm -f .restart_flag
}

while true; do
  # IP-watchdog ne restart flag lagaya? → dono restart
  if [ -f .restart_flag ]; then
    restart_all "restart_flag (IP-block watchdog)"
  fi

  # API zinda hai?
  if ! pgrep -f "uvicorn main:app" >/dev/null 2>&1; then
    restart_all "api dead"
    nohup ./venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000 \
      >> logs/api.log 2>&1 9>&- &          # 9>&-: lock fd child ko inherit na ho
    echo "[runner] API started pid $!" >> logs/runner.log
  fi

  # Bot zinda hai?
  if ! pgrep -f "python tgbot.py" >/dev/null 2>&1; then
    nohup ./venv/bin/python tgbot.py >> logs/bot.log 2>&1 9>&- &
    echo "[runner] BOT started pid $!" >> logs/runner.log
  fi

  sleep 5
done
