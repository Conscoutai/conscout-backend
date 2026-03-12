from typing import Literal, Optional

from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from core.config import DEFAULT_SITE_NAME, ENABLE_DXF_PROCESSING
from services.project_setup.floorplan_service import create_floorplan
from services.project_setup.site_config_service import (
    save_site_config_and_try_parse,
    upsert_floorplan_site_config,
)
from services.project_setup.project_assets_service import (
    replace_site_dxfs_from_zip,
    save_baseline_xer,
)


router = APIRouter(tags=["Floorplans"])

# Creates/updates a project floorplan upload.
# Accepts calibration points, optional DXF zip, optional site config JSON, optional baseline XER.
# Triggers floorplan creation + optional DXF processing + optional config attach.

@router.post("/projects/{site_name}/floorplans")
async def create_project_floorplan(
    site_name: str,
    file: UploadFile = File(...),
    name: str = Form(...),
    pointA_px: float = Form(...),
    pointA_py: float = Form(...),
    pointA_lat: float = Form(...),
    pointA_lon: float = Form(...),
    pointB_px: float = Form(...),
    pointB_py: float = Form(...),
    pointB_lat: float = Form(...),
    pointB_lon: float = Form(...),
    calibration_points: Optional[str] = Form(None),
    dxf_project_id: Optional[str] = Form(None),
    site_name_form: Optional[str] = Form(None, alias="site_name"),
    dxf_zip: Optional[UploadFile] = File(None),
    site_config: Optional[UploadFile] = File(None),
    baseline_xer: Optional[UploadFile] = File(None),
    capture_mode: Literal["outdoor", "indoor"] = Form("outdoor"),
):
    normalized_form_site = (site_name_form or "").strip()
    if normalized_form_site and normalized_form_site != site_name:
        raise HTTPException(400, "Path site_name and form site_name must match")

    effective_site = site_name or dxf_project_id or DEFAULT_SITE_NAME
    parsed_site_config = None
    if site_config:
        if not site_config.filename or not site_config.filename.lower().endswith(".json"):
            raise HTTPException(400, "Site config must be a .json file")
        raw_bytes = await site_config.read()
        parsed_site_config = save_site_config_and_try_parse(effective_site, raw_bytes)

    if dxf_zip and ENABLE_DXF_PROCESSING:
        replace_site_dxfs_from_zip(effective_site, await dxf_zip.read(), require_dxf=False)
        dxf_project_id = effective_site
    else:
        dxf_project_id = None

    baseline_xer_url = None
    baseline_xer_name = None
    if baseline_xer:
        if not baseline_xer.filename or not baseline_xer.filename.lower().endswith(".xer"):
            raise HTTPException(400, "Baseline file must be a .xer")
        baseline_xer_url, baseline_xer_name = save_baseline_xer(
            effective_site,
            baseline_xer.filename,
            await baseline_xer.read(),
        )

    result = create_floorplan(
        file=file,
        name=name,
        pointA_px=pointA_px,
        pointA_py=pointA_py,
        pointA_lat=pointA_lat,
        pointA_lon=pointA_lon,
        pointB_px=pointB_px,
        pointB_py=pointB_py,
        pointB_lat=pointB_lat,
        pointB_lon=pointB_lon,
        calibration_points=calibration_points,
        site_name=effective_site,
        dxf_project_id=dxf_project_id,
        baseline_xer_url=baseline_xer_url,
        baseline_xer_name=baseline_xer_name,
        capture_mode=capture_mode,
    )
    if isinstance(parsed_site_config, dict):
        floorplan_id = result.get("floorPlan", {}).get("id")
        upsert_floorplan_site_config(effective_site, parsed_site_config, floorplan_id)

    return result
