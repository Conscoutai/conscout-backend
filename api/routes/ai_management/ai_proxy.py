# AI proxy routes: forward AI calls to the separate AI service.
# Keeps the API light and decoupled from ML dependencies.

# api/ai_proxy.py

import os
import logging
import tempfile
import zipfile
import requests
from fastapi import APIRouter, HTTPException

from core.config import (
    AI_PROCESS_TIMEOUT_SECONDS,
    AI_SERVICE_URL,
    AI_SYNC_TIMEOUT_SECONDS,
    tour_detect_dir,
    tour_detect_seg_dir,
    tour_raw_dir,
)
from core.database import tours_collection

router = APIRouter(tags=["AI"])
logger = logging.getLogger(__name__)


def _ai_url(path: str) -> str:
    base = AI_SERVICE_URL.rstrip("/")
    return f"{base}{path}"


def _sync_streetview_images(tour_id: str) -> None:
    tour = tours_collection.find_one({"tour_id": tour_id})
    if not tour:
        raise HTTPException(404, f"Tour not found: {tour_id}")

    tour_dir = tour_raw_dir(
        tour_id,
        owner_email=tour.get("owner_email"),
        owner_user_id=tour.get("owner_user_id"),
        site_name=tour.get("site_name") or tour.get("site") or tour.get("project_id"),
    )
    if not os.path.isdir(tour_dir):
        raise HTTPException(404, f"Tour folder not found: {tour_id}")

    filenames = [
        name
        for name in os.listdir(tour_dir)
        if name.lower().endswith((".jpg", ".jpeg", ".png"))
    ]
    if not filenames:
        raise HTTPException(404, f"No images found for tour: {tour_id}")

    logger.info(
        "Syncing streetview images for tour_id=%s count=%s ai_base=%s",
        tour_id,
        len(filenames),
        AI_SERVICE_URL,
    )
    files = []
    file_handles = []
    try:
        for name in filenames:
            path = os.path.join(tour_dir, name)
            handle = open(path, "rb")
            file_handles.append(handle)
            files.append(("files", (name, handle)))

        resp = requests.post(
            _ai_url(f"/sync-streetview/{tour_id}"),
            files=files,
            timeout=AI_SYNC_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.exception("AI sync request failed for tour_id=%s", tour_id)
        raise HTTPException(502, f"AI sync error: {exc}") from exc
    finally:
        for handle in file_handles:
            try:
                handle.close()
            except OSError:
                pass

    if not resp.ok:
        logger.error(
            "AI sync error response tour_id=%s status=%s body=%s",
            tour_id,
            resp.status_code,
            resp.text[:1000],
        )
        raise HTTPException(resp.status_code, resp.text)


def _sync_processed_streetview_assets(tour_id: str, kind: str) -> None:
    tour = tours_collection.find_one({"tour_id": tour_id})
    if not tour:
        raise HTTPException(404, f"Tour not found: {tour_id}")

    params = {"kind": kind}
    try:
        logger.info(
            "Pulling AI assets tour_id=%s kind=%s url=%s",
            tour_id,
            kind,
            _ai_url(f"/export-streetview-assets/{tour_id}"),
        )
        resp = requests.get(
            _ai_url(f"/export-streetview-assets/{tour_id}"),
            params=params,
            timeout=AI_PROCESS_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.exception("AI asset export failed for tour_id=%s kind=%s", tour_id, kind)
        raise HTTPException(502, f"AI asset export error: {exc}") from exc

    if not resp.ok:
        logger.error(
            "AI asset export error tour_id=%s kind=%s status=%s body=%s",
            tour_id,
            kind,
            resp.status_code,
            resp.text[:1000],
        )
        raise HTTPException(resp.status_code, resp.text)

    storage_kwargs = {
        "owner_email": tour.get("owner_email"),
        "owner_user_id": tour.get("owner_user_id"),
        "site_name": tour.get("site_name") or tour.get("site") or tour.get("project_id"),
    }
    detect_dir = tour_detect_dir(tour_id, **storage_kwargs)
    seg_dir = tour_detect_seg_dir(tour_id, **storage_kwargs)
    os.makedirs(detect_dir, exist_ok=True)
    os.makedirs(seg_dir, exist_ok=True)

    fd, archive_path = tempfile.mkstemp(prefix=f"{tour_id}_{kind}_", suffix=".zip")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(resp.content)

        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                normalized = member.filename.replace("\\", "/").lstrip("/")
                if normalized.startswith("detect/"):
                    out_path = os.path.join(detect_dir, os.path.basename(normalized))
                elif normalized.startswith("detect+seg/"):
                    out_path = os.path.join(seg_dir, os.path.basename(normalized))
                else:
                    continue
                with archive.open(member) as source, open(out_path, "wb") as target:
                    target.write(source.read())
    finally:
        try:
            os.remove(archive_path)
        except OSError:
            pass


@router.post("/process-streetview-count/{tour_id}")
def proxy_process_streetview_count(tour_id: str):
    if not AI_SERVICE_URL:
        raise HTTPException(503, "AI service is not configured")

    _sync_streetview_images(tour_id)

    try:
        logger.info(
            "Calling AI count tour_id=%s url=%s",
            tour_id,
            _ai_url(f"/process-streetview-count/{tour_id}"),
        )
        resp = requests.post(
            _ai_url(f"/process-streetview-count/{tour_id}"),
            timeout=AI_PROCESS_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.exception("AI count request failed for tour_id=%s", tour_id)
        raise HTTPException(502, f"AI service error: {exc}") from exc

    if not resp.ok:
        logger.error(
            "AI count error response tour_id=%s status=%s body=%s",
            tour_id,
            resp.status_code,
            resp.text[:1000],
        )
        raise HTTPException(resp.status_code, resp.text)

    _sync_processed_streetview_assets(tour_id, "count")
    return resp.json()


@router.post("/process-streetview-seg/{tour_id}")
def proxy_process_streetview_seg(tour_id: str):
    if not AI_SERVICE_URL:
        raise HTTPException(503, "AI service is not configured")

    _sync_streetview_images(tour_id)

    try:
        logger.info(
            "Calling AI seg tour_id=%s url=%s",
            tour_id,
            _ai_url(f"/process-streetview-seg/{tour_id}"),
        )
        resp = requests.post(
            _ai_url(f"/process-streetview-seg/{tour_id}"),
            timeout=AI_PROCESS_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.exception("AI seg request failed for tour_id=%s", tour_id)
        raise HTTPException(502, f"AI service error: {exc}") from exc

    if not resp.ok:
        logger.error(
            "AI seg error response tour_id=%s status=%s body=%s",
            tour_id,
            resp.status_code,
            resp.text[:1000],
        )
        raise HTTPException(resp.status_code, resp.text)

    _sync_processed_streetview_assets(tour_id, "seg")
    return resp.json()
