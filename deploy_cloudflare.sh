#!/usr/bin/env bash
# KeyaraAPI site -> Cloudflare Pages (wrangler direct-upload)
# Requires: CLOUDFLARE_API_TOKEN env var (Cloudflare Pages - Edit permission)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$CLOUDFLARE_API_TOKEN" ]; then
  echo "ERROR: export CLOUDFLARE_API_TOKEN=<your-token> first (Cloudflare Pages - Edit template)"
  exit 1
fi

# wrangler ko neutral dir me rakhte hain (repo clean rehta hai)
WR_DIR=/root/.cf-wrangler
mkdir -p "$WR_DIR"
if [ ! -x "$WR_DIR/node_modules/.bin/wrangler" ]; then
  echo "Installing wrangler (one-time, ~1 min)..."
  (cd "$WR_DIR" && npm install wrangler --no-fund --no-audit 2>&1 | tail -1)
fi

echo "Deploying $DIR/_site -> Cloudflare Pages project: keyaraapi"
"$WR_DIR/node_modules/.bin/wrangler" pages deploy "$DIR/_site" \
  --project-name=keyaraapi --branch=main --commit-dirty=true
echo "DONE: https://keyaraapi.pages.dev"
