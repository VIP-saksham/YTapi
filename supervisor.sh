#!/bin/bash
# KeyaraAPI supervisor — uvicorn (8000) + key bot restart if down (cron 5min)
DIR=/root/nakshu/keyaraapi
cd "$DIR" || exit 1

if ! pgrep -f 'uvicorn main:app --host 0.0.0.0 --port 8000' > /dev/null; then
  setsid nohup "$DIR/venv/bin/python" -m uvicorn main:app --host 0.0.0.0 --port 8000 >> "$DIR/service.log" 2>&1 < /dev/null &
  echo "$(date '+%F %T') uvicorn restarted" >> "$DIR/supervisor.log"
fi

if ! pgrep -f 'venv/bin/python tgbot.py' > /dev/null; then
  setsid nohup "$DIR/venv/bin/python" tgbot.py >> "$DIR/keybot.log" 2>&1 < /dev/null &
  echo "$(date '+%F %T') tgbot restarted" >> "$DIR/supervisor.log"
fi
