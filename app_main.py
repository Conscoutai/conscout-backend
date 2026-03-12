# API service entrypoint: wires routes, CORS, and static mounts.
# Runs the main HTTP API for the application.

import logging
import os
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.router import api_router
from core.config import (
    ALLOWED_ORIGINS,
    API_PORT,
    APP_TITLE,
    APP_VERSION,
    SITES_DIR,
    TOURS_DIR,
)

load_dotenv()

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

for directory in (SITES_DIR, TOURS_DIR):
    os.makedirs(directory, exist_ok=True)

app.mount("/sites", StaticFiles(directory=SITES_DIR), name="sites")
app.mount("/streetview", StaticFiles(directory=TOURS_DIR), name="streetview")


@app.get("/")
def root():
    """Health check route confirms backend is running."""
    return {"message": "FastAPI Stitching Service is live and running!"}


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)
