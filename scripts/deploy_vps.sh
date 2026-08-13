#!/usr/bin/env bash
# Run on the VPS as root: bash scripts/deploy_vps.sh
set -Eeuo pipefail

APP_DIR="${APP_DIR:-$HOME/conscout-backend}"
API_CONTAINER="conscout-backend-api"
AI_CONTAINER="conscout-backend-ai"
NETWORK="conscout-main"

cd "$APP_DIR"
git pull --ff-only origin main

docker build -t conscout-backend-api .
docker build -f Dockerfile.ai -t conscout-backend-ai .

docker rm -f "$API_CONTAINER" "$AI_CONTAINER" 2>/dev/null || true
docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK"

docker run -d --restart always --network "$NETWORK" -p 8001:8001 --env-file .env \
  -v "$HOME/conscout-storage/data:/data" \
  -v "$HOME/conscout-storage/models:/models" \
  --name "$AI_CONTAINER" conscout-backend-ai

docker run -d --restart always --network "$NETWORK" -p 8000:8000 --env-file .env \
  -e "AI_SERVICE_URL=http://$AI_CONTAINER:8001" \
  -v "$HOME/conscout-storage/data:/data" \
  -v "$HOME/conscout-storage/models:/models" \
  --name "$API_CONTAINER" conscout-backend-api

docker ps --filter "name=conscout-backend-"
docker exec "$API_CONTAINER" python -c 'import os, urllib.request; response = urllib.request.urlopen(os.environ["AI_SERVICE_URL"].rstrip("/") + "/health", timeout=10); print(response.status, response.read().decode())'
docker logs --tail 50 "$API_CONTAINER"
docker logs --tail 50 "$AI_CONTAINER"
