# Tour node ingest service: saves uploads and computes node positions.
# Handles EXIF extraction, storage, and initial node persistence.

import os
import shutil
import time
import uuid
import math
from typing import Optional

from exif import Image as ExifImage
from fastapi import HTTPException, UploadFile

from core.config import TOURS_DIR
from core.database import floorplans_collection, tours_collection
from utils.geo import gps_to_xy, haversine, project_point
from services.tour_management.site_capture.shared.storage_service import (
    build_storage_key,
    build_streetview_url,
    resolve_storage_key_for_tour,
)


def _extract_exif_gps_yaw(image_path: str):
    lat, lon, yaw = None, None, None
    try:
        with open(image_path, "rb") as f:
            exif = ExifImage(f)

        if exif.has_exif and hasattr(exif, "gps_latitude"):
            lat = (
                exif.gps_latitude[0]
                + exif.gps_latitude[1] / 60
                + exif.gps_latitude[2] / 3600
            )
            if exif.gps_latitude_ref == "S":
                lat = -lat

            lon = (
                exif.gps_longitude[0]
                + exif.gps_longitude[1] / 60
                + exif.gps_longitude[2] / 3600
            )
            if exif.gps_longitude_ref == "W":
                lon = -lon

        if hasattr(exif, "gps_img_direction"):
            yaw = float(exif.gps_img_direction)
    except Exception:
        pass

    return lat, lon, yaw


def _is_valid_number(value) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(value)


def _is_valid_lat_lon(lat, lon) -> bool:
    if not (_is_valid_number(lat) and _is_valid_number(lon)):
        return False
    if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
        return False
    # Legacy docs may have used 0/0 as "missing GPS"; treat as invalid.
    if abs(float(lat)) < 1e-9 and abs(float(lon)) < 1e-9:
        return False
    return True


def _is_valid_yaw(value) -> bool:
    return _is_valid_number(value)


def _estimate_step_distance_m(nodes: list[dict]) -> float:
    distances = []
    for i in range(1, len(nodes)):
        prev = nodes[i - 1]
        curr = nodes[i]
        if _is_valid_lat_lon(prev.get("lat"), prev.get("lon")) and _is_valid_lat_lon(curr.get("lat"), curr.get("lon")):
            d = haversine(prev["lat"], prev["lon"], curr["lat"], curr["lon"])
            # Keep realistic walking intervals and ignore outliers.
            if 0.25 <= d <= 25:
                distances.append(d)
    if distances:
        return sum(distances) / len(distances)
    return 2.0


def _nearest_prev_with_geo(nodes: list[dict], idx: int):
    for i in range(idx - 1, -1, -1):
        if _is_valid_lat_lon(nodes[i].get("lat"), nodes[i].get("lon")):
            return i
    return None


def _nearest_next_with_geo(nodes: list[dict], idx: int):
    for i in range(idx + 1, len(nodes)):
        if _is_valid_lat_lon(nodes[i].get("lat"), nodes[i].get("lon")):
            return i
    return None


def _recompute_node_locations(nodes: list[dict], floorplan: dict) -> list[dict]:
    if not nodes:
        return nodes

    normalized = [n.copy() for n in nodes]

    # Sanitize legacy/missing coordinates first.
    for node in normalized:
        lat = node.get("lat")
        lon = node.get("lon")
        if _is_valid_lat_lon(lat, lon):
            node["lat"] = float(lat)
            node["lon"] = float(lon)
            if node.get("has_exif_gps"):
                node["location_source"] = "exif"
                node["location_confidence"] = 1.0
        else:
            node["lat"] = None
            node["lon"] = None
            if not node.get("location_source"):
                node["location_source"] = "unlocated"
            if node.get("location_confidence") is None:
                node["location_confidence"] = 0.0

    step_distance_m = _estimate_step_distance_m(normalized)

    for i, node in enumerate(normalized):
        if _is_valid_lat_lon(node.get("lat"), node.get("lon")):
            continue

        prev_idx = _nearest_prev_with_geo(normalized, i)
        next_idx = _nearest_next_with_geo(normalized, i)

        if prev_idx is not None and next_idx is not None and next_idx > prev_idx:
            prev_node = normalized[prev_idx]
            next_node = normalized[next_idx]
            ratio = (i - prev_idx) / (next_idx - prev_idx)
            node["lat"] = prev_node["lat"] + ratio * (next_node["lat"] - prev_node["lat"])
            node["lon"] = prev_node["lon"] + ratio * (next_node["lon"] - prev_node["lon"])
            node["location_source"] = "interpolated"
            node["location_confidence"] = 0.7
            continue

        if prev_idx is not None:
            prev_node = normalized[prev_idx]
            yaw = node.get("camera_yaw")
            if not _is_valid_yaw(yaw):
                yaw = prev_node.get("camera_yaw")
            if _is_valid_yaw(yaw):
                lat, lon = project_point(prev_node["lat"], prev_node["lon"], float(yaw), step_distance_m)
                node["lat"] = lat
                node["lon"] = lon
                node["location_source"] = "projected"
                node["location_confidence"] = 0.5
                continue

        if next_idx is not None:
            next_node = normalized[next_idx]
            yaw = next_node.get("camera_yaw")
            if _is_valid_yaw(yaw):
                # Back-project from the next known point.
                lat, lon = project_point(next_node["lat"], next_node["lon"], (float(yaw) + 180.0) % 360, step_distance_m)
                node["lat"] = lat
                node["lon"] = lon
                node["location_source"] = "projected"
                node["location_confidence"] = 0.45
                continue

        node["location_source"] = "unlocated"
        node["location_confidence"] = 0.0
        node["x"] = None
        node["y"] = None

    for node in normalized:
        node["has_location"] = _is_valid_lat_lon(node.get("lat"), node.get("lon"))
        if node["has_location"]:
            x, y = gps_to_xy(node["lat"], node["lon"], floorplan)
            node["x"] = max(0, min(x, floorplan["bounds"]["width"]))
            node["y"] = max(0, min(y, floorplan["bounds"]["height"]))
        else:
            node["x"] = None
            node["y"] = None

    return normalized


