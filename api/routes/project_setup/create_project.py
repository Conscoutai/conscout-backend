from dataclasses import dataclass
from typing import Generator, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends

from core.config import DEFAULT_SITE_NAME, ENABLE_DXF_PROCESSING
from core.auth import ensure_admin_user, require_authenticated_user
from core.auth_context import AuthenticatedUser
from services.project_setup.floorplan_service import create_floorplan
from services.project_setup.site_config_service import (
    save_site_config_and_try_parse,
    upsert_floorplan_site_config,
)
from services.project_setup.site_config_generation_service import (
    generate_site_config_from_saved_dxfs,
)
from services.project_setup.project_assets_service import (
    replace_site_dxfs_from_zip,
    save_baseline_xer,
)
from services.subscription_access_service import (
    release_lite_project_creation,
    reserve_lite_project_creation,
)
from services.progress.work_schedule.baseline_service import import_schedule_baseline


router = APIRouter(tags=["Floorplans"])

# Creates a project floorplan upload. Project names are display labels and are
# deliberately not used as identifiers: duplicate names are valid.
# Accepts calibration points, optional DXF zip, optional site config JSON, optional baseline XER.
# Triggers floorplan creation + optional DXF processing + optional config attach.


@dataclass(frozen=True)
class ProjectCreationAccess:
    user: AuthenticatedUser
    requested_project_id: str


def require_project_creation_access(
    project_id: Optional[str] = Form(None),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
) -> Generator[ProjectCreationAccess, None, None]:
    ensure_admin_user(current_user)
    requested_project_id = (project_id or "").strip()
    lease_token = reserve_lite_project_creation(
        user_id=current_user.user_id,
        requested_project_id=requested_project_id,
    )
    try:
        yield ProjectCreationAccess(
            user=current_user,
            requested_project_id=requested_project_id,
        )
    finally:
        release_lite_project_creation(
            user_id=current_user.user_id,
            lease_token=lease_token,
        )


@router.post("/projects/{site_name}/floorplans")
async def create_project_floorplan(
    site_name: str,
    file: UploadFile = File(...),
    name: str = Form(...),
    pointA_px: Optional[float] = Form(None),
    pointA_py: Optional[float] = Form(None),
    pointA_lat: Optional[float] = Form(None),
    pointA_lon: Optional[float] = Form(None),
    pointB_px: Optional[float] = Form(None),
    pointB_py: Optional[float] = Form(None),
    pointB_lat: Optional[float] = Form(None),
    pointB_lon: Optional[float] = Form(None),
    calibration_points: Optional[str] = Form(None),
    dxf_project_id: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    project_location: Optional[str] = Form(None),
    area_location: Optional[str] = Form(None),
    site_name_form: Optional[str] = Form(None, alias="site_name"),
    dxf_zip: Optional[UploadFile] = File(None),
    site_config: Optional[UploadFile] = File(None),
    baseline_xer: Optional[UploadFile] = File(None),
    capture_mode: Literal["outdoor", "indoor"] = Form("outdoor"),
    currency_code: Optional[str] = Form(None),
    currency: Optional[str] = Form(None),
    creation_access: ProjectCreationAccess = Depends(require_project_creation_access),
):
    current_user = creation_access.user
    normalized_form_site = (site_name_form or "").strip()
    if normalized_form_site and normalized_form_site != site_name:
        raise HTTPException(400, "Path site_name and form site_name must match")

    effective_site = site_name or DEFAULT_SITE_NAME
    # The client may provide an ID for retries. Otherwise the server creates
    # one before any files are written so every project's assets stay isolated.
    effective_project_id = (
        creation_access.requested_project_id or f"floorplan_{uuid4().hex}"
    )
    parsed_site_config = None
    if site_config:
        if not site_config.filename or not site_config.filename.lower().endswith(
            ".json"
        ):
            raise HTTPException(400, "Site config must be a .json file")
        raw_bytes = await site_config.read()
        parsed_site_config = save_site_config_and_try_parse(
            effective_project_id, raw_bytes
        )

    if dxf_zip and ENABLE_DXF_PROCESSING:
        replace_site_dxfs_from_zip(
            effective_project_id, await dxf_zip.read(), require_dxf=False
        )
        if not isinstance(parsed_site_config, dict):
            generated = generate_site_config_from_saved_dxfs(effective_project_id)
            parsed_site_config = generated["site_config"]
        dxf_project_id = effective_project_id
    else:
        dxf_project_id = None

    baseline_xer_url = None
    baseline_xer_name = None
    baseline_xer_bytes = None
    if baseline_xer:
        if not baseline_xer.filename or not baseline_xer.filename.lower().endswith(
            ".xer"
        ):
            raise HTTPException(400, "Baseline file must be a .xer")
        baseline_xer_bytes = await baseline_xer.read()
        baseline_xer_url, baseline_xer_name = save_baseline_xer(
            effective_project_id,
            baseline_xer.filename,
            baseline_xer_bytes,
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
        project_id=effective_project_id,
        dxf_project_id=dxf_project_id,
        baseline_xer_url=baseline_xer_url,
        baseline_xer_name=baseline_xer_name,
        capture_mode=capture_mode,
        currency_code=currency_code or currency,
        location=location or project_location or area_location,
        owner_user_id=current_user.user_id,
        owner_email=current_user.email,
        owner_name=current_user.name,
    )
    if isinstance(parsed_site_config, dict):
        floorplan_id = result.get("floorPlan", {}).get("id")
        upsert_floorplan_site_config(
            effective_project_id, parsed_site_config, floorplan_id
        )

    if baseline_xer_bytes is not None and baseline_xer_name:
        result["schedule_baseline_import"] = import_schedule_baseline(
            project_ref=effective_project_id,
            filename=baseline_xer_name,
            raw_bytes=baseline_xer_bytes,
            activate=True,
        )

    return result
