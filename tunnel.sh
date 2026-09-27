#!/usr/bin/env bash
# KeyaraAPI public tunnel (Cloudflare quick tunnel)
# - Starts cloudflared -> http://localhost:8000
# - Saves public URL to public_url.txt
# - URL change hone pe tgbot restart (naya URL pick karne)
# - Cron watchdog har minute ye script chalata hai (agar tunnel dead hai)
LOG=/root/nakshu/keyaraapi/tunnel.log
URLF=/root/nakshu/keyaraapi/public_url.txt

if pgrep -f '[c]loudflared tunnel' >/dev/null 2>&1; then
  exit 0  # already running
fi

echo "[$(date '+%F %T')] starting cloudflared..." >> "$LOG"
nohup cloudflared tunnel --url http://localhost:8000 --no-autoupdate \
  > /tmp/cf_tunnel_out.log 2>&1 &

# URL nikalna (trycloudflare.com try-tunnel line se)
URL=""
for i in $(seq 1 30); do
  sleep 2
  URL=$(grep -ao 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/cf_tunnel_out.log | head -1)
  [ -n "$URL" ] && break
done

if [ -z "$URL" ]; then
  echo "[$(date '+%F %T')] ERROR: tunnel URL nahi mila" >> "$LOG"
  exit 1
fi

OLDURL=$(cat "$URLF" 2>/dev/null)
echo "$URL" > "$URLF"
echo "[$(date '+%F %T')] tunnel UP: $URL (old: $OLDURL)" >> "$LOG"

# Agar URL badla hai to worker regenerate + deploy + bots restart
if [ -n "$OLDURL" ] && [ "$OLDURL" != "$URL" ]; then
  echo "$URL" > /root/nakshu/keyaraapi/api_url.txt
  echo "[$(date '+%F %T')] URL changed -> regenerating worker + redeploying" >> "$LOG"
  sed -i "s|const BACKEND = .*|const BACKEND = \"$URL\";|" /root/nakshu/keyaraapi/_site/_worker.js 2>/dev/null
  rm -f /root/nakshu/keyaraapi/.site_hash
  bash /root/nakshu/keyaraapi/autodeploy.sh || true
  pkill -f '[t]gbot.py' || true
fi
exit 0
