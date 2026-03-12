# Visualization helpers: drawing overlays and images.
# Shared by AI and reporting logic.

# utils/visualization.py

import cv2
import numpy as np

from typing import Optional

from core.site_config import get_class_colors
from utils.ai_classes import normalize_ai_class


def get_color(class_name: str, site_name: Optional[str] = None):
    colors = get_class_colors(site_name)
    normalized = normalize_ai_class(class_name, site_name)
    return colors.get(normalized, colors.get("default", (200, 200, 200)))


# ---------------------------------------------------------
# Draw bounding boxes (COUNT AI)
# ---------------------------------------------------------
def draw_count_boxes(image, boxes, sx, sy, model):
    """
    image: BGR image
    boxes: YOLO boxes
    model: YOLO model (for class names)
    """

    annotated = image.copy()

    for box in boxes:
        cls_id = int(box.cls[0])
        cls_name = normalize_ai_class(model.names[cls_id])
        conf = float(box.conf[0])

        x1, y1, x2, y2 = box.xyxy[0].tolist()

        x1 = int(x1 * sx)
        x2 = int(x2 * sx)
        y1 = int(y1 * sy)
        y2 = int(y2 * sy)

        color = get_color(cls_name)

        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        cv2.putText(
            annotated,
            f"{cls_name} {conf:.2f}",
            (x1, max(y1 - 5, 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            1,
            cv2.LINE_AA,
        )

    return annotated


# ---------------------------------------------------------
# Draw segmentation masks (SEG AI)
# ---------------------------------------------------------
def draw_segmentation_masks(image, results, model):
    """
    image: BGR image
    results: YOLO segmentation result
    """

    annotated = image.copy()

    if results.masks is None:
        return annotated

    masks = results.masks.data.cpu().numpy()

    for i, box in enumerate(results.boxes):
        cls_id = int(box.cls[0])
        cls_name = normalize_ai_class(model.names[cls_id])

        mask = cv2.resize(masks[i], (image.shape[1], image.shape[0]))
        color = np.array(get_color(cls_name), dtype=np.uint8)

        annotated[mask > 0.5] = annotated[mask > 0.5] * 0.6 + color * 0.4

    return annotated
