from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from core.database import (
    floorplans_collection,
    schedule_activities_collection,
    schedule_baselines_collection,
    schedule_evidence_collection,
    schedule_progress_snapshots_collection,
    tours_collection,
)

from .analytics_service import build_baseline_comparison
from .baseline_service import resolve_project
from .xer_parser import normalize_work_category


def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        raw = float(value)
        if raw > 1_000_000_000_000:
            raw /= 1000.0
        try:
            parsed = datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _point_in_polygon(x: float, y: float, points: list[dict[str, Any]]) -> bool:
    coordinates = [
        (_number(point.get("x")), _number(point.get("y")))
        for point in points
        if isinstance(point, dict)
    ]
    coordinates = [
        (px, py) for px, py in coordinates if px is not None and py is not None
    ]
    if len(coordinates) < 3:
        return False
    inside = False
    j = len(coordinates) - 1
    for i, (xi, yi) in enumerate(coordinates):
        xj, yj = coordinates[j]
        crosses = (yi > y) != (yj > y)
        if crosses:
            intersection_x = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
            if x < intersection_x:
                inside = not inside
        j = i
    return inside


def _node_zone(node: dict[str, Any], floorplan: dict[str, Any]) -> str:
    explicit = str(
        node.get("schedule_zone")
        or node.get("zone")
        or (node.get("metadata") or {}).get("zone")
        or ""
    ).strip()
    if explicit:
        return explicit
    x = _number(node.get("x"))
    y = _number(node.get("y"))
    if x is None or y is None:
        return ""
    zone_plan = floorplan.get("schedule_zone_plan") or {}
    if str(zone_plan.get("confirmation_status") or "").strip() != "confirmed":
        return ""
    for zone in floorplan.get("schedule_zones", []) or []:
        if not isinstance(zone, dict):
            continue
        if _point_in_polygon(x, y, zone.get("points") or zone.get("polygon") or []):
            return str(zone.get("name") or zone.get("zone") or "").strip()
    return ""


def _candidate_activity(
    *,
    baseline_id: str,
    node: dict[str, Any],
    zone: str,
    category: str,
    observed_at: Optional[datetime],
) -> tuple[Optional[dict[str, Any]], float, str]:
    explicit_id = str(
        node.get("schedule_activity_id") or node.get("activity_id") or ""
    ).strip()
    if explicit_id:
        explicit = schedule_activities_collection.find_one(
            {"baseline_id": baseline_id, "activity_id": explicit_id}, {"_id": 0}
        )
        if explicit:
            return (
                explicit,
                0.98,
                "Explicit Activity ID supplied by the analysis result",
            )

    query: dict[str, Any] = {
        "baseline_id": baseline_id,
        "photo_trackable": True,
        "work_category": category,
    }
    if zone:
        query["zone"] = zone
    candidates = list(schedule_activities_collection.find(query, {"_id": 0}))
    if not candidates:
        return (
            None,
            0.0,
            "No photo-trackable activity matches the detected work and zone",
        )
    if not zone and len(candidates) > 1:
        return (
            None,
            0.0,
            "The panorama has no schedule zone and matches multiple activities",
        )
    if len(candidates) == 1:
        confidence = 0.86 if zone else 0.68
        return candidates[0], confidence, "Matched by work category and schedule zone"

    observed_date = observed_at.date() if observed_at else None

    def distance(candidate: dict[str, Any]) -> int:
        if not observed_date:
            return 0
        raw = str(candidate.get("start_date") or "")
        try:
            start_date = datetime.fromisoformat(raw).date()
        except ValueError:
            return 999999
        return abs((start_date - observed_date).days)

    candidates.sort(key=distance)
    return (
        candidates[0],
        0.72,
        "Matched by work category, zone and nearest planned date",
    )


