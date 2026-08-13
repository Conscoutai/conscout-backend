# manages project asset files (DXF zip + baseline XER) and
# writes related site config/site objects updates to floorplan records.
import os
import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from core.database import floorplans_collection
from core.config import (
    ENABLE_DXF_PROCESSING,
    SITE_DXF_DIRNAME,
    site_dxf_dir,
    site_baseline_dir,
    site_dir,
    site_storage_roots,
)


def _project_filter(project_ref: str) -> dict:
    return {
        "$or": [
            {"id": project_ref},
            {"project_id": project_ref},
            {"site_name": project_ref},
            {"dxf_project_id": project_ref},
        ]
    }


def _project_storage_keys(project: dict, *extra_keys: str) -> list[str]:
    keys: list[str] = []
    for raw_value in (
        project.get("project_id"),
        project.get("id"),
        project.get("dxf_project_id"),
        project.get("site_name"),
        *extra_keys,
    ):
        value = str(raw_value or "").strip()
        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\x00" in value
            or value in keys
        ):
            continue
        keys.append(value)
    return keys


def remove_project_asset_directories(
    project: dict,
    directory_name: str,
    *extra_keys: str,
) -> tuple[int, int]:
    """Remove one exact project-asset directory from scoped and legacy roots."""
    removed_directories = 0
    removed_files = 0
    roots = site_storage_roots(
        owner_email=project.get("owner_email"),
        owner_user_id=project.get("owner_user_id"),
    )
    for root in roots:
        root_path = os.path.abspath(root)
        for storage_key in _project_storage_keys(project, *extra_keys):
            candidate = os.path.abspath(
                os.path.join(root_path, storage_key, directory_name)
            )
            try:
                is_scoped = os.path.commonpath([root_path, candidate]) == root_path
            except ValueError:
                is_scoped = False
            if not is_scoped or not os.path.isdir(candidate):
                continue
            for _, _, filenames in os.walk(candidate):
                removed_files += len(filenames)
            shutil.rmtree(candidate)
            removed_directories += 1
    return removed_directories, removed_files


def delete_project_dxf_assets(project_ref: str) -> dict:
    normalized_ref = str(project_ref or "").strip()
    if not normalized_ref:
        raise HTTPException(400, "Project reference is required")
    project = floorplans_collection.find_one(
        _project_filter(normalized_ref),
        sort=[("_id", -1)],
    )
    if not project:
        raise HTTPException(404, "Project not found")

    removed_directories, removed_files = remove_project_asset_directories(
        project,
        SITE_DXF_DIRNAME,
        normalized_ref,
    )
    now = datetime.now(timezone.utc)
    result = floorplans_collection.update_many(
        _project_filter(normalized_ref),
        {
            "$unset": {
                "dxf_project_id": "",
                "dxf_updated_at": "",
            },
            "$set": {
                "site_objects": [],
                "updated_at": now,
            },
        },
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Project not found")
    return {
        "status": "deleted",
        "asset": "dxf",
        "directories_deleted": removed_directories,
        "files_deleted": removed_files,
    }


def replace_site_dxfs_from_zip(
    site_name: str,
    zip_bytes: bytes,
    *,
    require_dxf: bool = False,
) -> bool:
    if not ENABLE_DXF_PROCESSING:
        raise HTTPException(400, "DXF processing is disabled")

    dxf_dir = site_dxf_dir(site_name)
    if os.path.isdir(dxf_dir):
        for filename in os.listdir(dxf_dir):
            path = os.path.join(dxf_dir, filename)
            if os.path.isfile(path):
                os.remove(path)
    else:
        os.makedirs(dxf_dir, exist_ok=True)

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(zip_bytes)
        tmp_path = tmp.name

    extracted_any = False
    try:
        with zipfile.ZipFile(tmp_path, "r") as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                if not member.filename.lower().endswith(".dxf"):
                    continue
                safe_name = os.path.basename(member.filename)
                if not safe_name:
                    continue
                extracted_any = True
                out_path = os.path.join(dxf_dir, safe_name)
                with zf.open(member) as src, open(out_path, "wb") as dst:
                    dst.write(src.read())
    finally:
        os.unlink(tmp_path)

    if require_dxf and not extracted_any:
        raise HTTPException(400, "No .dxf files found in the provided zip")

    return extracted_any


def save_baseline_xer(site_name: str, filename: str, raw_bytes: bytes) -> tuple[str, str]:
    baseline_dir = site_baseline_dir(site_name)
    os.makedirs(baseline_dir, exist_ok=True)
    safe_name = os.path.basename(filename)
    baseline_path = os.path.join(baseline_dir, safe_name)
    with open(baseline_path, "wb") as buffer:
        buffer.write(raw_bytes)
    return f"/sites/{site_name}/baseline/{safe_name}", safe_name


def persist_project_assets_update(
    site_name: str,
    parsed_site_config: dict,
    site_objects: list[dict],
) -> None:
    class_colors = parsed_site_config.get("class_colors", {})
    now = datetime.now(timezone.utc)
    floorplans_collection.update_many(
        _project_filter(site_name),
        {
            "$set": {
                "site_objects": site_objects,
                "site_config": {
                    "dxf_blocks": parsed_site_config.get("dxf_blocks", {}),
                    "class_colors": class_colors,
                    "ai_class_aliases": parsed_site_config.get("ai_class_aliases", {}),
                },
                "site_config_updated_at": now,
                "updated_at": now,
            }
        },
    )


def resolve_site_config_for_reprocess(
    site_name: str,
    uploaded_site_config: Optional[dict] = None,
) -> dict:
    if isinstance(uploaded_site_config, dict):
        return uploaded_site_config

    site_config_path = os.path.join(site_dir(site_name), "site_config.json")
    if os.path.isfile(site_config_path):
        try:
            with open(site_config_path, "r", encoding="utf-8") as handle:
                parsed = json.load(handle)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    latest_floorplan = floorplans_collection.find_one(
        _project_filter(site_name),
        sort=[("_id", -1)],
    )
    if latest_floorplan and isinstance(latest_floorplan.get("site_config"), dict):
        stored = latest_floorplan["site_config"]
        return {
            "dxf_blocks": stored.get("dxf_blocks", {}),
            "class_colors": stored.get("class_colors", {}),
            "ai_class_aliases": stored.get("ai_class_aliases", {}),
        }

    raise HTTPException(
        400,
        "Site config not found for this project. Upload site_config.json first.",
    )
