from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from core.auth_context import get_current_user
from core.auth import require_authenticated_user
from core.config import DATA_DIR, site_storage_roots, tour_storage_roots
from core.database import floorplans_collection, tours_collection


router = APIRouter(tags=["ProtectedAssets"], dependencies=[Depends(require_authenticated_user)])

_ASSET_CACHE_HEADERS = {
    "Cache-Control": "private, max-age=86400, stale-while-revalidate=604800",
}


def _safe_join(base_dir: str, relative_path: str) -> str:
    candidate = os.path.abspath(os.path.join(base_dir, relative_path))
    base = os.path.abspath(base_dir)
    if not candidate.startswith(base):
        raise HTTPException(status_code=403, detail="Invalid asset path.")
    return candidate


def _resolve_existing_file(base_dirs: list[str], relative_path: str) -> str:
    for base_dir in base_dirs:
        candidate = _safe_join(base_dir, relative_path)
        if os.path.isfile(candidate):
            return candidate
    raise HTTPException(status_code=404, detail="Asset file not found.")


def _resolve_tour_doc_for_path(storage_key: str):
    tour = tours_collection.find_one({"storage_key": storage_key})
    if tour:
        return tour
    tour = tours_collection.find_one({"tour_id": storage_key})
    if tour:
        return tour
    if "__" in storage_key:
        possible_tour_id = storage_key.split("__")[-1]
        return tours_collection.find_one({"tour_id": possible_tour_id})
    return None


def _append_unique_dir(base_dirs: list[str], candidate: str) -> None:
    if candidate and candidate not in base_dirs:
        base_dirs.append(candidate)


def _tour_asset_roots(tour: dict) -> list[str]:
    roots: list[str] = []

    for base_dir in tour_storage_roots(
        owner_email=tour.get("owner_email"),
        owner_user_id=tour.get("owner_user_id"),
    ):
        _append_unique_dir(roots, base_dir)

    current_user = get_current_user()
    if current_user:
        for base_dir in tour_storage_roots(
            owner_email=current_user.email,
            owner_user_id=current_user.user_id,
        ):
            _append_unique_dir(roots, base_dir)

    try:
        for entry in os.listdir(DATA_DIR):
            candidate = os.path.join(DATA_DIR, entry, "tours")
            if os.path.isdir(candidate):
                _append_unique_dir(roots, candidate)
    except FileNotFoundError:
        pass

    return roots


@router.get("/streetview/{asset_path:path}")
def get_tour_asset(asset_path: str):
    normalized = asset_path.strip().lstrip("/")
    parts = [part for part in normalized.split("/") if part]
    if len(parts) < 2:
        raise HTTPException(status_code=404, detail="Asset not found.")

    storage_key = parts[0]
    tour = _resolve_tour_doc_for_path(storage_key)
    if not tour:
        raise HTTPException(status_code=404, detail="Tour asset not found.")

    file_path = _resolve_existing_file(
        _tour_asset_roots(tour),
        normalized,
    )
    return FileResponse(file_path, headers=_ASSET_CACHE_HEADERS)


@router.get("/sites/{asset_path:path}")
def get_site_asset(asset_path: str):
    normalized = asset_path.strip().lstrip("/")
    parts = [part for part in normalized.split("/") if part]
    if len(parts) < 2:
        raise HTTPException(status_code=404, detail="Asset not found.")

    site_name = parts[0]
    floorplan = floorplans_collection.find_one(
        {"$or": [{"site_name": site_name}, {"dxf_project_id": site_name}]}
    )
    if not floorplan:
        raise HTTPException(status_code=404, detail="Site asset not found.")

    file_path = _resolve_existing_file(
        site_storage_roots(
            owner_email=floorplan.get("owner_email"),
            owner_user_id=floorplan.get("owner_user_id"),
        ),
        normalized,
    )
    return FileResponse(file_path, headers=_ASSET_CACHE_HEADERS)
