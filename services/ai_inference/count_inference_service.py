# AI count pipeline: run object detection and tally results.
# Returns counts and annotated outputs.

import os
import cv2

from utils.ai_classes import normalize_ai_class
from utils.world_coords import detection_to_world_xy
from services.ai_inference.inference_visualization_service import draw_count_boxes_from_detections
from core.config import tour_raw_dir, tour_detect_dir
from core.database import floorplans_collection
from services.tour_management.site_capture.shared.storage_service import resolve_storage_key_for_tour


# ---------------------------------------------------------
# MAIN SERVICE FUNCTION
# ---------------------------------------------------------
async def run_count_ai_for_tour(
    *, tour_id: str, tours_collection, count_model, count_lock
):
    """
    Runs COUNT AI on all nodes of a tour.
    Pure AI + DB logic (no visualization logic here)
    """

    tour = tours_collection.find_one({"tour_id": tour_id})
    if not tour:
        return {"error": "Tour not found"}

    if "nodes" not in tour or len(tour["nodes"]) == 0:
        return {"message": "No nodes to process"}

    site_name = None
    floorplan_id = tour.get("floorplan_id")
    if floorplan_id:
        floorplan = floorplans_collection.find_one({"id": floorplan_id})
        if floorplan:
            site_name = (
                floorplan.get("site_name")
                or floorplan.get("dxf_project_id")
                or floorplan.get("display_name")
            )
    if not site_name:
        site_name = tour.get("site_name") or tour.get("site") or tour.get("project_id")
    print(
        f"[count] tour={tour_id} floorplan_id={floorplan_id} site_name={site_name}"
    )
    if floorplan_id and not floorplan:
        print(f"[count] floorplan not found for id={floorplan_id}")
    storage_key = resolve_storage_key_for_tour(tour_id, tour)

    owner_email = tour.get("owner_email")
    owner_user_id = tour.get("owner_user_id")

    raw_dir = tour_raw_dir(
        tour_id,
        owner_email=owner_email,
        owner_user_id=owner_user_id,
        site_name=site_name,
    )
    detect_dir = tour_detect_dir(
        tour_id,
        owner_email=owner_email,
        owner_user_id=owner_user_id,
        site_name=site_name,
    )
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(detect_dir, exist_ok=True)

    for node in tour["nodes"]:

        img_path = os.path.join(raw_dir, node["filename"])
        orig = cv2.imread(img_path)

        if orig is None:
            print(f"Image read failed: {node['filename']}")
            continue

        # ---------------------------------------
        # Resize for inference
        # ---------------------------------------
        TARGET_WIDTH = 2048
        h, w = orig.shape[:2]
        scale = TARGET_WIDTH / w
        img = cv2.resize(orig, (TARGET_WIDTH, int(h * scale)))

        # ---------------------------------------
        # YOLO inference (thread-safe)
        # ---------------------------------------
        async with count_lock:
            results = count_model.predict(img, imgsz=1280, conf=0.25, verbose=False)

        det = results[0].boxes

        # ---------------------------------------
        # Scale boxes back to original image
        # ---------------------------------------
        h_o, w_o = orig.shape[:2]
        h_i, w_i = img.shape[:2]
        sx, sy = w_o / w_i, h_o / h_i

        object_counts = {}
        detections = []

        for b in det:
            raw_name = count_model.names[int(b.cls[0])]
            cls_name = normalize_ai_class(raw_name, site_name)
            object_counts[cls_name] = object_counts.get(cls_name, 0) + 1

            x1, y1, x2, y2 = b.xyxy[0].tolist()
            x1 *= sx
            x2 *= sx
            y1 *= sy
            y2 *= sy

            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0

            yaw_offset = (cx / w_o) * 360.0 - 180.0
            pitch_offset = (0.5 - (cy / h_o)) * 180.0
            pitch_offset = max(-85.0, min(85.0, pitch_offset))

            det_obj = {
                "class": cls_name,
                "yaw": yaw_offset,
                "pitch": pitch_offset,
                "confidence": float(b.conf[0]),
                "bbox": {
                    "x1": float(x1),
                    "y1": float(y1),
                    "x2": float(x2),
                    "y2": float(y2),
                },
            }

            # ---------------------------------------
            # ADD WORLD COORDS (ONLY FOR PALMS)
            # ---------------------------------------
            if cls_name == "palm":
                coords = detection_to_world_xy(node, det_obj)
                if coords:
                    det_obj["world_x"] = coords["world_x"]
                    det_obj["world_y"] = coords["world_y"]

            detections.append(det_obj)
        # ---------------------------------------
        # Save results to MongoDB
        # ---------------------------------------
        detected_name = node["filename"].replace(".jpg", "_det.jpg")
        detected_path = os.path.join(detect_dir, detected_name)
        detected_img = draw_count_boxes_from_detections(
            orig.copy(), detections, site_name=site_name
        )
        cv2.imwrite(detected_path, detected_img)

        tours_collection.update_one(
            {"tour_id": tour_id, "nodes.id": node["id"]},
            {
                "$set": {
                    "nodes.$.object_counts": object_counts,
                    "nodes.$.detections": detections,
                    "nodes.$.image_width": w_o,
                    "nodes.$.image_height": h_o,
                    "nodes.$.detectedImageUrl": f"/streetview/{storage_key}/detect/{detected_name}",
                }
            },
        )

    return {"message": "Countable AI completed (service-based)"}
