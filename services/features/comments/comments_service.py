# Comments service: create/list issues and annotations.
# Coordinates DB writes and report generation.

import time
import uuid
import math
import mimetypes

from fastapi import HTTPException

from core.database import floorplans_collection, tours_collection
from services.features.comments.report_generation import generate_issue_report_pdf


def _first_non_empty_string(*values) -> str:
    for value in values:
        if value is None:
            continue
        parsed = str(value).strip()
        if parsed and parsed.lower() != "null":
            return parsed
    return ""


def _attachment_candidates(payload: dict) -> list:
    return [
        payload.get("attachments"),
        payload.get("attachment"),
        payload.get("attachment_url"),
        payload.get("attachmentUrl"),
        payload.get("image_url"),
        payload.get("imageUrl"),
        payload.get("visual_evidence"),
        payload.get("visualEvidence"),
        payload.get("evidence_image_url"),
        payload.get("evidenceImageUrl"),
    ]


def _guess_attachment_type(url_or_path: str) -> str:
    guessed, _ = mimetypes.guess_type(url_or_path)
    return guessed or "image/jpeg"


def _normalize_image_attachments(payload: dict) -> list[dict]:
    normalized: list[dict] = []
    seen: set[str] = set()

    def add_candidate(item) -> None:
        if item is None:
            return
        if isinstance(item, str):
            url = item.strip()
            if not url or url in seen:
                return
            seen.add(url)
            normalized.append(
                {
                    "name": url.rsplit("/", 1)[-1] or "attachment",
                    "type": _guess_attachment_type(url),
                    "size": 0,
                    "url": url,
                }
            )
            return
        if isinstance(item, dict):
            url = _first_non_empty_string(
                item.get("url"),
                item.get("data_url"),
                item.get("imageUrl"),
                item.get("image_url"),
                item.get("attachmentUrl"),
                item.get("attachment_url"),
                item.get("visualEvidence"),
                item.get("visual_evidence"),
                item.get("evidenceImageUrl"),
                item.get("evidence_image_url"),
                item.get("path"),
                item.get("file"),
            )
            if not url or url in seen:
                return
            seen.add(url)
            normalized.append(
                {
                    "name": _first_non_empty_string(
                        item.get("name"), url.rsplit("/", 1)[-1], "attachment"
                    ),
                    "type": _first_non_empty_string(
                        item.get("type"), _guess_attachment_type(url)
                    ),
                    "size": item.get("size") or 0,
                    "url": item.get("url") or url,
                    **({"data_url": item["data_url"]} if item.get("data_url") else {}),
                }
            )

    existing = payload.get("image_attachments")
    if isinstance(existing, list):
        for item in existing:
            add_candidate(item)

    for candidate in _attachment_candidates(payload):
        if isinstance(candidate, list):
            for item in candidate:
                add_candidate(item)
        else:
            add_candidate(candidate)

    return normalized


def get_all_comments(tour_id: str) -> dict:
    tour = tours_collection.find_one({"tour_id": tour_id})
    if not tour:
        raise HTTPException(404, "Tour not found")

    all_comments = []
    for node in tour.get("nodes", []):
        node_id = node.get("id")
        for comment in node.get("comments", []):
            all_comments.append({
                **comment,
                "pano_id": node_id,
            })

    return {"comments": all_comments}


def _is_indoor_tour(tour: dict) -> bool:
    mode = str(tour.get("capture_mode") or "").strip().lower()
    return mode in {"indoor_video", "indoor_capture", "indoor"}


def _is_number(value) -> bool:
    return isinstance(value, (int, float))


