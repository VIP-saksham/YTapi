#!/usr/bin/env bash
# KeyaraAPI auto-deploy (Buffy)
# - _site/index.html change hone pe Cloudflare Pages pe auto redeploy
# - cron: */1 * * * * (har minute check)
SITE=/root/nakshu/keyaraapi/_site/index.html
HASHF=/root/nakshu/keyaraapi/.site_hash
LOG=/root/nakshu/keyaraapi/autodeploy.log

[ -f "$SITE" ] || exit 0
NEWHASH=$(md5sum "$SITE" | awk '{print $1}')
OLDHASH=$(cat "$HASHF" 2>/dev/null)

[ "$NEWHASH" = "$OLDHASH" ] && exit 0   # no change

echo "$NEWHASH" > "$HASHF"
echo "[$(date '+%F %T')] change detected -> deploying..." >> "$LOG"
export CLOUDFLARE_API_TOKEN=$(cat /root/.cf_token 2>/dev/null)
[ -z "$CLOUDFLARE_API_TOKEN" ] && { echo "[$(date '+%F %T')] ERROR: no token" >> "$LOG"; exit 1; }
/root/.cf-wrangler/node_modules/.bin/wrangler pages deploy /root/nakshu/keyaraapi/_site \
  --project-name=keyaraapi --branch=main --commit-dirty=true >> "$LOG" 2>&1
echo "[$(date '+%F %T')] deploy done" >> "$LOG"
