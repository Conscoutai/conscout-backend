import json
import os
from pathlib import Path
from typing import Any, Optional

import ezdxf
from fastapi import HTTPException

from core.config import site_dir, site_dxf_dir
from core.database import floorplans_collection

DEFAULT_CLASS_COLORS = {
    "palm": [34, 139, 34],
    "tree": [0, 120, 0],
    "shrubs": [50, 170, 90],
    "tree_grate": [150, 150, 150],
    "seating_family": [200, 80, 40],
    "seating_kubb_cc": [255, 160, 0],
    "seating_demetray": [170, 60, 200],
    "shade_structure": [255, 100, 0],
    "street_light_pole": [0, 210, 255],
    "bike_rack": [0, 180, 255],
    "sculpture": [255, 0, 255],
    "vehicle": [100, 100, 255],
    "ev_charger": [0, 255, 150],
    "kiosk": [180, 100, 50],
    "trash_bin": [120, 120, 120],
    "worker": [255, 0, 0],
    "cycle_path": [0, 100, 255],
    "interlock_pavers": [200, 120, 60],
    "pedestrian_path": [255, 255, 0],
    "default": [200, 200, 200],
}

DEFAULT_AI_CLASS_ALIASES = {
    "street_light_poles": "street_light_pole",
    "street_lightpole": "street_light_pole",
    "bench": "seating_family",
    "curved_bench": "seating_kubb_cc",
    "interloack_pavers": "interlock_pavers",
}

_EXACT_BLOCK_CLASS_MAP = {
    "PALM-S": "palm",
    "PALMFRONT01": "palm",
    "PALM_TREE": "palm",
    "ALBIZIA": "tree",
    "AZADIRACHTA INDICA": "tree",
    "DELONIK REGIA": "tree",
    "CONOCARPUS LANCIFOLIA": "tree",
    "TERMINALIA": "tree",
    "TREE-8M-ARID": "tree",
    "TREE_TALL_001_FRONT": "tree",
    "NGTONIA": "tree",
    "HEDGE": "shrubs",
    "SHRUB_LITTLE_001_FRONT": "shrubs",
    "STEEL GRATE 01": "tree_grate",
    "TREE STAKES": "tree_grate",
    "BENCH C": "seating_family",
    "BENCH STRAIGHT": "seating_family",
    "BENCH Y": "seating_family",
    "SEATING 04": "seating_family",
    "BENCH 02": "seating_family",
    "BENCH CURVED": "seating_kubb_cc",
    "DOUBLE CURVED BENCH": "seating_kubb_cc",
    "SEATING CONC 01": "seating_kubb_cc",
    "OLIMPO SEAT": "seating_demetray",
    "COCN COFFEE TABLE": "seating_demetray",
    "SHADE LONG 02": "shade_structure",
    "SHADE LONG 03": "shade_structure",
    "SHADE LONG 04": "shade_structure",
    "SHADE LONG TYPE 01": "shade_structure",
    "SHADE SHORT 03": "shade_structure",
    "SHADE SHORT 04": "shade_structure",
    "SHADE SHORT TYPE 01": "shade_structure",
    "SHADE SHORT TYPE 011": "shade_structure",
    "SHADE SHORTER TYPE 022": "shade_structure",
    "SAHDE SHORTER TYPE 033": "shade_structure",
    "PLUTONE BOLLARD": "street_light_pole",
    "BOLLARDS": "street_light_pole",
    "BIKE": "bike_rack",
    "BIKE RACK UNIT": "bike_rack",
    "MARKING BIKE": "bike_rack",
    "SCULPTURE": "sculpture",
    "STAR SHAPED BECNH": "sculpture",
    "CAR1": "vehicle",
    "CHARGE POINT": "ev_charger",
    "KIOSKS": "kiosk",
    "LITTRE BIN": "trash_bin",
    "SKATER": "worker",
}

_KEYWORD_RULES = [
    ("palm", ("PALM",)),
    ("tree_grate", ("GRATE", "STAKE")),
    ("shade_structure", ("SHADE",)),
    ("bike_rack", ("BIKE",)),
    ("ev_charger", ("CHARGE", "EV")),
    ("trash_bin", ("BIN", "LITTER", "LITTRE", "TRASH")),
    ("kiosk", ("KIOSK",)),
    ("vehicle", ("CAR", "VEHICLE")),
    ("worker", ("SKATER", "WORKER", "PERSON", "PEDESTRIAN")),
    ("sculpture", ("SCULPT", "ART")),
    ("street_light_pole", ("LIGHT", "LAMP", "BOLLARD", "POLE")),
    ("seating_kubb_cc", ("CURVED BENCH", "CURVED", "CONC")),
    ("seating_demetray", ("OLIMPO", "COFFEE TABLE")),
    ("seating_family", ("BENCH", "SEAT", "SEATING")),
    ("shrubs", ("SHRUB", "HEDGE", "BUSH")),
    (
        "tree",
        (
            "TREE",
            "ALBIZIA",
            "AZADIRACHTA",
            "DELONIK",
            "CONOCARPUS",
            "TERMINALIA",
            "NGTONIA",
        ),
    ),
]


