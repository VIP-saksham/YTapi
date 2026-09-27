#!/usr/bin/env bash
# KeyaraAPI site -> Cloudflare Pages (wrangler direct-upload)
# Requires: CLOUDFLARE_API_TOKEN env var (Cloudflare Pages - Edit permission)
set -e
cd "$(dirname "$0")/_site"

if [ -z "$CLOUDFLARE_API_TOKEN" ]; then
  echo "ERROR: export CLOUDFLARE_API_TOKEN=<your-token> first (Cloudflare Pages - Edit template)"
  exit 1
fi

if ! command -v wrangler >/dev/null 2>&1; then
  echo "wrangler not found, installing locally..."
  npm install wrangler --no-fund --no-audit 2>&1 | tail -1
  alias npx=./node_modules/.bin/wrangler
fi

echo "Deploying _site/ to Cloudflare Pages project: keyaraapi"
npx wrangler pages deploy . --project-name=keyaraapi --branch=main --commit-dirty=true
echo "DONE: https://keyaraapi.pages.dev"
