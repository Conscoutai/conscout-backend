import os
import time
from typing import Iterable, Optional

import cv2
from fastapi import HTTPException, UploadFile

from core.database import floorplans_collection, tours_collection
from services.tour_management.site_capture.shared.storage_service import (
    build_storage_key,
    build_streetview_url,
    resolve_storage_dir_for_tour,
    resolve_storage_key_for_tour,
)

MAX_STITCH_FRAMES = 30
MAX_STITCH_IMAGE_EDGE = 1600
VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp")


def _frames_dir(tour_id: str, tour_doc=None, node_index: Optional[int] = None) -> str:
    base = os.path.join(resolve_storage_dir_for_tour(tour_id, tour_doc), "frames")
    if isinstance(node_index, int) and node_index >= 0:
        return os.path.join(base, f"node_{node_index}")
    return base


def _videos_dir(tour_id: str, tour_doc=None, node_index: Optional[int] = None) -> str:
    base = os.path.join(resolve_storage_dir_for_tour(tour_id, tour_doc), "videos")
    if isinstance(node_index, int) and node_index >= 0:
        return os.path.join(base, f"node_{node_index}")
    return base


def _sorted_frame_paths(raw_dir: str) -> list[str]:
    files = [
        os.path.join(raw_dir, name)
        for name in os.listdir(raw_dir)
        if name.lower().endswith((".jpg", ".jpeg", ".png"))
    ]
    return sorted(files)


def _node_dirs_with_frames(base_frames_dir: str) -> list[tuple[int, str]]:
    if not os.path.isdir(base_frames_dir):
        return []

    node_dirs: list[tuple[int, str]] = []
    for name in os.listdir(base_frames_dir):
        candidate = os.path.join(base_frames_dir, name)
        if not os.path.isdir(candidate) or not name.startswith("node_"):
            continue
        try:
            idx = int(name.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        if _sorted_frame_paths(candidate):
            node_dirs.append((idx, candidate))
    return sorted(node_dirs, key=lambda pair: pair[0])


def _read_images(paths: Iterable[str]) -> list:
    images = []
    for path in paths:
        image = cv2.imread(path)
        if image is not None:
            images.append(image)
    return images


def _downscale_for_stitch(image):
    h, w = image.shape[:2]
    max_edge = max(h, w)
    if max_edge <= MAX_STITCH_IMAGE_EDGE:
        return image
    scale = MAX_STITCH_IMAGE_EDGE / float(max_edge)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)


def _prepare_images_for_stitch(frame_paths: list[str]) -> list:
    if len(frame_paths) <= MAX_STITCH_FRAMES:
        selected = frame_paths
    else:
        # Uniformly sample frames to avoid OpenCV/OOM instability on long videos.
        step = len(frame_paths) / float(MAX_STITCH_FRAMES)
        selected = [frame_paths[int(i * step)] for i in range(MAX_STITCH_FRAMES)]
    images = _read_images(selected)
    return [_downscale_for_stitch(img) for img in images]


def _stitch_images(images: list):
    # OpenCL can fail on some Windows GPU/driver setups for large stitching workloads.
    try:
        cv2.ocl.setUseOpenCL(False)
    except Exception:
        pass

    status = None
    stitched = None
    used_fallback = False
    stitch_error = None
    try:
        stitcher = cv2.Stitcher_create(cv2.Stitcher_PANORAMA)
        status, stitched = stitcher.stitch(images)
    except cv2.error as exc:
        stitch_error = str(exc)

    if status != cv2.Stitcher_OK or stitched is None:
        stitched = images[0]
        used_fallback = True

    return stitched, status, used_fallback, stitch_error


def _split_frame_paths_into_segments(frame_paths: list[str], segment_count: int) -> list[list[str]]:
    total = len(frame_paths)
    if total == 0:
        return []

    count = max(1, min(int(segment_count), total))
    segments: list[list[str]] = []
    for idx in range(count):
        start = int(round((idx * total) / float(count)))
        end = int(round(((idx + 1) * total) / float(count)))
        chunk = frame_paths[start:end]
        if not chunk:
            chunk = [frame_paths[min(start, total - 1)]]
        segments.append(chunk)
    return segments


def _extract_frames_from_video(
    video_path: str,
    output_dir: str,
    frame_step_seconds: float = 0.5,
) -> tuple[int, float, float]:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return 0, 0.0, 0.0

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if fps <= 0:
        fps = 30.0
    frame_step = max(1, int(round(fps * frame_step_seconds)))
    duration_seconds = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps) if fps > 0 else 0.0

    extracted = 0
    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        if frame_idx % frame_step == 0:
            filename = f"frame-{extracted}.jpg"
            cv2.imwrite(os.path.join(output_dir, filename), frame)
            extracted += 1
        frame_idx += 1

    cap.release()
    return extracted, fps, duration_seconds


