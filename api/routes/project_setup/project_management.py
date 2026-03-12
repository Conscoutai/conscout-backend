from typing import Literal

from pydantic import BaseModel
from fastapi import APIRouter, UploadFile, File, HTTPException

from core.database import floorplans_collection
from core.config import ENABLE_DXF_PROCESSING
from services.project_setup.dxf_service import DXFService
from services.project_setup.floorplan_normalization_service import (
    normalize_floorplan,
)
from services.project_setup.site_config_service import (
    save_site_config_strict,
    upsert_floorplan_site_config,
)
from services.project_setup.project_assets_service import (
    replace_site_dxfs_from_zip,
    persist_project_assets_update,
    resolve_site_config_for_reprocess,
)
from services.project_setup.project_lifecycle_service import (
    delete_project as delete_project_service,
    rename_project as rename_project_service,
)


router = APIRouter(tags=["Floorplans"])
dxf_service = DXFService()


class RenameProjectRequest(BaseModel):
    new_site_name: str


class CaptureModeRequest(BaseModel):
    capture_mode: Literal["outdoor", "indoor"]

#Lists projects
@router.get("/projects")
def list_projects():
    floorplans = list(floorplans_collection.find().sort("_id", -1))
    seen_sites = set()
    projects = []
    for fp in floorplans:
        site_name = fp.get("site_name") or fp.get("dxf_project_id")
        if not site_name or site_name in seen_sites:
            continue
        seen_sites.add(site_name)
        fp["_id"] = str(fp["_id"])
        projects.append(normalize_floorplan(fp))
    return projects

# Fetches latest floorplan for that site.
@router.get("/projects/{site_name}/floorplan")
def get_project_floorplan(site_name: str):
    fp = floorplans_collection.find_one(
        {"$or": [{"site_name": site_name}, {"dxf_project_id": site_name}]},
        sort=[("_id", -1)],
    )
    if not fp:
        raise HTTPException(404, "No floorplan found for this project")
    fp["_id"] = str(fp["_id"])
    return normalize_floorplan(fp)

#Deletes a project and related assets.
@router.delete("/projects/{site_name}")
def delete_project(site_name: str):
    if not site_name:
        raise HTTPException(400, "Project name is required")

    delete_project_service(site_name)
    return {"message": "Project deleted"}


@router.patch("/projects/{site_name}/rename")
def rename_project(site_name: str, payload: RenameProjectRequest):
    rename_project_service(site_name, payload.new_site_name)
    return {
        "status": "renamed",
        "old_site_name": site_name,
        "new_site_name": payload.new_site_name.strip(),
    }


@router.patch("/projects/{site_name}/capture-mode")
def update_project_capture_mode(site_name: str, payload: CaptureModeRequest):
    if not site_name:
        raise HTTPException(400, "Project name is required")

    update = floorplans_collection.update_many(
        {"$or": [{"site_name": site_name}, {"dxf_project_id": site_name}]},
        {"$set": {"capture_mode": payload.capture_mode}},
    )
    if update.matched_count == 0:
        raise HTTPException(404, "No floorplan found for this project")

    return {
        "status": "updated",
        "site_name": site_name,
        "capture_mode": payload.capture_mode,
    }


#Uploads/updates site config JSON.
@router.put("/projects/{site_name}/site-config")
async def upload_site_config(site_name: str, site_config: UploadFile = File(...)):
    if not site_name:
        raise HTTPException(400, "Site name is required")
    if not site_config.filename or not site_config.filename.lower().endswith(".json"):
        raise HTTPException(400, "Site config must be a .json file")

    parsed = save_site_config_strict(site_name, await site_config.read())
    upsert_floorplan_site_config(site_name, parsed)
    return {"message": "Site config updated", "site_name": site_name}


@router.put("/projects/{site_name}/assets")
async def update_project_assets(
    site_name: str,
    site_config: UploadFile = File(None),
    dxf_zip: UploadFile = File(None),
):
    if not site_name:
        raise HTTPException(400, "Site name is required")
    if not site_config and not dxf_zip:
        raise HTTPException(400, "At least one asset (site_config or dxf_zip) is required")
    if site_config and (
        not site_config.filename or not site_config.filename.lower().endswith(".json")
    ):
        raise HTTPException(400, "Site config must be a .json file")
    if dxf_zip and (not dxf_zip.filename or not dxf_zip.filename.lower().endswith(".zip")):
        raise HTTPException(400, "DXF upload must be a .zip file")
    if not ENABLE_DXF_PROCESSING:
        raise HTTPException(400, "DXF processing is disabled")

    parsed = None
    if site_config:
        parsed = save_site_config_strict(site_name, await site_config.read())
    if dxf_zip:
        replace_site_dxfs_from_zip(site_name, await dxf_zip.read(), require_dxf=True)
    parsed_for_reprocess = resolve_site_config_for_reprocess(site_name, parsed)

    latest_floorplan = floorplans_collection.find_one(
        {"$or": [{"site_name": site_name}, {"dxf_project_id": site_name}]},
        sort=[("_id", -1)],
    )
    if not latest_floorplan:
        raise HTTPException(404, "No floorplan found for this site")

    try:
        site_objects = dxf_service.process_project_dxfs(site_name, latest_floorplan)
    except FileNotFoundError as err:
        raise HTTPException(
            400,
            "DXF files not found for this project. Upload a DXF zip before reprocessing.",
        ) from err
    persist_project_assets_update(site_name, parsed_for_reprocess, site_objects)

    return {
        "message": "Saved successfully",
        "site_name": site_name,
        "updated": {
            "site_config": bool(site_config),
            "dxf": bool(dxf_zip),
        },
        "total_objects": len(site_objects),
    }
