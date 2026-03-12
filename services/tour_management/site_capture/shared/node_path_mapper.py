import os
from typing import Optional


def normalize_node_paths(tour_id: str, node: dict, storage_key: Optional[str] = None) -> dict:
    segment = storage_key or tour_id
    filename = node.get("filename")
    if filename:
        node["imageUrl"] = f"/streetview/{segment}/raw/{filename}"

    detected_name = node.get("detectedImageUrl") or ""
    if detected_name and detected_name.endswith(".jpg"):
        base = os.path.basename(detected_name)
        if base.endswith("_det.jpg") or base.endswith("_count.jpg"):
            node["detectedImageUrl"] = f"/streetview/{segment}/detect/{base}"

    segmented_name = node.get("segmentedImageUrl") or ""
    if segmented_name and segmented_name.endswith(".jpg"):
        base = os.path.basename(segmented_name)
        if base.endswith("_seg.jpg"):
            node["segmentedImageUrl"] = f"/streetview/{segment}/detect+seg/{base}"

    return node

