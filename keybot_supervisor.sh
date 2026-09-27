#!/usr/bin/env bash
# keybot_supervisor.sh — key-bot ko PERMANENT ON rakhta hai (crash → 5s me restart)
cd /root/hellapi || exit 1
while true; do
  ./venv/bin/python tgbot.py >> tgbot.log 2>&1
  echo "[supervisor] tgbot exited $(date) — restart in 5s" >> tgbot.log
  sleep 5
done
