# VPS backend deployment

Use this guide to deploy the ConScout backend from the `main` branch to the VPS.
Run every command on the VPS as `root`.

> The Main API/AI services use ports `8000` and `8001`. ConScout Lite is a
> separate API service on port `8002`; deploy it whenever a Lite or Moyasar
> backend change is included.

## 1. Connect and update the source

```bash
ssh root@91.98.16.60
cd ~/conscout-backend
git pull --ff-only origin main
```

If the VPS clone tracks a different remote, check it with `git remote -v` and
replace `origin` only when necessary.

## 2. Build the images

```bash
docker build -t conscout-backend-api .
docker build -f Dockerfile.ai -t conscout-backend-ai .
docker build -t conscout-backend-lite-api:latest .
```

## 3. Restart the Main API and AI

```bash
docker rm -f conscout-backend-api conscout-backend-ai
```

```bash
docker run -d --restart always -p 8000:8000 --env-file .env -v ~/conscout-storage/data:/data -v ~/conscout-storage/models:/models --name conscout-backend-api conscout-backend-api
```

```bash
docker run -d --restart always -p 8001:8001 --env-file .env -v ~/conscout-storage/data:/data -v ~/conscout-storage/models:/models --name conscout-backend-ai conscout-backend-ai
```

## 4. Restart the Lite API

The Lite API has its own data directory and uses the public HTTPS API URL for
checkout callbacks. Keep these Lite variables configured in `.env`:

```env
MOYASAR_PUBLISHABLE_KEY=pk_test_or_pk_live_value
MOYASAR_SECRET_KEY=sk_test_or_sk_live_value
MOYASAR_WEBHOOK_SECRET=a_private_long_random_value
```

The command below explicitly sets Lite-only settings so the shared environment
file cannot accidentally start this container as the Main product.

```bash
docker rm -f conscout-backend-lite-api
```

```bash
docker run -d --restart always --network bridge -p 8002:8000 --env-file .env -e APP_SURFACE=lite -e DB_NAME=construction_ai_lite -e PUBLIC_API_BASE_URL=https://lite-api.conscout.com -e SUBSCRIPTION_RENEWAL_SCHEDULER_ENABLED=false -v ~/conscout-storage/data-lite:/data -v ~/conscout-storage/models:/models --name conscout-backend-lite-api conscout-backend-lite-api:latest
```

Do not expose `.env`, API keys, webhook secrets, database URLs, or checkout
URLs in screenshots, chat, or Git.

## 5. Verify the deployment

```bash
docker ps
docker logs --tail 50 conscout-backend-api
docker logs --tail 50 conscout-backend-ai
docker logs --tail 50 conscout-backend-lite-api
curl -sS https://lite-api.conscout.com/health
```

The last command must return:

```json
{"status":"ok","product":"lite"}
```

## 6. Moyasar test checklist

Use Moyasar **Test Environment** while the backend has `pk_test_` and `sk_test_`
keys. The test webhook must be:

```text
POST https://lite-api.conscout.com/subscriptions/moyasar/webhook
```

Use the same value as `MOYASAR_WEBHOOK_SECRET` and select `payment_paid`,
`payment_failed`, `payment_refunded`, and `payment_voided`.

Switch to live keys and a newly generated live webhook secret only after the
complete test checkout succeeds.
