# AI classification helper: map detections to work types.
# Used by segmentation and reporting logic.

import numpy as np


def as_name_dict(names):
    if isinstance(names, dict):
        return names
    return {i: n for i, n in enumerate(names)}


def work_type_map():
    return {
        # "Concrete Pore": ["asphalt_parking"],
        "painting": ["pedestrian_path", "cycle_path"],
        "paving_installation": ["concrete_paving_slabs"],
        "planting": ["shrubs", "palm", "tree", "tree_gates"],

        # "Concreting": ["paint_patterns", "pedestrian_path"],
        # "structural": ["street_light_poles", "seating_benches"],
    }


def _boxes_to_mask(shape, boxes):
    mask = np.zeros(shape, dtype=np.uint8)
    h, w = shape
    for box in boxes:
        x1, y1, x2, y2 = [int(v) for v in box]
        x1 = max(0, min(w - 1, x1))
        x2 = max(0, min(w - 1, x2))
        y1 = max(0, min(h - 1, y1))
        y2 = max(0, min(h - 1, y2))
        if x2 <= x1 or y2 <= y1:
            continue
        mask[y1:y2, x1:x2] = 1
    return mask


def choose_work_type(class_instances, seg_model, worker_count, detections):
    if worker_count <= 0 or not class_instances:
        return None, None

    name_by_id = as_name_dict(seg_model.names)
    id_by_name = {v: k for k, v in name_by_id.items()}

    worker_boxes = []
    for det in detections or []:
        if det.get("class") != "worker":
            continue
        bbox = det.get("bbox") or {}
        worker_boxes.append(
            [bbox.get("x1", 0), bbox.get("y1", 0), bbox.get("x2", 0), bbox.get("y2", 0)]
        )

    any_mask = None
    for masks in class_instances.values():
        if masks:
            any_mask = masks[0]
            break
    if any_mask is None:
        return None, None

    worker_mask = _boxes_to_mask(any_mask.shape[:2], worker_boxes)
    work_type_label = None
    work_type_mask = None
    best_overlap = 0

    for work_type, class_names in work_type_map().items():
        best_type_overlap = 0
        best_type_mask = None
        for class_name in class_names:
            cls_id = id_by_name.get(class_name)
            if cls_id is None:
                continue
            for mask in class_instances.get(int(cls_id), []):
                overlap = int((mask & worker_mask).sum())
                if overlap > best_type_overlap:
                    best_type_overlap = overlap
                    best_type_mask = mask
                elif (
                    overlap == best_type_overlap
                    and overlap > 0
                    and best_type_mask is not None
                    and int(mask.sum()) > int(best_type_mask.sum())
                ):
                    best_type_mask = mask
        if best_type_mask is None:
            continue
        if best_type_overlap > best_overlap:
            best_overlap = best_type_overlap
            work_type_label = work_type
            work_type_mask = best_type_mask

    return work_type_label, work_type_mask
