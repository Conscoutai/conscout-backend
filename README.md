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
