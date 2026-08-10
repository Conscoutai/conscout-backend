# Backend Deployment Notes

This folder contains the FastAPI backend for Construction Monitor.

## Services

There are two FastAPI services:

- API service: `app_main.py` (port `8000`)
- AI service: `ai_main.py` (port `8001`)

## Local Run (without Docker)

```bash
cd Backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements.ai.txt
```

Run AI service:

```bash
uvicorn ai_main:app --host 127.0.0.1 --port 8001 --reload
```

Run API service:

```bash
uvicorn app_main:app --host 127.0.0.1 --port 8000 --reload
```

## Docker

Build and run with the root compose file:

```bash
docker compose up --build
```

Endpoints:

```
API: http://127.0.0.1:8000
AI: http://127.0.0.1:8001
```

## Environment Variables

Copy `Backend/.env.example` to your deployment environment and set:

- `ALLOWED_ORIGINS` (comma-separated frontend URLs)
- `MONGO_URI`
- `DB_NAME`
- Optional storage paths (if you mount custom volumes)

### Moyasar Lite subscription checkout

Use test credentials first:

```env
PUBLIC_API_BASE_URL=https://api.conscout.com
SUBSCRIPTION_PAYMENT_CURRENCY=USD
MOYASAR_PUBLISHABLE_KEY=pk_test_replace_me
MOYASAR_SECRET_KEY=sk_test_replace_me
MOYASAR_WEBHOOK_SECRET=replace_with_a_long_random_value
SUBSCRIPTION_RENEWAL_SCHEDULER_ENABLED=false
```

Configure the Moyasar dashboard webhook as:

```text
POST https://api.conscout.com/subscriptions/moyasar/webhook
```

Set the landing deployment's `LITE_API_BASE_URL` to the same HTTPS API origin.
Only localhost may use HTTP during development.

Select the paid, failed, refunded, and voided payment events and enter the
same value used for `MOYASAR_WEBHOOK_SECRET`. Never commit or expose the
`MOYASAR_SECRET_KEY`. Replace test credentials with live credentials only
after callback, webhook, duplicate-payment, failed-payment, and refund tests
all pass. Keep the renewal scheduler disabled while testing the first payment,
then test a deliberately due sandbox subscription before setting
`SUBSCRIPTION_RENEWAL_SCHEDULER_ENABLED=true`.

Lite admin approval is disabled: a paid tier becomes active only after the
backend fetches and verifies the Moyasar payment. The same verified entitlement
is enforced by the project-creation API using the server-owned 1/4/10/unlimited
project limits and the paid period end date. Customers can stop renewal from
the plans page without losing the remainder of the paid period. See
`docs/moyasar_lite_checkout.md` for the full test and production-switch
checklist.

## Storage

The backend reads/writes these local folders:

- `Backend/data/sites/<site_name>/floorplan` (floorplans)
- `Backend/data/sites/<site_name>/dxf` (DXF files)
- `Backend/data/tours/<tour_id>/raw` (uploaded 360s)
- `Backend/data/tours/<tour_id>/detect` (count model outputs)
- `Backend/data/tours/<tour_id>/detect+seg` (segmentation outputs)
- `Backend/data/tours/<tour_id>/comments` (comment reports/attachments)
- `Backend/models` (AI models)

Mount these as volumes in production or switch to external storage.
