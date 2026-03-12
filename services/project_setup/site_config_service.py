# saves/validates project site_config.json and updates that config into floorplan records in the database.
import json
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from core.database import floorplans_collection
from core.config import site_dir


def save_site_config_and_try_parse(site_name: str, raw_bytes: bytes) -> Optional[dict]:
    os.makedirs(site_dir(site_name), exist_ok=True)
    target_path = os.path.join(site_dir(site_name), "site_config.json")
    with open(target_path, "wb") as buffer:
        buffer.write(raw_bytes)
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def save_site_config_strict(site_name: str, raw_bytes: bytes) -> dict:
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(400, f"Invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(400, "Config must be a JSON object")

    os.makedirs(site_dir(site_name), exist_ok=True)
    target_path = os.path.join(site_dir(site_name), "site_config.json")
    with open(target_path, "wb") as buffer:
        buffer.write(raw_bytes)
    return parsed


def upsert_floorplan_site_config(
    site_name: str,
    parsed_site_config: dict,
    floorplan_id: Optional[str] = None,
) -> None:
    class_colors = parsed_site_config.get("class_colors", {})
    payload = {
        "site_config": {
            "dxf_blocks": parsed_site_config.get("dxf_blocks", {}),
            "class_colors": class_colors,
            "ai_class_aliases": parsed_site_config.get("ai_class_aliases", {}),
        },
        "site_config_updated_at": datetime.now(timezone.utc),
    }

    if floorplan_id:
        floorplans_collection.update_one(
            {"id": floorplan_id},
            {"$set": payload},
        )
        return

    floorplans_collection.update_many(
        {"site_name": site_name},
        {"$set": payload},
    )
