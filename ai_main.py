# AI service entrypoint: hosts ML inference endpoints and models.
# Runs independently to isolate heavy dependencies and memory use.

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

from core.config import AI_PORT, ALLOWED_ORIGINS, APP_TITLE, APP_VERSION
from api.routes.ai_management.ai_inference import router as ai_router


app = FastAPI(title=f"{APP_TITLE} - AI Service", version=APP_VERSION)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ai_router)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=AI_PORT)

