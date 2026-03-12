import os
import shutil

from fastapi import HTTPException

from core.database import floorplans_collection, tours_collection, work_schedules_collection
from core.config import (
    DEFAULT_SITE_NAME,
    SITES_DIR,
    site_dir,
    site_floorplan_dir,
)


def delete_floorplan_image(fp: dict) -> None:
    image_url = fp.get("imageUrl", "")
    image_name = os.path.basename(image_url)
    site_name = fp.get("site_name") or fp.get("dxf_project_id") or DEFAULT_SITE_NAME
    image_path = os.path.join(site_floorplan_dir(site_name), image_name)
    if image_url.startswith("/sites/"):
        rel = image_url.replace("/sites/", "").lstrip("/").replace("/", os.sep)
        image_path = os.path.join(SITES_DIR, rel)
    if os.path.exists(image_path):
        os.remove(image_path)


def delete_project(site_name: str) -> None:
    floorplans_collection.delete_many(
        {"$or": [{"site_name": site_name}, {"dxf_project_id": site_name}]}
    )
    project_dir = site_dir(site_name)
    if os.path.isdir(project_dir):
        shutil.rmtree(project_dir, ignore_errors=True)


def _rewrite_site_path(value: str, old_site_name: str, new_site_name: str) -> str:
    if not isinstance(value, str):
        return value
    old_token = f"/sites/{old_site_name}/"
    new_token = f"/sites/{new_site_name}/"
    if old_token in value:
        return value.replace(old_token, new_token)
    return value


def rename_project(old_site_name: str, new_site_name: str) -> None:
    old_site = (old_site_name or "").strip()
    new_site = (new_site_name or "").strip()

    if not old_site or not new_site:
        raise HTTPException(400, "Both old and new site names are required")
    if old_site == new_site:
        raise HTTPException(400, "New site name must be different")

    existing = floorplans_collection.find_one(
        {"$or": [{"site_name": new_site}, {"dxf_project_id": new_site}]}
    )
    if existing or os.path.isdir(site_dir(new_site)):
        raise HTTPException(409, "Project with this name already exists")

    floorplans = list(
        floorplans_collection.find(
            {"$or": [{"site_name": old_site}, {"dxf_project_id": old_site}]}
        )
    )
    if not floorplans:
        raise HTTPException(404, "Project not found")

    old_dir = site_dir(old_site)
    new_dir = site_dir(new_site)
    if os.path.isdir(old_dir):
        shutil.move(old_dir, new_dir)

    for fp in floorplans:
        updates = {
            "site_name": new_site,
            "dxf_project_id": new_site,
        }
        image_url = fp.get("imageUrl")
        if isinstance(image_url, str):
            updates["imageUrl"] = _rewrite_site_path(image_url, old_site, new_site)
        baseline_url = fp.get("baseline_xer_url")
        if isinstance(baseline_url, str):
            updates["baseline_xer_url"] = _rewrite_site_path(
                baseline_url, old_site, new_site
            )
        floorplans_collection.update_one({"_id": fp["_id"]}, {"$set": updates})

    tours_collection.update_many(
        {"site_name": old_site},
        {"$set": {"site_name": new_site}},
    )
    work_schedules_collection.update_many(
        {"project_id": old_site},
        {"$set": {"project_id": new_site}},
    )