def _collect_shared_indoor_comments(
    *,
    tour_id: str,
    pano_id: str,
    floorplan_id: str,
    center_x: float,
    center_y: float,
    radius_px: int,
) -> list[dict]:
    radius = float(radius_px or 40)
    if radius <= 0:
        radius = 40.0
    radius_sq = radius * radius

    pipeline = [
        {
            "$match": {
                "floorplan_id": floorplan_id,
                "capture_mode": "indoor_video",
            }
        },
        {"$unwind": "$nodes"},
        {"$match": {"nodes.comments": {"$exists": True, "$ne": []}}},
        {"$unwind": "$nodes.comments"},
        {
            "$project": {
                "_id": 0,
                "tour_id": 1,
                "tour_name": "$name",
                "node_id": "$nodes.id",
                "node_x": "$nodes.x",
                "node_y": "$nodes.y",
                "comment": "$nodes.comments",
            }
        },
    ]

    shared_comments: list[dict] = []
    for row in tours_collection.aggregate(pipeline):
        source_tour_id = row.get("tour_id")
        source_node_id = row.get("node_id")

        # Local node comments are loaded separately and should not be duplicated.
        if source_tour_id == tour_id and source_node_id == pano_id:
            continue

        node_x = row.get("node_x")
        node_y = row.get("node_y")
        if not _is_number(node_x) or not _is_number(node_y):
            continue

        dx = float(node_x) - float(center_x)
        dy = float(node_y) - float(center_y)
        dist_sq = dx * dx + dy * dy
        if dist_sq > radius_sq:
            continue

        comment = row.get("comment") or {}
        if not isinstance(comment, dict):
            continue

        enriched = {
            **comment,
            "tour_id": comment.get("tour_id") or source_tour_id,
            "pano_id": comment.get("pano_id") or source_node_id,
            "source_tour_id": source_tour_id,
            "source_tour_name": row.get("tour_name"),
            "distance_px": round(math.sqrt(dist_sq), 2),
            "is_shared": True,
        }
        shared_comments.append(enriched)

    return shared_comments


def build_comment_report(comment_id: str) -> str:
    print(f"[comment-report] requested comment_id={comment_id}")
    tour = tours_collection.find_one({"nodes.comments.id": comment_id})
    if not tour:
        print("[comment-report] tour not found for comment")
        raise HTTPException(404, "Comment not found")

    comment = None
    node = None
    for n in tour.get("nodes", []):
        for c in n.get("comments", []) or []:
            if c.get("id") == comment_id:
                comment = c
                node = n
                break
        if comment:
            break

    if not comment:
        print("[comment-report] comment not found inside tour nodes")
        raise HTTPException(404, "Comment not found")

    floorplan = None
    if tour.get("floorplan_id"):
        floorplan = floorplans_collection.find_one({"id": tour["floorplan_id"]})
    print(
        "[comment-report] resolved data",
        {
            "tour_id": tour.get("tour_id"),
            "floorplan_id": tour.get("floorplan_id"),
            "has_node": node is not None,
            "has_floorplan": floorplan is not None,
        },
    )

    try:
        pdf_path = generate_issue_report_pdf(
            issue=comment,
            tour=tour,
            node=node,
            floorplan=floorplan,
        )
    except Exception as exc:
        print(f"[comment-report] generation failed: {exc}")
        raise HTTPException(500, f"Comment report generation failed: {exc}")

    print(f"[comment-report] generated pdf={pdf_path}")
    return pdf_path


