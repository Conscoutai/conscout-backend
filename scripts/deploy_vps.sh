#!/usr/bin/env bash
# Run on the VPS as root: bash scripts/deploy_vps.sh
set -Eeuo pipefail

APP_DIR="${APP_DIR:-$HOME/conscout-backend}"
API_CONTAINER="conscout-backend-api"
AI_CONTAINER="conscout-backend-ai"
NETWORK="conscout-main"
FIREBASE_CREDENTIALS_HOST_FILE="${FIREBASE_CREDENTIALS_HOST_FILE:-$HOME/conscout-secrets/firebase-adminsdk.json}"

# Validate before replacing any running service. Keep the key outside the repo.
if [[ ! -r "$FIREBASE_CREDENTIALS_HOST_FILE" ]]; then
  echo "Firebase Admin credential missing: $FIREBASE_CREDENTIALS_HOST_FILE" >&2
  exit 1
fi

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
  -e FIREBASE_CREDENTIALS_FILE=/secrets/firebase-adminsdk.json \
  --mount "type=bind,src=$FIREBASE_CREDENTIALS_HOST_FILE,dst=/secrets/firebase-adminsdk.json,readonly" \
  -v "$HOME/conscout-storage/data:/data" \
  -v "$HOME/conscout-storage/models:/models" \
  --name "$API_CONTAINER" conscout-backend-api

docker ps --filter "name=conscout-backend-"
docker exec "$API_CONTAINER" python -c 'import os, urllib.request; response = urllib.request.urlopen(os.environ["AI_SERVICE_URL"].rstrip("/") + "/health", timeout=10); print(response.status, response.read().decode())'
docker logs --tail 50 "$API_CONTAINER"
docker logs --tail 50 "$AI_CONTAINER"