def _normalize_and_persist_tour_nodes(tour_id: str, floorplan: dict) -> list[dict]:
    tour = tours_collection.find_one({"tour_id": tour_id})
    if not tour:
        return []
    nodes = tour.get("nodes", [])
    normalized_nodes = _recompute_node_locations(nodes, floorplan)
    tours_collection.update_one(
        {"tour_id": tour_id},
        {"$set": {"nodes": normalized_nodes}},
    )
    return normalized_nodes


def upload_streetview_image(
    *,
    tour_id: str,
    image: UploadFile,
    tour_name: str,
    floorplan_id: Optional[str],
):
    filename = image.filename or ""
    filename_lower = filename.lower()
    allowed_ext = (".jpg", ".jpeg", ".png")
    allowed_types = {"image/jpeg", "image/png"}
    if not (filename_lower.endswith(allowed_ext) or image.content_type in allowed_types):
        raise HTTPException(400, "File must be a JPG, JPEG, or PNG")

    node_id = f"pano_{uuid.uuid4().hex}"
    existing = tours_collection.find_one({"tour_id": tour_id})
    storage_key = resolve_storage_key_for_tour(tour_id, existing)
    if existing is None:
        storage_key = build_storage_key(tour_id, tour_name or tour_id)

    tour_folder = os.path.join(TOURS_DIR, storage_key, "raw")
    os.makedirs(tour_folder, exist_ok=True)

    filename = f"{node_id}.jpg"
    save_path = os.path.join(tour_folder, filename)

    with open(save_path, "wb") as buffer:
        shutil.copyfileobj(image.file, buffer)

    lat, lon, yaw = _extract_exif_gps_yaw(save_path)

    # Require an explicit, valid floorplan_id - do not silently fall back.
    if not floorplan_id:
        raise HTTPException(400, "floorplan_id is required for streetview upload")

    floorplan = floorplans_collection.find_one({"id": floorplan_id})
    if not floorplan:
        raise HTTPException(400, "Unknown floorplan_id")
    x = y = None
    if _is_valid_lat_lon(lat, lon) and floorplan:
        x, y = gps_to_xy(lat, lon, floorplan)
        x = max(0, min(x, floorplan["bounds"]["width"]))
        y = max(0, min(y, floorplan["bounds"]["height"]))

    node = {
        "id": node_id,
        "tour_id": tour_id,
        "filename": filename,
        "imageUrl": build_streetview_url(storage_key, "raw", filename),
        "lat": lat,
        "lon": lon,
        "x": x,
        "y": y,
        "has_exif_gps": _is_valid_lat_lon(lat, lon),
        "has_location": _is_valid_lat_lon(lat, lon),
        "location_source": "exif" if _is_valid_lat_lon(lat, lon) else "unlocated",
        "location_confidence": 1.0 if _is_valid_lat_lon(lat, lon) else 0.0,
        "floorplan_id": floorplan["id"] if floorplan else None,
        "object_counts": {},
        "camera_yaw": yaw,
        "comments": [],
    }

    if existing:
        update_set = {"storage_key": storage_key}
        if not existing.get("name") and tour_name:
            update_set["name"] = tour_name
        tours_collection.update_one(
            {"tour_id": tour_id},
            {"$set": update_set, "$push": {"nodes": node}}
        )
    else:
        tours_collection.insert_one({
            "tour_id": tour_id,
            "floorplan_id": floorplan["id"] if floorplan else None,
            "name": tour_name,
            "storage_key": storage_key,
            "created_at": int(time.time() * 1000),
            "nodes": [node],
        })

    normalized_nodes = _normalize_and_persist_tour_nodes(tour_id, floorplan)
    normalized_node = next((n for n in normalized_nodes if n.get("id") == node_id), node)

    return {"message": "Uploaded successfully", "node": normalized_node}

