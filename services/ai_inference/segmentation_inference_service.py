# AI segmentation pipeline: run segmentation models.
# Returns class presence and overlays.

import os
import cv2

from services.ai_inference.inference_visualization_service import (
    as_name_dict,
    build_segmentation_overlay,
    draw_roi_contours,
    draw_class_contours,
    draw_count_boxes_from_detections,
    draw_worker_boxes_from_detections,
    count_workers_from_detections,
    apply_work_type_overlay,
    draw_worker_count_label,
)
from utils.ai_classes import normalize_ai_class
from utils.visualization import get_color
from services.progress.work_schedule.work_classification_service import choose_work_type
from core.config import (
    AI_DEVICE,
    SEG_CONF,
    SEG_IMGSZ,
    SEG_IOU,
    tour_raw_dir,
    tour_detect_seg_dir,
)
from core.database import floorplans_collection
from services.tour_management.site_capture.shared.storage_service import resolve_storage_key_for_tour


# ---------------------------------------------------------
# MAIN SERVICE FUNCTION
# ---------------------------------------------------------
async def run_seg_ai_for_tour(
    *,
    tour_id: str,
    tours_collection,
    seg_model,
    seg_lock
):
    """
    Runs SEGMENTATION AI for presence detection + work type labeling.
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
        f"[seg] tour={tour_id} floorplan_id={floorplan_id} site_name={site_name}"
    )
    if floorplan_id and not floorplan:
        print(f"[seg] floorplan not found for id={floorplan_id}")
    storage_key = resolve_storage_key_for_tour(tour_id, tour)

    owner_email = tour.get("owner_email")
    owner_user_id = tour.get("owner_user_id")

    raw_dir = tour_raw_dir(
        tour_id,
        owner_email=owner_email,
        owner_user_id=owner_user_id,
        site_name=site_name,
    )
    seg_dir = tour_detect_seg_dir(
        tour_id,
        owner_email=owner_email,
        owner_user_id=owner_user_id,
        site_name=site_name,
    )
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(seg_dir, exist_ok=True)
    # print(
    #     "[SEG-INF-CONFIG] "
    #     f"tour={tour_id} "
    #     f"imgsz={SEG_IMGSZ} conf={SEG_CONF} iou={SEG_IOU} "
    #     f"device={AI_DEVICE or 'default'}"
    # )

    for node in tour["nodes"]:

        img_path = os.path.join(raw_dir, node["filename"])
        orig = cv2.imread(img_path)
        if orig is None:
            continue

        # ---------------------------------------
        # YOLO Segmentation inference (thread-safe)
        # ---------------------------------------
        predict_kwargs = {
            "imgsz": SEG_IMGSZ,
            "conf": SEG_CONF,
            "iou": SEG_IOU,
            "verbose": False,
        }
        if AI_DEVICE:
            predict_kwargs["device"] = AI_DEVICE
        print(
            "[SEG-INF] "
            f"tour={tour_id} "
            f"file={node['filename']} "
            f"shape={orig.shape} "
            f"kwargs={predict_kwargs}"
        )
        async with seg_lock:
            seg_results = seg_model.predict(orig, **predict_kwargs)

        seg_res = seg_results[0]
        detections = node.get("detections", []) or []

        # ---------------------------------------
        # Presence logic (all classes)
        # ---------------------------------------
        seg_names = as_name_dict(seg_model.names)
        if seg_res.boxes is not None and len(seg_res.boxes) > 0:
            present_set = {
                seg_names[int(cls_id)]
                for cls_id in seg_res.boxes.cls.cpu().numpy().astype(int)
            }
        else:
            present_set = set()

        seg_presence = {name: name in present_set for name in seg_names.values()}

        # ---------------------------------------
        # Visualization + work type logic
        # ---------------------------------------
        overlay, roi_mask, class_masks, class_instances = build_segmentation_overlay(
            orig, seg_res, seg_model, site_name=site_name
        )
        blended = cv2.addWeighted(orig, 0.45, overlay, 0.55, 0)

        worker_count = count_workers_from_detections(detections)
        blended = draw_count_boxes_from_detections(
            blended, detections, site_name=site_name
        )
        blended = draw_worker_boxes_from_detections(blended, detections)

        work_type_label, work_type_mask = choose_work_type(
            class_instances, seg_model, worker_count, detections
        )
        if work_type_label:
            contour_color = get_color(work_type_label, site_name=site_name)
            blended = draw_roi_contours(blended, roi_mask, contour_color)
        else:
            class_name_by_id = {
                int(cls_id): normalize_ai_class(name, site_name=site_name)
                for cls_id, name in as_name_dict(seg_model.names).items()
            }
            blended = draw_class_contours(
                blended, class_masks, class_name_by_id, site_name=site_name
            )
        blended = apply_work_type_overlay(blended, work_type_mask, work_type_label)
        blended = draw_worker_count_label(blended, worker_count)

        out_name = node["filename"].replace(".jpg", "_seg.jpg")
        out_path = os.path.join(seg_dir, out_name)
        cv2.imwrite(out_path, blended)

        # ---------------------------------------
        # Save results to MongoDB
        # ---------------------------------------
        tours_collection.update_one(
            {"tour_id": tour_id, "nodes.id": node["id"]},
            {"$set": {
                "nodes.$.seg_presence": seg_presence,
                "nodes.$.seg_present_classes": sorted(present_set),
                "nodes.$.worker_count": worker_count,
                "nodes.$.work_type": work_type_label,
                "nodes.$.segmentedImageUrl": f"/streetview/{storage_key}/detect+seg/{out_name}",
            }}
        )

    return {"message": "Segmentation + work type completed (service-based)"}
