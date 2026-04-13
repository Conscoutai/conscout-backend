#creates/updates a project floorplan by saving the image, calibrating map scale/rotation,
# optionally extracting DXF objects, and storing all metadata in DB.

import os
import time
from datetime import datetime, timezone
import math
import json
import shutil
from typing import Literal, Optional

from fastapi import UploadFile, HTTPException
from PIL import Image

from core.database import floorplans_collection
from core.config import DEFAULT_SITE_NAME, site_floorplan_dir
from utils.geo import haversine
from services.project_setup.dxf_service import DXFService


dxf_service = DXFService()


def create_floorplan(
    *,
    file: UploadFile,
    name: str,
    pointA_px: Optional[float],
    pointA_py: Optional[float],
    pointA_lat: Optional[float],
    pointA_lon: Optional[float],
    pointB_px: Optional[float],
    pointB_py: Optional[float],
    pointB_lat: Optional[float],
    pointB_lon: Optional[float],
    calibration_points: Optional[str] = None,
    site_name: Optional[str] = None,
    dxf_project_id: Optional[str] = None,
    baseline_xer_url: Optional[str] = None,
    baseline_xer_name: Optional[str] = None,
    capture_mode: Literal["outdoor", "indoor"] = "outdoor",
):
    try:
        ext = file.filename.split(".")[-1].lower()
        if ext not in ["jpg", "jpeg", "png"]:
            raise HTTPException(400, "Only JPG or PNG supported")

        site_name = site_name or dxf_project_id or DEFAULT_SITE_NAME
        existing_floorplan = floorplans_collection.find_one(
            {"site_name": site_name},
            sort=[("_id", -1)],
        )
        floorplan_id = (
            existing_floorplan.get("id")
            if existing_floorplan and existing_floorplan.get("id")
            else f"floorplan_{int(time.time())}"
        )
        save_as = f"{floorplan_id}.png"

        floorplan_dir = site_floorplan_dir(site_name)
        os.makedirs(floorplan_dir, exist_ok=True)
        image_path = os.path.join(floorplan_dir, save_as)

        if existing_floorplan:
            previous_image = os.path.basename(existing_floorplan.get("imageUrl", ""))
            if previous_image and previous_image != save_as:
                previous_path = os.path.join(floorplan_dir, previous_image)
                if os.path.exists(previous_path):
                    os.remove(previous_path)

        with open(image_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        img = Image.open(image_path)
        width, height = img.size

        safe_capture_mode = capture_mode if capture_mode in {"outdoor", "indoor"} else "outdoor"
        has_calibration = all(
            value is not None
            for value in (
                pointA_px,
                pointA_py,
                pointA_lat,
                pointA_lon,
                pointB_px,
                pointB_py,
                pointB_lat,
                pointB_lon,
            )
        )
        if safe_capture_mode == "outdoor" and not has_calibration:
            raise HTTPException(
                422,
                "Outdoor floorplans require Point A and Point B calibration values",
            )
        if calibration_points and not has_calibration:
            raise HTTPException(
                422,
                "Calibration points require Point A and Point B calibration values",
            )

        scale = None
        rotation_deg = None
        origin = None
        if has_calibration:
            # ---- Compute GPS distance vs pixel distance ----
            gps_dist = haversine(pointA_lat, pointA_lon, pointB_lat, pointB_lon)
            pixel_dist = math.dist([pointA_px, pointA_py], [pointB_px, pointB_py])

            if pixel_dist == 0:
                raise HTTPException(400, "Point A/B pixels must be different")

            scale = gps_dist / pixel_dist  # meters per pixel

            # ---- Compute rotation (pixel Y-axis inverted) ----
            gps_angle = math.atan2(pointB_lat - pointA_lat, pointB_lon - pointA_lon)

            dy_pixel = -(pointB_py - pointA_py)
            dx_pixel = (pointB_px - pointA_px)

            pixel_angle = math.atan2(dy_pixel, dx_pixel)
            rotation = gps_angle - pixel_angle
            rotation_deg = math.degrees(rotation)
            origin = {
                "latitude": pointA_lat,
                "longitude": pointA_lon,
                "pixel": {"x": pointA_px, "y": pointA_py},
            }

        calibration_points_data = None
        if calibration_points:
            try:
                raw_points = json.loads(calibration_points)
                if not isinstance(raw_points, list):
                    raise ValueError("calibration_points must be a list")
                calibration_points_data = []
                for point in raw_points:
                    if not isinstance(point, dict):
                        raise ValueError("calibration_points must contain objects")
                    pixel = point.get("pixel") or {}
                    calibration_points_data.append(
                        {
                            "label": point.get("label"),
                            "latitude": float(point.get("latitude")),
                            "longitude": float(point.get("longitude")),
                            "pixel": {
                                "x": float(pixel.get("x")),
                                "y": float(pixel.get("y")),
                            },
                        }
                    )
            except (ValueError, TypeError) as exc:
                raise HTTPException(400, f"Invalid calibration_points: {exc}")

        # ---- Save metadata ----
        effective_name = site_name or name
        now = datetime.now(timezone.utc)
        floorplan_metadata = {
            "id": floorplan_id,
            "name": effective_name,
            "imageUrl": f"/sites/{site_name}/floorplan/{save_as}",
            "bounds": {"width": width, "height": height},
            "site_name": site_name,
            "capture_mode": safe_capture_mode,
            "created_at": existing_floorplan.get("created_at") if existing_floorplan else now,
            "updated_at": now,
        }
        if scale is not None:
            floorplan_metadata["scale"] = scale
        if rotation_deg is not None:
            floorplan_metadata["rotation"] = rotation_deg
        if origin is not None:
            floorplan_metadata["origin"] = origin
        if baseline_xer_url:
            floorplan_metadata["baseline_xer_url"] = baseline_xer_url
        if baseline_xer_name:
            floorplan_metadata["baseline_xer_name"] = baseline_xer_name
        if calibration_points_data:
            floorplan_metadata["calibration_points"] = calibration_points_data

        if dxf_project_id:
            print(f"[DXF] Processing project: {dxf_project_id}")
            floorplan_metadata["dxf_project_id"] = dxf_project_id
            floorplan_metadata["site_objects"] = dxf_service.process_project_dxfs(
                dxf_project_id,
                floorplan_metadata,
            )
            print(
                "[DXF] Extraction complete. Objects:",
                len(floorplan_metadata["site_objects"]),
            )

        if existing_floorplan:
            floorplans_collection.update_one(
                {"_id": existing_floorplan["_id"]},
                {"$set": floorplan_metadata},
            )
            floorplans_collection.delete_many(
                {
                    "$or": [{"site_name": site_name}, {"dxf_project_id": site_name}],
                    "_id": {"$ne": existing_floorplan["_id"]},
                }
            )
            floorplan_metadata["_id"] = str(existing_floorplan["_id"])
        else:
            result = floorplans_collection.insert_one(floorplan_metadata)
            floorplan_metadata["_id"] = str(result.inserted_id)

        return {"message": "Floorplan saved", "floorPlan": floorplan_metadata}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Upload failed: {e}")

