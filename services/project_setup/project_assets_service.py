# manages project asset files (DXF zip + baseline XER) and
# writes related site config/site objects updates to floorplan records.
import os
import json
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from core.database import floorplans_collection
from core.config import (
    ENABLE_DXF_PROCESSING,
    site_dxf_dir,
    site_baseline_dir,
    site_dir,
)


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
        {"$or": [{"site_name": site_name}, {"dxf_project_id": site_name}]},
        {
            "$set": {
                "dxf_project_id": site_name,
                "site_name": site_name,
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
        {"$or": [{"site_name": site_name}, {"dxf_project_id": site_name}]},
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
