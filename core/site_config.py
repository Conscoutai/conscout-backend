# Site-specific configuration loader.
# Keeps per-site settings out of core config.

import json
import os
from typing import Any, Dict, Optional

from core.config import DEFAULT_SITE_NAME, site_dir
from core.database import floorplans_collection

_SITE_CONFIG_CACHE: Dict[str, Dict[str, Any]] = {}
_SITE_CONFIG_MTIME: Dict[str, Optional[float]] = {}
_SITE_CONFIG_DB_REV: Dict[str, Optional[Any]] = {}


def _site_config_path(site_name: str) -> str:
    return os.path.join(site_dir(site_name), "site_config.json")


def _load_json_config(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _normalize_colors(colors: Dict[str, Any]) -> Dict[str, tuple]:
    normalized: Dict[str, tuple] = {}
    for name, value in colors.items():
        if isinstance(value, (list, tuple)) and len(value) == 3:
            try:
                normalized[name] = tuple(int(v) for v in value)
            except (TypeError, ValueError):
                continue
    return normalized


def load_site_config(site_name: Optional[str] = None) -> Dict[str, Any]:
    site = site_name or DEFAULT_SITE_NAME
    path = _site_config_path(site)
    mtime = os.path.getmtime(path) if os.path.isfile(path) else None
    db_doc = floorplans_collection.find_one({"site_name": site}, sort=[("_id", -1)])
    db_rev = db_doc.get("site_config_updated_at") if db_doc else None
    db_raw = db_doc.get("site_config") if db_doc else None
    if isinstance(db_doc, dict) and (not db_raw or not isinstance(db_raw, dict)):
        colors = db_doc.get("site_object_colors")
        if colors:
            db_raw = {"class_colors": colors}

    cached = _SITE_CONFIG_CACHE.get(site)
    if cached is not None and _SITE_CONFIG_MTIME.get(site) == mtime and _SITE_CONFIG_DB_REV.get(site) == db_rev:
        return cached

    raw = db_raw if isinstance(db_raw, dict) and db_raw else _load_json_config(path)
    merged = {
        "dxf_blocks": raw.get("dxf_blocks", {}),
        "class_colors": raw.get("class_colors", {}),
        "ai_class_aliases": raw.get("ai_class_aliases", {}),
    }
    merged["class_colors"] = _normalize_colors(merged["class_colors"])

    _SITE_CONFIG_CACHE[site] = merged
    _SITE_CONFIG_MTIME[site] = mtime
    _SITE_CONFIG_DB_REV[site] = db_rev
    return merged


def get_dxf_blocks(site_name: Optional[str] = None) -> Dict[str, Any]:
    return load_site_config(site_name).get("dxf_blocks", {})


def get_class_colors(site_name: Optional[str] = None) -> Dict[str, tuple]:
    return load_site_config(site_name).get("class_colors", {})


def get_ai_class_aliases(site_name: Optional[str] = None) -> Dict[str, str]:
    return load_site_config(site_name).get("ai_class_aliases", {})
