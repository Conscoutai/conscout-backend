# AI routes: expose inference endpoints for count and segmentation.
# Loads models and calls the AI pipeline services.

# api/ai.py

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
import asyncio
import hashlib
import os
import tempfile
import zipfile

from ultralytics import YOLO

from core.config import MODEL_DIR, tour_detect_dir, tour_detect_seg_dir, tour_raw_dir
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


def _tour_storage_kwargs(tour: dict) -> dict:
    return {
        "owner_email": tour.get("owner_email"),
        "owner_user_id": tour.get("owner_user_id"),
        "site_name": tour.get("site_name") or tour.get("site") or tour.get("project_id"),
    }


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

    dest_dir = tour_raw_dir(tour_id, **_tour_storage_kwargs(tour))
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


@router.get("/export-streetview-assets/{tour_id}")
async def export_streetview_assets(tour_id: str, kind: str = "all"):
    tour = tours_collection.find_one({"tour_id": tour_id})
    if not tour:
        raise HTTPException(404, "Tour not found")

    storage_kwargs = _tour_storage_kwargs(tour)
    export_roots: list[tuple[str, str]] = []
    if kind in {"all", "count"}:
        export_roots.append(("detect", tour_detect_dir(tour_id, **storage_kwargs)))
    if kind in {"all", "seg"}:
        export_roots.append(("detect+seg", tour_detect_seg_dir(tour_id, **storage_kwargs)))
    if not export_roots:
        raise HTTPException(400, "Invalid export kind")

    fd, archive_path = tempfile.mkstemp(prefix=f"{tour_id}_", suffix=".zip")
    os.close(fd)
    file_count = 0
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for prefix, root in export_roots:
            if not os.path.isdir(root):
                continue
            for name in os.listdir(root):
                source = os.path.join(root, name)
                if not os.path.isfile(source):
                    continue
                archive.write(source, arcname=f"{prefix}/{name}")
                file_count += 1

    if file_count == 0:
        try:
            os.remove(archive_path)
        except OSError:
            pass
        raise HTTPException(404, "No processed assets found")

    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"{tour_id}_{kind}.zip",
    )


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
