# AI routes: expose inference endpoints for count and segmentation.
# Loads models and calls the AI pipeline services.

# api/ai.py

from fastapi import APIRouter, HTTPException, UploadFile, File
import asyncio
import hashlib
import os

from ultralytics import YOLO

from core.config import MODEL_DIR, tour_raw_dir
from core.database import tours_collection

from services.ai_inference.count_inference_service import run_count_ai_for_tour
from services.ai_inference.segmentation_inference_service import run_seg_ai_for_tour

router = APIRouter(tags=["AI"])


def _model_sha256_short(path: str) -> str:
    if not os.path.isfile(path):
        return "missing"
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:12]


# =========================================================
# Load AI Models (ONCE)
# =========================================================

count_model_path = os.path.join(MODEL_DIR, "count.pt")
seg_model_path = os.path.join(MODEL_DIR, "seg.pt")
count_model = YOLO(count_model_path)
seg_model = YOLO(seg_model_path)
print(
    "[AI-BOOT] "
    f"count_model={count_model_path} "
    f"sha256={_model_sha256_short(count_model_path)}"
)
print(
    "[AI-BOOT] "
    f"seg_model={seg_model_path} "
    f"sha256={_model_sha256_short(seg_model_path)}"
)

count_lock = asyncio.Lock()
seg_lock = asyncio.Lock()


@router.post("/sync-streetview/{tour_id}")
async def sync_streetview_images(
    tour_id: str,
    files: list[UploadFile] = File(...),
):
    if not files:
        raise HTTPException(400, "No files provided")

    tour = tours_collection.find_one({"tour_id": tour_id})
    if not tour:
        raise HTTPException(404, "Tour not found")

    dest_dir = tour_raw_dir(
        tour_id,
        owner_email=tour.get("owner_email"),
        owner_user_id=tour.get("owner_user_id"),
    )
    os.makedirs(dest_dir, exist_ok=True)

    saved = 0
    for upload in files:
        filename = upload.filename or ""
        if not filename:
            continue
        out_path = os.path.join(dest_dir, filename)
        with open(out_path, "wb") as buffer:
            buffer.write(await upload.read())
        saved += 1

    return {"message": "Streetview images synced", "saved": saved}


# =========================================================
# Countable Object Detection (YOLO - Bounding Boxes)
# =========================================================
@router.post("/process-streetview-count/{tour_id}")
async def process_streetview_count(tour_id: str):
    result = await run_count_ai_for_tour(
        tour_id=tour_id,
        tours_collection=tours_collection,
        count_model=count_model,
        count_lock=count_lock,
    )

    if "error" in result:
        raise HTTPException(404, result["error"])

    return result


# =========================================================
# Segmentation & Presence Detection
# =========================================================
@router.post("/process-streetview-seg/{tour_id}")
async def process_streetview_seg(tour_id: str):
    result = await run_seg_ai_for_tour(
        tour_id=tour_id,
        tours_collection=tours_collection,
        seg_model=seg_model,
        seg_lock=seg_lock,
    )

    if "error" in result:
        raise HTTPException(404, result["error"])

    return result