def get_comments_for_pano(tour_id: str, pano_id: str, include_shared: bool = False, radius_px: int = 40) -> dict:
    tour = tours_collection.find_one({"tour_id": tour_id})
    if not tour:
        raise HTTPException(404, "Tour not found")

    node = next((n for n in tour.get("nodes", []) if n["id"] == pano_id), None)
    if not node:
        return {"comments": []}

    local_comments = []
    for comment in node.get("comments", []):
        if not isinstance(comment, dict):
            continue
        local_comments.append(
            {
                **comment,
                "tour_id": comment.get("tour_id") or tour_id,
                "pano_id": comment.get("pano_id") or pano_id,
                "is_shared": False,
            }
        )

    if not include_shared:
        return {"comments": local_comments}

    if not _is_indoor_tour(tour):
        return {"comments": local_comments}

    floorplan_id = tour.get("floorplan_id")
    node_x = node.get("x")
    node_y = node.get("y")
    if not floorplan_id or not _is_number(node_x) or not _is_number(node_y):
        return {"comments": local_comments}

    shared_comments = _collect_shared_indoor_comments(
        tour_id=tour_id,
        pano_id=pano_id,
        floorplan_id=floorplan_id,
        center_x=float(node_x),
        center_y=float(node_y),
        radius_px=radius_px,
    )

    # De-duplicate by comment id, preferring local comments.
    seen: set[str] = set()
    merged: list[dict] = []
    for comment in [*local_comments, *shared_comments]:
        comment_id = comment.get("id")
        if isinstance(comment_id, str) and comment_id:
            if comment_id in seen:
                continue
            seen.add(comment_id)
        merged.append(comment)

    return {"comments": merged}


def create_comment(payload: dict) -> dict:
    comment_data = {k: v for k, v in payload.items() if v is not None}
    image_attachments = _normalize_image_attachments(comment_data)
    if image_attachments:
        comment_data["image_attachments"] = image_attachments
    comment_data["id"] = f"comment_{uuid.uuid4().hex}"
    comment_data["created_at"] = int(time.time() * 1000)

    result = tours_collection.update_one(
        {
            "tour_id": comment_data["tour_id"],
            "nodes.id": comment_data["pano_id"],
        },
        {
            "$push": {"nodes.$.comments": comment_data},
        },
    )

    if result.modified_count == 0:
        raise HTTPException(404, "Failed to save comment")

    return {"message": "Comment saved", "comment": comment_data}


def delete_comment(comment_id: str) -> dict:
    tour = tours_collection.find_one({
        "nodes.comments.id": comment_id
    })

    if not tour:
        raise HTTPException(404, "Comment not found")

    result = tours_collection.update_one(
        {"nodes.comments.id": comment_id},
        {"$pull": {"nodes.$[].comments": {"id": comment_id}}},
    )

    if result.modified_count == 0:
        raise HTTPException(404, "Comment not removed")

    return {"status": "deleted", "comment_id": comment_id}


def update_comment(comment_id: str, payload: dict) -> dict:
    update_fields = {
        "nodes.$[n].comments.$[b].updated_at": int(time.time() * 1000),
    }

    field_mapping = {
        "title": "title",
        "description": "description",
        "severity": "severity",
        "department": "department",
        "discipline": "discipline",
        "status": "status",
        "issue_type": "issue_type",
        "issueType": "issueType",
        "type": "type",
        "response": "response",
        "action_required": "action_required",
        "actionRequired": "actionRequired",
        "action_request": "action_request",
        "response_by": "response_by",
        "responseBy": "responseBy",
        "assigned_to": "assigned_to",
        "assignedTo": "assignedTo",
        "responsible_party": "responsible_party",
        "attachment_url": "attachment_url",
        "attachmentUrl": "attachmentUrl",
        "image_url": "image_url",
        "imageUrl": "imageUrl",
        "evidence_image_url": "evidence_image_url",
        "evidenceImageUrl": "evidenceImageUrl",
        "visual_evidence": "visual_evidence",
        "visualEvidence": "visualEvidence",
    }
    for payload_key, db_key in field_mapping.items():
        if payload.get(payload_key) is not None:
            update_fields[f"nodes.$[n].comments.$[b].{db_key}"] = payload.get(
                payload_key
            )

    image_attachments = _normalize_image_attachments(payload)
    if image_attachments:
        update_fields["nodes.$[n].comments.$[b].image_attachments"] = image_attachments

    result = tours_collection.update_one(
        {"nodes.comments.id": comment_id},
        {"$set": update_fields},
        array_filters=[
            {"n.comments": {"$exists": True}},
            {"b.id": comment_id},
        ],
    )

    if result.modified_count == 0:
        raise HTTPException(400, "Comment update failed")

    return {"message": "Comment updated"}
