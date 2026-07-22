from typing import Literal

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from pydantic import BaseModel

from core.database import (
    floorplans_collection,
    raw_floorplans_collection,
    users_collection,
)
from core.auth import ensure_admin_user, require_authenticated_user
from core.auth_context import AuthenticatedUser
from core.config import ENABLE_DXF_PROCESSING
from services.project_setup.dxf_service import DXFService
from services.project_setup.floorplan_normalization_service import (
    normalize_floorplan,
)
from services.project_setup.site_config_service import (
    save_site_config_strict,
    upsert_floorplan_site_config,
)
from services.project_setup.site_config_generation_service import (
    generate_site_config_from_saved_dxfs,
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


class CurrencyRequest(BaseModel):
    currency_code: str


class StakeholderEmailRequest(BaseModel):
    email: str


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _email_username(email: str) -> str:
    normalized = _normalize_email(email)
    if "@" in normalized:
        return normalized.split("@", 1)[0]
    return normalized


def _registered_user_exists(email: str) -> bool:
    normalized_email = _normalize_email(email)
    if not normalized_email:
        return False
    return (
        users_collection.find_one(
            {"email": normalized_email},
            {"_id": 1},
        )
        is not None
    )


def _build_stakeholder_members(stakeholder_emails: list[str]) -> list[dict]:
    normalized_emails = []
    seen = set()
    for email in stakeholder_emails:
        normalized = _normalize_email(str(email))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        normalized_emails.append(normalized)

    if not normalized_emails:
        return []

    user_lookup = {}
    for user in users_collection.find(
        {"email": {"$in": normalized_emails}},
        {"email": 1, "name": 1, "role": 1},
    ):
        email = _normalize_email(str(user.get("email") or ""))
        if not email:
            continue
        user_lookup[email] = user

    members = []
    for email in normalized_emails:
        user = user_lookup.get(email, {})
        name = str(user.get("name") or "").strip() or _email_username(email)
        role = str(user.get("role") or "stakeholder").strip() or "stakeholder"
        members.append({"email": email, "name": name, "role": role})
    return members


def _project_filter(project_ref: str) -> dict:
    """Resolve ID-based requests while retaining legacy name routes."""
    value = project_ref.strip()
    return {
        "$or": [
            {"id": value},
            {"project_id": value},
            {"site_name": value},
            {"dxf_project_id": value},
        ]
    }

#Lists projects
@router.get("/projects")
def list_projects(
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    floorplan_ids = {
        floorplan_id.strip()
        for floorplan_id in current_user.accessible_floorplan_ids
        if floorplan_id.strip()
    }
    query = {
        "$or": [
            {"owner_user_id": current_user.user_id},
            {"owner_email": current_user.email.strip().lower()},
            {"stakeholder_emails": current_user.email.strip().lower()},
        ]
    }
    if floorplan_ids:
        query["$or"].append({"id": {"$in": list(floorplan_ids)}})

    floorplans = list(raw_floorplans_collection.find(query).sort("_id", -1))
    projects = []
    for fp in floorplans:
        if not str(fp.get("id") or fp.get("project_id") or "").strip():
            continue
        fp["_id"] = str(fp["_id"])
        normalized = normalize_floorplan(fp)
        normalized["stakeholder_members"] = _build_stakeholder_members(
            normalized.get("stakeholder_emails", [])
        )
        projects.append(normalized)
    return projects

# Fetches latest floorplan for that site.
@router.get("/projects/{site_name}/floorplan")
def get_project_floorplan(
    site_name: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    fp = floorplans_collection.find_one(
        _project_filter(site_name),
        sort=[("_id", -1)],
    )
    if not fp:
        raise HTTPException(404, "No floorplan found for this project")
    fp["_id"] = str(fp["_id"])
    normalized = normalize_floorplan(fp)
    normalized["stakeholder_members"] = _build_stakeholder_members(
        normalized.get("stakeholder_emails", [])
    )
    return normalized

#Deletes a project and related assets.
@router.delete("/projects/{site_name}")
def delete_project(
    site_name: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    if not site_name:
        raise HTTPException(400, "Project name is required")

    delete_project_service(site_name)
    return {"message": "Project deleted"}


@router.patch("/projects/{site_name}/rename")
def rename_project(
    site_name: str,
    payload: RenameProjectRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    rename_project_service(site_name, payload.new_site_name)
    return {
        "status": "renamed",
        "old_site_name": site_name,
        "new_site_name": payload.new_site_name.strip(),
    }


@router.patch("/projects/{site_name}/capture-mode")
def update_project_capture_mode(
    site_name: str,
    payload: CaptureModeRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    if not site_name:
        raise HTTPException(400, "Project name is required")

    update = floorplans_collection.update_many(
        _project_filter(site_name),
        {"$set": {"capture_mode": payload.capture_mode}},
    )
    if update.matched_count == 0:
        raise HTTPException(404, "No floorplan found for this project")

    return {
        "status": "updated",
        "site_name": site_name,
        "capture_mode": payload.capture_mode,
    }


@router.patch("/projects/{site_name}/currency")
def update_project_currency(
    site_name: str,
    payload: CurrencyRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    if not site_name:
        raise HTTPException(400, "Project name is required")

    currency_code = payload.currency_code.strip().upper()
    if not currency_code:
        raise HTTPException(400, "Currency type is required")

    update = floorplans_collection.update_many(
        _project_filter(site_name),
        {"$set": {"currency_code": currency_code, "currency": currency_code}},
    )
    if update.matched_count == 0:
        raise HTTPException(404, "No floorplan found for this project")

    return {
        "status": "updated",
        "site_name": site_name,
        "currency_code": currency_code,
        "currency": currency_code,
    }


#Uploads/updates site config JSON.
@router.put("/projects/{site_name}/site-config")
async def upload_site_config(
    site_name: str,
    site_config: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
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
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
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
        if not isinstance(parsed, dict):
            generated = generate_site_config_from_saved_dxfs(site_name)
            parsed = generated["site_config"]
    parsed_for_reprocess = resolve_site_config_for_reprocess(site_name, parsed)

    latest_floorplan = floorplans_collection.find_one(
        _project_filter(site_name),
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


@router.post("/projects/{site_name}/site-config/generate")
async def generate_site_config(
    site_name: str,
    dxf_zip: UploadFile = File(None),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    if not site_name:
        raise HTTPException(400, "Site name is required")
    if dxf_zip and (not dxf_zip.filename or not dxf_zip.filename.lower().endswith(".zip")):
        raise HTTPException(400, "DXF upload must be a .zip file")
    if not ENABLE_DXF_PROCESSING:
        raise HTTPException(400, "DXF processing is disabled")

    if dxf_zip:
        replace_site_dxfs_from_zip(site_name, await dxf_zip.read(), require_dxf=True)

    generated = generate_site_config_from_saved_dxfs(site_name)
    upsert_floorplan_site_config(site_name, generated["site_config"])

    return {
        "message": "Site config generated successfully",
        **generated,
    }


@router.get("/projects/{site_name}/stakeholders")
def list_project_stakeholders(
    site_name: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    floorplan = floorplans_collection.find_one(
        _project_filter(site_name),
        sort=[("_id", -1)],
    )
    if not floorplan:
        raise HTTPException(404, "No floorplan found for this project")
    stakeholder_emails = floorplan.get("stakeholder_emails", [])
    return {
        "site_name": site_name,
        "stakeholder_emails": stakeholder_emails,
        "stakeholder_members": _build_stakeholder_members(stakeholder_emails),
    }


@router.post("/projects/{site_name}/stakeholders")
def add_project_stakeholder(
    site_name: str,
    payload: StakeholderEmailRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    normalized_email = payload.email.strip().lower()
    if not _registered_user_exists(normalized_email):
        raise HTTPException(404, "No registered user found for this email")
    update = floorplans_collection.update_many(
        _project_filter(site_name),
        {"$addToSet": {"stakeholder_emails": normalized_email}},
    )
    if update.matched_count == 0:
        raise HTTPException(404, "No floorplan found for this project")
    return {
        "message": "Stakeholder added",
        "site_name": site_name,
        "email": normalized_email,
    }


@router.delete("/projects/{site_name}/stakeholders")
def remove_project_stakeholder(
    site_name: str,
    payload: StakeholderEmailRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    normalized_email = payload.email.strip().lower()
    update = floorplans_collection.update_many(
        _project_filter(site_name),
        {"$pull": {"stakeholder_emails": normalized_email}},
    )
    if update.matched_count == 0:
        raise HTTPException(404, "No floorplan found for this project")
    return {
        "message": "Stakeholder removed",
        "site_name": site_name,
        "email": normalized_email,
    }
