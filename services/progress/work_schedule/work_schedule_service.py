from datetime import datetime, timezone
from typing import List, Optional, Tuple

from fastapi import HTTPException

from core.database import work_schedules_collection, floorplans_collection, tours_collection


SUPPORTED_WORK_SCHEDULE_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
)


def _project_filter(project_id: str) -> dict:
    return {"$or": [{"site_name": project_id}, {"dxf_project_id": project_id}]}


def parse_work_schedule_date(value: str) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None

    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass

    for fmt in SUPPORTED_WORK_SCHEDULE_DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def normalize_work_schedule_date(value: str) -> str:
    parsed = parse_work_schedule_date(value)
    if parsed is None:
        raise HTTPException(
            400,
            "Invalid work schedule date. Use YYYY-MM-DD or DD-MM-YYYY format",
        )
    return parsed.date().isoformat()


def _normalize_schedule_activity(activity: dict) -> dict:
    normalized = dict(activity)
    normalized["start_date"] = normalize_work_schedule_date(activity.get("start_date", ""))
    normalized["end_date"] = normalize_work_schedule_date(activity.get("end_date", ""))
    return normalized


def save_work_schedule(project_id: str, source: str, activities: List[dict]) -> dict:
    now = datetime.now(timezone.utc)
    normalized_activities = [_normalize_schedule_activity(activity) for activity in activities]
    floorplans_collection.update_many(
        _project_filter(project_id),
        {
            "$set": {
                "site_name": project_id,
                "work_schedule": {
                    "source": source,
                    "activities": normalized_activities,
                    "updated_at": now,
                },
                "updated_at": now,
            }
        },
        upsert=False,
    )

    return {"status": "saved", "project_id": project_id}


def list_work_schedules(project_id: str) -> dict:
    if not project_id:
        raise HTTPException(400, "project_id is required")
    doc = floorplans_collection.find_one(_project_filter(project_id), sort=[("_id", -1)])
    if not doc:
        return {"schedules": []}

    schedule = doc.get("work_schedule")
    if isinstance(schedule, dict):
        return {"schedules": [schedule]}

    # Legacy fallback for older records that still use work_schedules collection.
    legacy_docs = list(work_schedules_collection.find({"project_id": project_id}).sort("_id", -1))
    for legacy_doc in legacy_docs:
        legacy_doc["_id"] = str(legacy_doc["_id"])
    return {"schedules": legacy_docs}


def latest_work_schedule(project_id: str) -> dict:
    if not project_id:
        raise HTTPException(400, "project_id is required")
    doc = floorplans_collection.find_one(_project_filter(project_id), sort=[("_id", -1)])
    if doc and isinstance(doc.get("work_schedule"), dict):
        return {"schedule": doc["work_schedule"]}

    legacy_doc = work_schedules_collection.find_one({"project_id": project_id}, sort=[("_id", -1)])
    if not legacy_doc:
        raise HTTPException(404, "No work schedule found")
    legacy_doc["_id"] = str(legacy_doc["_id"])
    return {"schedule": legacy_doc}


def _parse_date(value: str) -> Optional[datetime]:
    return parse_work_schedule_date(value)


