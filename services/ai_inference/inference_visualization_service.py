# AI visualization helpers: draw overlays and annotations.
# Shared by count and segmentation pipelines.

import cv2
import numpy as np

from utils.ai_classes import normalize_ai_class
from utils.visualization import get_color


def as_name_dict(names):
    if isinstance(names, dict):
        return names
    return {i: n for i, n in enumerate(names)}


def _draw_mask_label(image, text, mask):
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return
    c = max(contours, key=cv2.contourArea)
    m = cv2.moments(c)
    if m["m00"] == 0:
        return
    cx = int(m["m10"] / m["m00"])
    cy = int(m["m01"] / m["m00"])
    cv2.putText(
        image,
        text,
        (cx, cy),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        image,
        text,
        (cx, cy),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )


def build_segmentation_overlay(
    image, seg_res, seg_model, roi_classes=None, site_name=None
):
    overlay = image.copy()
    roi_mask = np.zeros(image.shape[:2], dtype=np.uint8)
    class_masks = {}
    class_instances = {}

    if seg_res.masks is None or seg_res.masks.data is None:
        return overlay, roi_mask, class_masks, class_instances

    names_dict = as_name_dict(seg_model.names)
    if roi_classes is None:
        roi_classes = sorted(names_dict.keys())

    masks = seg_res.masks.data.cpu().numpy()
    classes = seg_res.boxes.cls.cpu().numpy().astype(int)
    confs = seg_res.boxes.conf.cpu().numpy() if seg_res.boxes is not None else None
    target_h, target_w = image.shape[:2]

    for i, (mask, cls_id) in enumerate(zip(masks, classes)):
        mask_bin = (mask > 0.5).astype(np.uint8)
        if mask_bin.shape != (target_h, target_w):
            mask_bin = cv2.resize(
                mask_bin, (target_w, target_h), interpolation=cv2.INTER_NEAREST
            )
        if mask_bin.sum() == 0:
            continue

        raw_name = names_dict.get(int(cls_id), str(cls_id))
        class_name = normalize_ai_class(raw_name, site_name=site_name)
        color = np.array(get_color(class_name, site_name=site_name), dtype=np.uint8)
        overlay[mask_bin == 1] = color
        conf = None
        if confs is not None and i < len(confs):
            conf = float(confs[i])
        label = class_name if conf is None else f"{class_name} {conf:.2f}"
        _draw_mask_label(overlay, label, mask_bin)

        if int(cls_id) in roi_classes:
            roi_mask = cv2.bitwise_or(roi_mask, mask_bin)

        prev = class_masks.get(int(cls_id))
        class_masks[int(cls_id)] = (
            mask_bin if prev is None else cv2.bitwise_or(prev, mask_bin)
        )
        class_instances.setdefault(int(cls_id), []).append(mask_bin)

    return overlay, roi_mask, class_masks, class_instances


def draw_roi_contours(image, roi_mask, color=None):
    if roi_mask.sum() == 0:
        return image
    contours, _ = cv2.findContours(
        roi_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    contour_color = (0, 255, 255) if color is None else color
    cv2.polylines(image, contours, True, contour_color, 2)
    return image


def draw_class_contours(image, class_masks, class_name_by_id, site_name=None):
    if not class_masks:
        return image
    for cls_id, mask in class_masks.items():
        if mask is None or mask.sum() == 0:
            continue
        contours, _ = cv2.findContours(
            mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours:
            continue
        class_name = class_name_by_id.get(int(cls_id), str(cls_id))
        color = get_color(class_name, site_name=site_name)
        cv2.polylines(image, contours, True, color, 2)
    return image


def draw_count_boxes_from_detections(image, detections, site_name=None):
    if not detections:
        return image

    for det in detections:
        bbox = det.get("bbox") or {}
        x1 = int(bbox.get("x1", 0))
        y1 = int(bbox.get("y1", 0))
        x2 = int(bbox.get("x2", 0))
        y2 = int(bbox.get("y2", 0))
        cls_name = det.get("class", "unknown")
        conf = det.get("confidence")

        color = get_color(cls_name, site_name=site_name)
        cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
        label = cls_name if conf is None else f"{cls_name} {float(conf):.2f}"
        cv2.putText(
            image,
            label,
            (x1, max(y1 - 6, 12)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            1,
            cv2.LINE_AA,
        )

    return image


def draw_worker_boxes_from_detections(image, detections):
    if not detections:
        return image

    for det in detections:
        if det.get("class") != "worker":
            continue
        bbox = det.get("bbox") or {}
        x1 = int(bbox.get("x1", 0))
        y1 = int(bbox.get("y1", 0))
        x2 = int(bbox.get("x2", 0))
        y2 = int(bbox.get("y2", 0))

        cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 0), 2)

    return image


def count_workers_from_detections(detections):
    if not detections:
        return 0
    return sum(1 for det in detections if det.get("class") == "worker")


def apply_work_type_overlay(image, work_type_mask, work_type_label):
    if work_type_mask is None or not work_type_label:
        return image
    orange = (0, 165, 255)
    highlight_overlay = image.copy()
    highlight_overlay[work_type_mask == 1] = orange
    blended = cv2.addWeighted(image, 0.6, highlight_overlay, 0.4, 0)
    contours, _ = cv2.findContours(
        work_type_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if contours:
        cv2.polylines(blended, contours, True, (0, 0, 0), 6)
        cv2.polylines(blended, contours, True, orange, 3)
    h, w = blended.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 1.8
    thickness = 4
    (tw, th), _ = cv2.getTextSize(work_type_label, font, scale, thickness)
    moments = cv2.moments(work_type_mask.astype(np.uint8))
    if moments["m00"] != 0:
        cx = int(moments["m10"] / moments["m00"])
        cy = int(moments["m01"] / moments["m00"])
    else:
        cx, cy = w // 2, h // 2
    tx = max(10, min(w - tw - 10, cx - tw // 2))
    ty = max(th + 10, min(h - 10, cy))
    cv2.putText(
        blended,
        work_type_label,
        (tx, ty),
        font,
        scale,
        (0, 0, 0),
        thickness + 2,
        cv2.LINE_AA,
    )
    cv2.putText(
        blended,
        work_type_label,
        (tx, ty),
        font,
        scale,
        (255, 255, 255),
        thickness,
        cv2.LINE_AA,
    )
    return blended


def draw_worker_count_label(image, worker_count):
    cv2.putText(
        image,
        f"Workers: {worker_count}",
        (10, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        image,
        f"Workers: {worker_count}",
        (10, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    return image
