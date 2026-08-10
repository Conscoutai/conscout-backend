#!/usr/bin/env bash
# Run on the VPS as root: bash scripts/deploy_vps.sh
set -Eeuo pipefail

APP_DIR="${APP_DIR:-$HOME/conscout-backend}"
API_CONTAINER="conscout-backend-api"
AI_CONTAINER="conscout-backend-ai"

cd "$APP_DIR"
git pull --ff-only origin main

docker build -t conscout-backend-api .
docker build -f Dockerfile.ai -t conscout-backend-ai .

docker rm -f "$API_CONTAINER" "$AI_CONTAINER" 2>/dev/null || true

docker run -d --restart always -p 8000:8000 --env-file .env \
  -v "$HOME/conscout-storage/data:/data" \
  -v "$HOME/conscout-storage/models:/models" \
  --name "$API_CONTAINER" conscout-backend-api

docker run -d --restart always -p 8001:8001 --env-file .env \
  -v "$HOME/conscout-storage/data:/data" \
  -v "$HOME/conscout-storage/models:/models" \
  --name "$AI_CONTAINER" conscout-backend-ai

docker ps --filter "name=conscout-backend-"
docker logs --tail 50 "$API_CONTAINER"
docker logs --tail 50 "$AI_CONTAINER"
