#!/usr/bin/env bash
set -euo pipefail

DEPLOY_HOST="${1:?Usage: ./deploy.sh ssh-host}"

echo "==> Pushing latest code..."
git push

echo "==> Deploying to ${DEPLOY_HOST}..."
# The existing checkout owns its .env and encrypted data; never export/copy browser cookies.
ssh "$DEPLOY_HOST" 'cd /opt/essusic && git pull --ff-only && docker compose -f docker-compose.yml -f compose.web.yml up -d --build'

echo "==> Done. Server owners manage their sources through /setup."