def upload_indoor_video(tour_id: str, file: UploadFile, node_index: int) -> dict:
    filename = (file.filename or "").strip()
    if not filename.lower().endswith(VIDEO_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Video must be MP4/MOV/M4V/AVI/MKV/WEBM/3GP")

    tour_doc = tours_collection.find_one({"tour_id": tour_id})
    videos_dir = _videos_dir(tour_id, tour_doc, node_index=node_index)
    frames_dir = _frames_dir(tour_id, tour_doc, node_index=node_index)
    os.makedirs(videos_dir, exist_ok=True)
    os.makedirs(frames_dir, exist_ok=True)

    for existing in os.listdir(frames_dir):
        existing_path = os.path.join(frames_dir, existing)
        if os.path.isfile(existing_path) and existing.lower().endswith((".jpg", ".jpeg", ".png")):
            os.remove(existing_path)

    save_path = os.path.join(videos_dir, filename)
    with open(save_path, "wb") as out:
        out.write(file.file.read())

    frame_count, fps, duration_seconds = _extract_frames_from_video(save_path, frames_dir, frame_step_seconds=0.5)
    if frame_count <= 0:
        raise HTTPException(
            status_code=400,
            detail="Could not decode frames from this video on server. Please re-record or use a different format.",
        )

    return {
        "nodeIndex": int(node_index),
        "frameCount": int(frame_count),
        "fps": float(fps),
        "durationSeconds": float(duration_seconds),
    }


def stitch_indoor_panoramas(tour_id: str, node_count: int) -> dict:
    if not isinstance(node_count, int) or node_count < 1:
        raise HTTPException(status_code=400, detail="node_count must be a positive integer")

    tour_doc = tours_collection.find_one({"tour_id": tour_id})
    frames_dir = _frames_dir(tour_id, tour_doc)
    if not os.path.exists(frames_dir):
        raise HTTPException(status_code=404, detail="No frames directory found for this tour")

    storage_key = resolve_storage_key_for_tour(tour_id, tour_doc)
    raw_dir = os.path.join(
        resolve_storage_dir_for_tour(
            tour_id,
            {
                "storage_key": storage_key,
                "owner_email": (tour_doc or {}).get("owner_email"),
                "owner_user_id": (tour_doc or {}).get("owner_user_id"),
            },
        ),
        "raw",
    )
    os.makedirs(raw_dir, exist_ok=True)

    node_dirs = _node_dirs_with_frames(frames_dir)
    if node_dirs:
        segments = [paths for _, paths in [(idx, _sorted_frame_paths(path)) for idx, path in node_dirs]]
        segments = [segment for segment in segments if segment]
    else:
        frame_paths = _sorted_frame_paths(frames_dir)
        if not frame_paths:
            raise HTTPException(status_code=400, detail="No frames found for stitching")
        segments = _split_frame_paths_into_segments(frame_paths, node_count)

    panoramas = []
    for idx, segment_paths in enumerate(segments):
        images = _prepare_images_for_stitch(segment_paths)
        if not images:
            continue

        stitched, status, used_fallback, stitch_error = _stitch_images(images)
        filename = "panorama.jpg" if len(segments) == 1 else f"pano_{idx}.jpg"
        panorama_path = os.path.join(raw_dir, filename)

        ok = cv2.imwrite(panorama_path, stitched)
        if not ok:
            raise HTTPException(status_code=500, detail=f"Failed to write panorama image: {filename}")

        panoramas.append(
            {
                "index": idx,
                "filename": filename,
                "panoramaUrl": build_streetview_url(storage_key, "raw", filename),
                "frameCount": len(images),
                "inputFrameCount": len(segment_paths),
                "usedFallback": used_fallback,
                "stitchStatus": int(status) if isinstance(status, int) else None,
                "stitchError": stitch_error,
            }
        )

    if not panoramas:
        raise HTTPException(status_code=400, detail="Frames exist but could not be decoded")

    input_frame_count = 0
    for segment in segments:
        input_frame_count += len(segment)

    return {
        "panoramas": panoramas,
        "nodeCountRequested": node_count,
        "nodeCountProduced": len(panoramas),
        "inputFrameCount": input_frame_count,
    }


def save_indoor_tour_metadata(tour_id: str, payload: dict) -> dict:
    floorplan_id = payload.get("floorplan_id")
    if not floorplan_id:
        raise HTTPException(status_code=400, detail="floorplan_id is required")

    floorplan = floorplans_collection.find_one({"id": floorplan_id})
    if not floorplan:
        raise HTTPException(status_code=400, detail="Unknown floorplan_id")
    bounds = floorplan.get("bounds", {}) or {}
    bound_w = bounds.get("width")
    bound_h = bounds.get("height")

    # Indoor tours should persist panorama nodes (not frame nodes).
    # Keep compatibility with old payload key "nodes".
    panorama_nodes = payload.get("panorama_nodes")
    if panorama_nodes is None:
        panorama_nodes = payload.get("nodes")
    if not isinstance(panorama_nodes, list) or len(panorama_nodes) == 0:
        raise HTTPException(status_code=400, detail="panorama_nodes (or nodes) must be a non-empty list")

    existing = tours_collection.find_one({"tour_id": tour_id})
    old_storage_key = resolve_storage_key_for_tour(tour_id, existing)
    desired_storage_key = build_storage_key(tour_id, payload.get("tour_name") or (existing or {}).get("name"))

    old_dir = resolve_storage_dir_for_tour(
        tour_id,
        {
            "storage_key": old_storage_key,
            "owner_email": (existing or {}).get("owner_email"),
            "owner_user_id": (existing or {}).get("owner_user_id"),
        },
    )
    new_dir = resolve_storage_dir_for_tour(
        tour_id,
        {
            "storage_key": desired_storage_key,
            "owner_email": (existing or {}).get("owner_email"),
            "owner_user_id": (existing or {}).get("owner_user_id"),
        },
    )
    if old_storage_key != desired_storage_key and os.path.isdir(old_dir) and not os.path.exists(new_dir):
        os.rename(old_dir, new_dir)

    nodes = []
    for idx, node in enumerate(panorama_nodes):
        x = node.get("x")
        y = node.get("y")
        lat = node.get("lat")
        lon = node.get("lon")
        direction = node.get("direction")
        filename = os.path.basename((node.get("filename") or "panorama.jpg").strip()) or "panorama.jpg"

        if isinstance(x, (int, float)) and isinstance(bound_w, (int, float)):
            x = max(0.0, min(float(x), float(bound_w)))
        elif not isinstance(x, (int, float)) and isinstance(bound_w, (int, float)):
            x = float(bound_w) / 2.0

        if isinstance(y, (int, float)) and isinstance(bound_h, (int, float)):
            y = max(0.0, min(float(y), float(bound_h)))
        elif not isinstance(y, (int, float)) and isinstance(bound_h, (int, float)):
            y = float(bound_h) / 2.0

        nodes.append(
            {
                "id": node.get("id") or f"pano_{tour_id}_{idx}",
                "tour_id": tour_id,
                # Construction view should open the node's stitched panorama for indoor tours.
                "filename": filename,
                "imageUrl": build_streetview_url(
                    desired_storage_key,
                    "raw",
                    filename,
                ),
                "lat": lat if isinstance(lat, (int, float)) else None,
                "lon": lon if isinstance(lon, (int, float)) else None,
                "x": x if isinstance(x, (int, float)) else None,
                "y": y if isinstance(y, (int, float)) else None,
                "has_location": isinstance(x, (int, float)) and isinstance(y, (int, float)),
                "location_source": "indoor_video",
                "location_confidence": 1.0 if isinstance(x, (int, float)) and isinstance(y, (int, float)) else 0.0,
                "object_counts": {},
                "camera_yaw": direction if isinstance(direction, (int, float)) else None,
                "comments": [],
            }
        )

    created_at = payload.get("created_at")
    if not isinstance(created_at, (int, float)):
        created_at = int(time.time() * 1000)

    tours_collection.update_one(
        {"tour_id": tour_id},
        {
            "$set": {
                "tour_id": tour_id,
                "name": payload.get("tour_name") or "Indoor Video Tour",
                "storage_key": desired_storage_key,
                "floorplan_id": floorplan_id,
                "created_at": int(created_at),
                "nodes": nodes,
                "panorama_url": build_streetview_url(desired_storage_key, "raw", "panorama.jpg"),
                "capture_mode": "indoor_video",
                "metadata": payload.get("metadata", {}),
            }
        },
        upsert=True,
    )

    return {
        "message": "Indoor tour saved",
        "tour_id": tour_id,
        "node_count": len(nodes),
        "node_mode": "panorama",
    }