def _site_config_path(site_name: str) -> str:
    return os.path.join(site_dir(site_name), "site_config.json")


def _load_existing_site_config(site_name: str) -> dict[str, Any]:
    path = _site_config_path(site_name)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
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
            "site_name": site_name,
            "site_type": str(latest_floorplan.get("site_type") or "urban_landscape"),
            "environment": str(latest_floorplan.get("capture_mode") or "outdoor"),
            "camera_type": "360_degree",
            "coordinate_system": "local_to_dxf",
            "dxf_blocks": stored.get("dxf_blocks", {}),
            "class_colors": stored.get("class_colors", {}),
            "ai_class_aliases": stored.get("ai_class_aliases", {}),
        }

    return {}


def _save_generated_site_config(site_name: str, config: dict[str, Any]) -> None:
    os.makedirs(site_dir(site_name), exist_ok=True)
    with open(_site_config_path(site_name), "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)


def _iter_dxf_block_names(site_name: str) -> list[str]:
    dxf_dir = Path(site_dxf_dir(site_name))
    if not dxf_dir.exists():
        raise HTTPException(400, "DXF files not found for this project")

    block_names = set()
    for dxf_file in dxf_dir.glob("*.dxf"):
        try:
            doc = ezdxf.readfile(dxf_file)
            modelspace = doc.modelspace()
        except Exception:
            continue

        for entity in modelspace.query("INSERT"):
            block_name = str(entity.dxf.name or "").strip().upper()
            if not block_name or block_name.startswith("*"):
                continue
            block_names.add(block_name)

    if not block_names:
        raise HTTPException(400, "No DXF block references found in the uploaded files")

    return sorted(block_names)


def _normalize_existing_block_map(existing_dxf_blocks: Any) -> dict[str, str]:
    normalized: dict[str, str] = {}
    if not isinstance(existing_dxf_blocks, dict):
        return normalized

    for class_name, blocks in existing_dxf_blocks.items():
        if not isinstance(class_name, str) or not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, str) and block.strip():
                normalized[block.strip().upper()] = class_name
    return normalized


def _classify_block(block_name: str, existing_map: dict[str, str]) -> Optional[str]:
    existing = existing_map.get(block_name)
    if existing:
        return existing

    exact = _EXACT_BLOCK_CLASS_MAP.get(block_name)
    if exact:
        return exact

    for class_name, keywords in _KEYWORD_RULES:
        if any(keyword in block_name for keyword in keywords):
            return class_name
    return None


def generate_site_config_from_saved_dxfs(site_name: str) -> dict[str, Any]:
    existing = _load_existing_site_config(site_name)
    block_names = _iter_dxf_block_names(site_name)
    existing_block_map = _normalize_existing_block_map(existing.get("dxf_blocks", {}))

    grouped_blocks: dict[str, list[str]] = {}
    unknown_blocks: list[str] = []

    for block_name in block_names:
        class_name = _classify_block(block_name, existing_block_map)
        if not class_name:
            unknown_blocks.append(block_name)
            continue
        grouped_blocks.setdefault(class_name, []).append(block_name)

    for class_name in grouped_blocks:
        grouped_blocks[class_name] = sorted(set(grouped_blocks[class_name]))

    config = {
        "site_name": site_name,
        "site_type": existing.get("site_type", "urban_landscape"),
        "environment": existing.get("environment", "outdoor"),
        "camera_type": existing.get("camera_type", "360_degree"),
        "coordinate_system": existing.get("coordinate_system", "local_to_dxf"),
        "dxf_blocks": grouped_blocks,
        "class_colors": {
            **DEFAULT_CLASS_COLORS,
            **(
                existing.get("class_colors", {})
                if isinstance(existing.get("class_colors"), dict)
                else {}
            ),
        },
        "ai_class_aliases": {
            **DEFAULT_AI_CLASS_ALIASES,
            **(
                existing.get("ai_class_aliases", {})
                if isinstance(existing.get("ai_class_aliases"), dict)
                else {}
            ),
        },
    }
    if unknown_blocks:
        config["unmapped_blocks"] = unknown_blocks

    _save_generated_site_config(site_name, config)

    return {
        "site_name": site_name,
        "site_config": config,
        "site_config_text": json.dumps(config, indent=2),
        "unknown_blocks": unknown_blocks,
        "mapped_blocks_count": sum(len(blocks) for blocks in grouped_blocks.values()),
        "total_unique_blocks": len(block_names),
        "used_existing_mapping": bool(existing_block_map),
    }