def analyze_tour_schedule(tour_id: str) -> dict[str, Any]:
    tour = tours_collection.find_one({"tour_id": tour_id})
    if not tour:
        raise HTTPException(404, "Tour not found")
    floorplan = floorplans_collection.find_one({"id": tour.get("floorplan_id")}) or {}
    project_ref = str(
        tour.get("project_id")
        or tour.get("site_name")
        or floorplan.get("project_id")
        or floorplan.get("id")
        or floorplan.get("site_name")
        or ""
    ).strip()
    if not project_ref:
        return {"status": "skipped", "reason": "Tour is not linked to a project"}
    project_context = resolve_project(project_ref)
    baseline = schedule_baselines_collection.find_one(
        {"project_id": project_context["project_id"], "is_active": True}
    )
    if not baseline:
        return {"status": "skipped", "reason": "No active schedule baseline"}

    grouped: dict[str, dict[str, Any]] = {}
    unmatched_count = 0
    nodes = tour.get("nodes", []) or []
    for index, node in enumerate(nodes):
        work_type = str(node.get("work_type") or "").strip()
        category = normalize_work_category(work_type)
        if not category:
            continue
        zone = _node_zone(node, floorplan)
        observed_at = _timestamp(
            node.get("captured_at")
            or tour.get("capture_ended_at")
            or tour.get("captured_at")
        )
        activity, confidence, rationale = _candidate_activity(
            baseline_id=baseline["baseline_id"],
            node=node,
            zone=zone,
            category=category,
            observed_at=observed_at,
        )
        if not activity:
            unmatched_count += 1
            continue
        activity_key = str(activity.get("activity_internal_id") or "")
        item = grouped.setdefault(
            activity_key,
            {
                "activity": activity,
                "confidence": confidence,
                "rationale": rationale,
                "zone": zone,
                "work_type": work_type,
                "work_category": category,
                "nodes": [],
                "observed_times": [],
                "suggested_percent": None,
            },
        )
        item["confidence"] = max(float(item["confidence"]), confidence)
        item["nodes"].append(
            {
                "node_id": str(node.get("id") or ""),
                "node_index": node.get("index") or index + 1,
                "image_url": node.get("segmentedImageUrl")
                or node.get("imageUrl")
                or "",
            }
        )
        if observed_at:
            item["observed_times"].append(observed_at)
        suggested = _number(
            node.get("schedule_progress_percent")
            or node.get("completion_percent")
            or node.get("work_progress_percent")
        )
        if suggested is not None:
            item["suggested_percent"] = max(
                float(item["suggested_percent"] or 0.0),
                min(100.0, max(0.0, suggested)),
            )

    created_count = 0
    updated_count = 0
    auto_approved_count = 0
    now = datetime.now(timezone.utc)
    for activity_key, item in grouped.items():
        activity = item["activity"]
        evidence_nodes = item["nodes"]
        observed_times = item["observed_times"]
        captured_at = max(observed_times).isoformat() if observed_times else ""
        suggested_percent = item["suggested_percent"]
        explicit_match = item["confidence"] >= 0.95
        auto_approved = explicit_match and suggested_percent is not None
        evidence_filter = {
            "baseline_id": baseline["baseline_id"],
            "activity_internal_id": activity_key,
            "tour_id": tour_id,
        }
        existing = schedule_evidence_collection.find_one(evidence_filter)
        evidence_id = str(
            (existing or {}).get("evidence_id") or f"evidence_{uuid4().hex}"
        )
        primary_node = evidence_nodes[0] if evidence_nodes else {}
        payload = {
            "evidence_id": evidence_id,
            "baseline_id": baseline["baseline_id"],
            "project_id": project_context["project_id"],
            "floorplan_id": project_context["floorplan_id"],
            "site_name": project_context["site_name"],
            "activity_internal_id": activity_key,
            "activity_id": activity.get("activity_id"),
            "activity_name": activity.get("activity_name"),
            "tour_id": tour_id,
            "tour_name": tour.get("name") or "Tour",
            "node_id": primary_node.get("node_id"),
            "node_index": primary_node.get("node_index"),
            "total_nodes": len(nodes),
            "evidence_nodes": evidence_nodes,
            "image_url": primary_node.get("image_url") or "",
            "captured_at": captured_at,
            "uploaded_at": tour.get("uploaded_at") or tour.get("created_at"),
            "analysis_completed_at": now,
            "zone": item["zone"],
            "work_type": item["work_type"],
            "work_category": item["work_category"],
            "confidence": round(float(item["confidence"]), 3),
            "rationale": item["rationale"],
            "suggested_percent": suggested_percent,
            "status": "approved" if auto_approved else "needs_review",
            "approved_percent": suggested_percent if auto_approved else None,
            "review_source": "automatic" if auto_approved else "pending",
            "updated_at": now,
        }
        schedule_evidence_collection.update_one(
            evidence_filter,
            {
                "$set": payload,
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        if existing:
            updated_count += 1
        else:
            created_count += 1
        if auto_approved:
            auto_approved_count += 1

    return {
        "status": "analyzed",
        "baseline_id": baseline["baseline_id"],
        "created_count": created_count,
        "updated_count": updated_count,
        "auto_approved_count": auto_approved_count,
        "needs_review_count": len(grouped) - auto_approved_count,
        "unmatched_node_count": unmatched_count,
    }


def review_schedule_evidence(
    *,
    evidence_id: str,
    decision: str,
    approved_percent: Optional[float],
    verified_quantity: Optional[float],
    review_note: str,
    reviewer_user_id: str,
    reviewer_email: str,
) -> dict[str, Any]:
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"approved", "rejected"}:
        raise HTTPException(400, "decision must be approved or rejected")
    evidence = schedule_evidence_collection.find_one({"evidence_id": evidence_id})
    if not evidence:
        raise HTTPException(404, "Schedule evidence not found")
    activity = (
        schedule_activities_collection.find_one(
            {
                "baseline_id": evidence.get("baseline_id"),
                "activity_internal_id": evidence.get("activity_internal_id"),
            }
        )
        or {}
    )
    planned_quantity = float(activity.get("planned_quantity") or 0.0)
    if (
        normalized_decision == "approved"
        and approved_percent is None
        and verified_quantity is not None
        and planned_quantity > 0
    ):
        approved_percent = min(
            100.0, max(0.0, verified_quantity * 100.0 / planned_quantity)
        )
    if normalized_decision == "approved":
        if approved_percent is None or not 0 <= approved_percent <= 100:
            raise HTTPException(
                400,
                "Provide approved_percent, or verified_quantity for an activity with a planned quantity",
            )
    now = datetime.now(timezone.utc)
    schedule_evidence_collection.update_one(
        {"evidence_id": evidence_id},
        {
            "$set": {
                "status": normalized_decision,
                "approved_percent": (
                    approved_percent if normalized_decision == "approved" else None
                ),
                "verified_quantity": (
                    verified_quantity if normalized_decision == "approved" else None
                ),
                "review_note": str(review_note or "").strip(),
                "review_source": "human",
                "reviewed_at": now,
                "reviewed_by_user_id": reviewer_user_id,
                "reviewed_by_email": reviewer_email,
                "updated_at": now,
            }
        },
    )

    observed_at = _timestamp(evidence.get("captured_at")) or now
    comparison = build_baseline_comparison(
        str(evidence.get("project_id") or ""), as_of=observed_at.date()
    )
    if comparison:
        summary = comparison.get("summary") or {}
        schedule_progress_snapshots_collection.update_one(
            {
                "baseline_id": evidence.get("baseline_id"),
                "snapshot_date": observed_at.date().isoformat(),
            },
            {
                "$set": {
                    "project_id": evidence.get("project_id"),
                    "floorplan_id": evidence.get("floorplan_id"),
                    "baseline_id": evidence.get("baseline_id"),
                    "snapshot_date": observed_at.date().isoformat(),
                    "captured_at": observed_at,
                    "planned_percent": summary.get("planned_percent"),
                    "actual_percent": summary.get("actual_percent"),
                    "variance_percent": summary.get("variance_percent"),
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    updated = schedule_evidence_collection.find_one(
        {"evidence_id": evidence_id}, {"_id": 0}
    )
    return {"status": normalized_decision, "evidence": updated}


def record_manual_activity_progress(
    *,
    baseline_id: str,
    activity_id: str,
    observed_at: str,
    approved_percent: Optional[float],
    verified_quantity: Optional[float],
    note: str,
    reviewer_user_id: str,
    reviewer_email: str,
) -> dict[str, Any]:
    baseline = schedule_baselines_collection.find_one({"baseline_id": baseline_id})
    if not baseline:
        raise HTTPException(404, "Schedule baseline not found")
    activity = schedule_activities_collection.find_one(
        {"baseline_id": baseline_id, "activity_id": activity_id}
    )
    if not activity:
        raise HTTPException(404, "Schedule activity not found")
    observed = _timestamp(observed_at)
    if observed is None:
        raise HTTPException(400, "observed_at must be a valid ISO timestamp")
    if approved_percent is None and verified_quantity is None:
        raise HTTPException(400, "Provide approved_percent or verified_quantity")

    evidence_id = f"evidence_{uuid4().hex}"
    manual_tour_id = f"manual:{observed.date().isoformat()}"
    existing = schedule_evidence_collection.find_one(
        {
            "baseline_id": baseline_id,
            "activity_internal_id": activity.get("activity_internal_id"),
            "tour_id": manual_tour_id,
        }
    )
    if existing:
        evidence_id = str(existing.get("evidence_id") or evidence_id)
    now = datetime.now(timezone.utc)
    schedule_evidence_collection.update_one(
        {
            "baseline_id": baseline_id,
            "activity_internal_id": activity.get("activity_internal_id"),
            "tour_id": manual_tour_id,
        },
        {
            "$set": {
                "evidence_id": evidence_id,
                "baseline_id": baseline_id,
                "project_id": baseline.get("project_id"),
                "floorplan_id": baseline.get("floorplan_id"),
                "site_name": baseline.get("site_name"),
                "activity_internal_id": activity.get("activity_internal_id"),
                "activity_id": activity_id,
                "activity_name": activity.get("activity_name"),
                "tour_id": manual_tour_id,
                "tour_name": "Manual verified update",
                "captured_at": observed.isoformat(),
                "uploaded_at": now,
                "analysis_completed_at": now,
                "zone": activity.get("zone"),
                "work_type": activity.get("work_category"),
                "work_category": activity.get("work_category"),
                "confidence": 1.0,
                "rationale": "Progress entered and verified by a project administrator",
                "suggested_percent": approved_percent,
                "status": "needs_review",
                "review_source": "manual",
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return review_schedule_evidence(
        evidence_id=evidence_id,
        decision="approved",
        approved_percent=approved_percent,
        verified_quantity=verified_quantity,
        review_note=note,
        reviewer_user_id=reviewer_user_id,
        reviewer_email=reviewer_email,
    )
