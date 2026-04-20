from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse

from core.auth import require_authenticated_user
from core.auth_context import AuthenticatedUser
from core.config import DATA_DIR, site_storage_roots, tour_storage_roots
from core.database import raw_floorplans_collection, raw_tours_collection


router = APIRouter(tags=["ProtectedAssets"])

_ASSET_CACHE_HEADERS = {
    "Cache-Control": "private, max-age=86400, stale-while-revalidate=604800",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
}


def _asset_error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        },
    )


def _safe_join(base_dir: str, relative_path: str) -> str:
    candidate = os.path.abspath(os.path.join(base_dir, relative_path))
    base = os.path.abspath(base_dir)
    if not candidate.startswith(base):
        raise HTTPException(status_code=403, detail="Invalid asset path.")
    return candidate


def _resolve_existing_file(base_dirs: list[str], relative_paths: list[str]) -> str:
    seen: set[tuple[str, str]] = set()
    for base_dir in base_dirs:
        for relative_path in relative_paths:
            key = (base_dir, relative_path)
            if key in seen:
                continue
            seen.add(key)
            candidate = _safe_join(base_dir, relative_path)
            if os.path.isfile(candidate):
                return candidate
    raise FileNotFoundError("Asset file not found.")


def _resolve_tour_doc_for_path(storage_key: str):
    tour = raw_tours_collection.find_one({"storage_key": storage_key})
    if tour:
        return tour
    tour = raw_tours_collection.find_one({"tour_id": storage_key})
    if tour:
        return tour
    if "__" in storage_key:
        possible_tour_id = storage_key.split("__")[-1]
        return raw_tours_collection.find_one({"tour_id": possible_tour_id})
    return None


def _append_unique_dir(base_dirs: list[str], candidate: str) -> None:
    if candidate and candidate not in base_dirs:
        base_dirs.append(candidate)


def _tour_asset_roots(tour: dict) -> list[str]:
    roots: list[str] = []
    site_name = tour.get("site_name") or tour.get("site") or tour.get("project_id")

    for base_dir in tour_storage_roots(
        owner_email=tour.get("owner_email"),
        owner_user_id=tour.get("owner_user_id"),
        site_name=site_name,
    ):
        _append_unique_dir(roots, base_dir)

    try:
        for entry in os.listdir(DATA_DIR):
            candidate = os.path.join(DATA_DIR, entry, "tours")
            if os.path.isdir(candidate):
                _append_unique_dir(roots, candidate)
            sites_root = os.path.join(DATA_DIR, entry, "sites")
            if not os.path.isdir(sites_root):
                continue
            for site_entry in os.listdir(sites_root):
                nested = os.path.join(sites_root, site_entry, "tours")
                if os.path.isdir(nested):
                    _append_unique_dir(roots, nested)
    except FileNotFoundError:
        pass

    return roots


def _user_can_access_tour(user: AuthenticatedUser, tour: dict) -> bool:
    if user.role != "stakeholder":
        return True

    project_names = set(user.accessible_project_names)
    floorplan_ids = set(user.accessible_floorplan_ids)
    tour_project = (
        tour.get("site_name")
        or tour.get("site")
        or tour.get("project_id")
        or tour.get("dxf_project_id")
    )
    floorplan_id = tour.get("floorplan_id")
    return bool(
        (tour_project and tour_project in project_names)
        or (floorplan_id and floorplan_id in floorplan_ids)
    )


def _user_can_access_site(user: AuthenticatedUser, floorplan: dict) -> bool:
    if user.role != "stakeholder":
        return True

    project_names = set(user.accessible_project_names)
    floorplan_ids = set(user.accessible_floorplan_ids)
    site_name = floorplan.get("site_name") or floorplan.get("dxf_project_id")
    floorplan_id = floorplan.get("id")
    return bool(
        (site_name and site_name in project_names)
        or (floorplan_id and floorplan_id in floorplan_ids)
    )


def _append_unique_path(paths: list[str], candidate: str) -> None:
    cleaned = candidate.strip().lstrip("/")
    if cleaned and cleaned not in paths:
        paths.append(cleaned)


def _tour_relative_path_candidates(
    *,
    normalized: str,
    path_storage_key: str,
    tour: dict,
    roots: list[str],
) -> list[str]:
    candidates: list[str] = []
    _append_unique_path(candidates, normalized)

    parts = [part for part in normalized.split("/") if part]
    rest = "/".join(parts[1:])
    tour_id = str(tour.get("tour_id") or "").strip()
    stored_key = str(tour.get("storage_key") or "").strip()

    for key in (stored_key, tour_id):
        if key and rest:
            _append_unique_path(candidates, f"{key}/{rest}")

    if tour_id and rest:
        suffix = f"__{tour_id}"
        for root in roots:
            try:
                for entry in os.listdir(root):
                    candidate_dir = os.path.join(root, entry)
                    if not os.path.isdir(candidate_dir):
                        continue
                    if entry == path_storage_key or entry == tour_id or entry.endswith(suffix):
                        _append_unique_path(candidates, f"{entry}/{rest}")
            except FileNotFoundError:
                continue

    return candidates


@router.get("/streetview/{asset_path:path}")
def get_tour_asset(
    asset_path: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    normalized = asset_path.strip().lstrip("/")
    parts = [part for part in normalized.split("/") if part]
    if len(parts) < 2:
        return _asset_error(404, "Asset not found.")

    storage_key = parts[0]
    tour = _resolve_tour_doc_for_path(storage_key)
    if not tour:
        return _asset_error(404, "Tour asset not found.")
    if not _user_can_access_tour(current_user, tour):
        return _asset_error(403, "Tour asset access denied.")

    roots = _tour_asset_roots(tour)

    try:
        file_path = _resolve_existing_file(
            roots,
            _tour_relative_path_candidates(
                normalized=normalized,
                path_storage_key=storage_key,
                tour=tour,
                roots=roots,
            ),
        )
    except FileNotFoundError:
        return _asset_error(404, "Asset file not found.")
    return FileResponse(file_path, headers=_ASSET_CACHE_HEADERS)


@router.get("/sites/{asset_path:path}")
def get_site_asset(
    asset_path: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    normalized = asset_path.strip().lstrip("/")
    parts = [part for part in normalized.split("/") if part]
    if len(parts) < 2:
        return _asset_error(404, "Asset not found.")

    site_name = parts[0]
    floorplan = raw_floorplans_collection.find_one(
        {"$or": [{"site_name": site_name}, {"dxf_project_id": site_name}]}
    )
    if not floorplan:
        return _asset_error(404, "Site asset not found.")
    if not _user_can_access_site(current_user, floorplan):
        return _asset_error(403, "Site asset access denied.")

    try:
        file_path = _resolve_existing_file(
            site_storage_roots(
                owner_email=floorplan.get("owner_email"),
                owner_user_id=floorplan.get("owner_user_id"),
            ),
            [normalized],
        )
    except FileNotFoundError:
        return _asset_error(404, "Asset file not found.")
    return FileResponse(file_path, headers=_ASSET_CACHE_HEADERS)
