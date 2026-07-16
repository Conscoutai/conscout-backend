# API service entrypoint: wires routes, CORS, and static mounts.
# Runs the main HTTP API for the application.

import logging
import os
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from api.router import api_router
from core.auth import ensure_default_user_and_migrate_legacy_data
from core.config import (
    ALLOWED_ORIGINS,
    API_PORT,
    APP_SURFACE,
    APP_TITLE,
    APP_VERSION,
    DATA_DIR,
)
from services.progress.weekly_progress_notification_service import (
    ensure_weekly_progress_scheduler_started,
)

app = FastAPI(title=APP_TITLE, version=APP_VERSION)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

app.include_router(api_router)

# This migration belonged to the original shared database. It must be run only
# as an explicit, one-time administrative operation, never when a new Lite
# deployment starts.
if os.getenv("ENABLE_LEGACY_BOOTSTRAP_MIGRATION", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    ensure_default_user_and_migrate_legacy_data()

for directory in (DATA_DIR,):
    os.makedirs(directory, exist_ok=True)


@app.on_event("startup")
def startup_background_jobs():
    ensure_weekly_progress_scheduler_started()


@app.get("/")
def root():
    """Health check route confirms backend is running."""
    return {
        "message": "FastAPI Stitching Service is live and running!",
        "product": APP_SURFACE,
    }


@app.get("/health")
def health():
    return {"status": "ok", "product": APP_SURFACE}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)