def _parse_timestamp(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1000000000000:
            timestamp /= 1000.0
        try:
            return datetime.fromtimestamp(timestamp)
        except (OverflowError, OSError, ValueError):
            return None

    raw = str(value).strip()
    if not raw:
        return None

    parsed_schedule_date = parse_work_schedule_date(raw)
    if parsed_schedule_date is not None:
        return parsed_schedule_date

    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _tour_observed_datetime(tour: dict) -> Optional[datetime]:
    captured_at = _parse_timestamp(tour.get("captured_at"))
    if captured_at is not None:
        return captured_at
    return _parse_timestamp(tour.get("created_at"))


def _fetch_tours_for_project(project_id: str):
    floorplans = list(floorplans_collection.find(_project_filter(project_id), {"id": 1}))
    floorplan_ids = {fp.get("id") for fp in floorplans if fp.get("id")}
    if not floorplan_ids:
        return []
    return list(tours_collection.find({"floorplan_id": {"$in": list(floorplan_ids)}}))


def _collect_work_types(tours: list) -> Tuple[set, List[str]]:
    work_types = set()
    tour_ids = []
    for tour in tours:
        tour_ids.append(tour.get("tour_id"))
        for node in tour.get("nodes", []) or []:
            work_type = node.get("work_type")
            if isinstance(work_type, str) and work_type.strip():
                work_types.add(work_type.strip().lower())
    return work_types, [t for t in tour_ids if t]


def _activity_has_work(activity_name: str, work_types: set) -> bool:
    if not activity_name or not work_types:
        return False
    name = activity_name.strip().lower().replace("_", " ")
    for work_type in work_types:
        label = work_type.replace("_", " ")
        if label in name:
            return True
    return False


def _collect_activity_evidence(activity_name: str, tours: list, site_name: str, limit: int = 10) -> list:
    if not activity_name:
        return []
    name = activity_name.strip().lower().replace("_", " ")
    matches = []
    for tour in tours:
        tour_id = tour.get("tour_id")
        tour_name = tour.get("name") or "Tour"
        observed_at = _tour_observed_datetime(tour)
        nodes = tour.get("nodes", []) or []
        total_nodes = len(nodes)
        for idx, node in enumerate(nodes):
            work_type = node.get("work_type")
            if not isinstance(work_type, str) or not work_type.strip():
                continue
            label = work_type.strip().lower().replace("_", " ")
            if label not in name:
                continue
            image_url = node.get("segmentedImageUrl") or node.get("imageUrl")
            if not image_url:
                continue
            matches.append({
                "tour_id": tour_id,
                "tour_name": tour_name,
                "site_name": site_name,
                "observed_at": observed_at.date().isoformat() if observed_at is not None else "",
                "node_id": node.get("id"),
                "node_index": node.get("index") or idx + 1,
                "total_nodes": total_nodes,
                "work_type": work_type,
                "image_url": image_url,
            })
            if len(matches) >= limit:
                return matches
    return matches


def _evidence_observed_range(evidence: list) -> tuple[str, str]:
    observed_dates = []
    for item in evidence:
        parsed = _parse_timestamp(item.get("observed_at"))
        if parsed is not None:
            observed_dates.append(parsed)

    if not observed_dates:
        return "", ""

    observed_dates.sort()
    return (
        observed_dates[0].date().isoformat(),
        observed_dates[-1].date().isoformat(),
    )


def work_schedule_comparison(project_id: str) -> dict:
    if not project_id:
        raise HTTPException(400, "project_id is required")
    floorplan_doc = floorplans_collection.find_one(
        _project_filter(project_id), sort=[("_id", -1)]
    )
    schedule = floorplan_doc.get("work_schedule") if floorplan_doc else None
    if not isinstance(schedule, dict):
        schedule = work_schedules_collection.find_one({"project_id": project_id}, sort=[("_id", -1)])
    if not schedule:
        raise HTTPException(404, "No work schedule found")

    tours = _fetch_tours_for_project(project_id)
    work_types, tour_ids = _collect_work_types(tours)
    today = datetime.now()

    results = []
    for activity in schedule.get("activities", []):
        start_date = _parse_date(activity.get("start_date", ""))
        end_date = _parse_date(activity.get("end_date", ""))
        evidence = _collect_activity_evidence(activity.get("activity_name", ""), tours, project_id)
        observed_start_date, observed_end_date = _evidence_observed_range(evidence)
        matched_nodes = len(evidence)
        actual_percent = min(matched_nodes, 5) * 20

        primary_status = "NOT STARTED"
        if actual_percent >= 100:
            primary_status = "DONE"
        elif start_date and today < start_date:
            primary_status = "NOT STARTED"
        elif matched_nodes > 0 or _activity_has_work(activity.get("activity_name", ""), work_types):
            primary_status = "IN PROGRESS"

        is_critical = bool(end_date and today > end_date and primary_status != "DONE")

        results.append({
            **activity,
            "actual_percent": actual_percent,
            "status": primary_status,
            "primary_status": primary_status,
            "is_critical": is_critical,
            "observed_start_date": observed_start_date,
            "observed_end_date": observed_end_date,
            "related_tour_ids": tour_ids,
            "evidence": evidence,
        })

    return {"project_id": project_id, "activities": results, "actual_percent": None}
