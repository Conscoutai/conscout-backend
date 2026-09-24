"""Dated, reviewed P6 progress snapshots. Never mutate baseline or evidence rows."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from bson import BSON
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from core.config import site_baseline_dir
from core.database import schedule_activities_collection, schedule_updates_collection
from .baseline_service import active_schedule_baseline, resolve_project
from .xer_parser import XerParseError, _decode_xer, _parse_tables, parse_xer

MAX_UPDATE_BYTES = 10 * 1024 * 1024


def reported_percent(activity: dict) -> float | None:
    """P6 activity % complete follows its configured type, not always physical %."""
    status = activity.get("status_code")
    if status == "TK_Complete":
        return 100.0
    if status == "TK_NotStart":
        return 0.0
    if status != "TK_Active":
        return None
    kind = activity.get("completion_type")
    if kind == "CP_Phys":
        value = float(activity.get("physical_complete_percent") or 0)
    elif kind == "CP_Drtn":
        total = float(activity.get("target_duration_hours") or 0)
        remaining = float(activity.get("remaining_duration_hours") or 0)
        if total <= 0 or remaining < 0:
            return None
        value = 100 * (total - remaining) / total
    elif kind == "CP_Units":
        actual = float(activity.get("actual_units") or 0)
        remaining = float(activity.get("remaining_units") or 0)
        if actual < 0 or remaining < 0 or actual + remaining <= 0:
            return None
        value = 100 * actual / (actual + remaining)
    else:
        return None
    return round(min(100.0, max(0.0, value)), 3) if math.isfinite(value) else None


def latest_accepted(baseline_id: str, as_of: str | None = None) -> dict | None:
    query: dict[str, Any] = {
        "baseline_id": baseline_id,
        "status": "accepted",
        "removed_at": None,
    }
    if as_of:
        query["data_date"] = {"$lte": as_of[:10]}
    # Sorting by reporting date, not upload/accept time, prevents a concurrent
    # acceptance of an older snapshot from rolling back the displayed progress.
    return schedule_updates_collection.find_one(
        query, sort=[("data_date", -1), ("accepted_at", -1), ("update_id", -1)]
    )


def public_update(document: dict) -> dict:
    return {
        key: value
        for key, value in document.items()
        if key
        not in {"_id", "owner_user_id", "owner_email", "activities", "source_sha256"}
    }


def build_review(parsed: dict, baseline_activities: list[dict], baseline: dict) -> dict:
    original = {a["activity_id"]: a for a in baseline_activities}
    incoming = parsed["activities"]
    ids = [a["activity_id"] for a in incoming]
    if len(ids) != len(set(ids)):
        raise HTTPException(
            422, "Duplicate Activity IDs in the XER; correct the file before importing."
        )
    if not set(ids) & original.keys():
        raise HTTPException(
            422,
            "No Activity IDs match the active baseline. Check the selected project.",
        )
    data_date = str(parsed["project"].get("data_date") or "")[:10]
    if not data_date:
        raise HTTPException(
            422,
            "The XER has no valid reporting/data date. Set the P6 data date and export again.",
        )
    if data_date > datetime.now(timezone.utc).date().isoformat():
        raise HTTPException(422, "The XER reporting date is in the future.")
    warnings: list[dict] = []
    added = sorted(set(ids) - original.keys())
    missing = sorted(original.keys() - set(ids))
    renamed = [
        a["activity_id"]
        for a in incoming
        if a["activity_id"] in original
        and a["activity_name"] != original[a["activity_id"]]["activity_name"]
    ]
    for code, values, message in [
        (
            "unmatched_activities",
            added,
            "New Activity IDs are kept in this update but excluded from baseline progress.",
        ),
        (
            "missing_activities",
            missing,
            "Baseline activities missing from this update have no reported value; they are not assumed complete or zero.",
        ),
        (
            "renamed_activities",
            renamed,
            "Activity names differ; matching uses Activity IDs and retains baseline mappings.",
        ),
    ]:
        if values:
            warnings.append({"code": code, "message": message, "activity_ids": values})
    activities = []
    status_map = {
        "TK_Complete": "Completed",
        "TK_Active": "In progress",
        "TK_NotStart": "Not started",
    }
    for activity in incoming:
        aid = activity["activity_id"]
        percent = reported_percent(activity)
        if percent is None:
            warnings.append(
                {
                    "code": "unknown_completion",
                    "activity_ids": [aid],
                    "message": f"{aid}: completion cannot be calculated from its P6 completion type/units/duration.",
                }
            )
        start, finish = activity.get("actual_start_at", ""), activity.get(
            "actual_end_at", ""
        )
        if any(d and d[:10] > data_date for d in (start, finish)) or (
            start and finish and finish < start
        ):
            warnings.append(
                {
                    "code": "invalid_actual_dates",
                    "activity_ids": [aid],
                    "message": f"{aid}: actual dates need review (start {start or 'unset'}, finish {finish or 'unset'}; reporting date {data_date}).",
                }
            )
        activities.append(
            {
                "activity_id": aid,
                "activity_name": activity["activity_name"],
                "reported_percent": percent,
                "reported_status": status_map.get(
                    activity.get("status_code"), "Unknown"
                ),
                "completion_type": activity.get("completion_type"),
                "actual_start_at": start,
                "actual_end_at": finish,
                "forecast_start_at": activity.get("target_start_at"),
                "forecast_finish_at": activity.get("target_end_at"),
                "matched": aid in original,
            }
        )
    old_relationships = int(
        (baseline.get("summary") or {}).get("relationship_count") or 0
    )
    new_relationships = int(parsed["summary"].get("relationship_count") or 0)
    if old_relationships != new_relationships:
        warnings.append(
            {
                "code": "changed_relationships",
                "message": f"Relationship count changed from {old_relationships} to {new_relationships}. Original baseline logic is retained.",
            }
        )
    source_name = parsed["project"].get("name", "")
    baseline_name = (baseline.get("project") or {}).get("name", "")
    if (
        source_name
        and baseline_name
        and source_name.strip().casefold() != baseline_name.strip().casefold()
    ):
        warnings.append(
            {
                "code": "project_name_changed",
                "message": f"XER project name differs from the baseline: {source_name}. Confirm this is the correct project.",
            }
        )
    costs = sum(float(a.get("target_cost") or 0) for a in baseline_activities)
    weight_field = "target_cost" if costs > 0 else "target_duration_hours"
    total_weight = sum(
        max(0, float(a.get(weight_field) or 0)) for a in baseline_activities
    )
    matched = [a for a in activities if a["matched"]]
    known = [a for a in matched if a["reported_percent"] is not None]
    known_weight = sum(
        max(0, float(original[a["activity_id"]].get(weight_field) or 0)) for a in known
    )
    numerator = sum(
        max(0, float(original[a["activity_id"]].get(weight_field) or 0))
        * a["reported_percent"]
        for a in known
    )
    full_coverage = len(known) == len(original)
    counts = Counter(a["reported_status"] for a in matched)
    return {
        "data_date": data_date,
        "activities": activities,
        "warnings": warnings,
        "forecast_finish_date": str(parsed["project"].get("planned_end_at") or "")[:10],
        "summary": {
            "activity_count": len(incoming),
            "matched_count": len(matched),
            "added_count": len(added),
            "missing_count": len(missing),
            "renamed_count": len(renamed),
            "unknown_percent_count": len(matched) - len(known),
            "completed_count": counts["Completed"],
            "in_progress_count": counts["In progress"],
            "not_started_count": counts["Not started"],
            "reported_percent": (
                round(numerator / total_weight, 3)
                if full_coverage and total_weight > 0
                else None
            ),
            "matched_reported_percent": (
                round(numerator / known_weight, 3) if known_weight > 0 else None
            ),
            "weight_coverage_percent": (
                round(100 * known_weight / total_weight, 3) if total_weight else 0
            ),
            "weighting_method": weight_field,
        },
    }


def import_schedule_update(
    *, project_ref: str, filename: str, raw_bytes: bytes, reviewer_email: str = ""
) -> dict:
    project = resolve_project(project_ref)
    baseline = active_schedule_baseline(project_ref)
    if not baseline or baseline.get("source_type") != "xer":
        raise HTTPException(
            409, "Activate the approved XER baseline before importing schedule updates."
        )
    safe_name = Path((filename or "update.xer").replace("\\", "/")).name
    if Path(safe_name).suffix.lower() != ".xer":
        raise HTTPException(
            400, "Schedule progress updates must be Primavera .xer files."
        )
    if not raw_bytes or len(raw_bytes) > MAX_UPDATE_BYTES:
        raise HTTPException(413, "Choose a nonempty XER of at most 10 MB.")
    digest = hashlib.sha256(raw_bytes).hexdigest()
    identity = {"baseline_id": baseline["baseline_id"], "source_sha256": digest}
    existing = schedule_updates_collection.find_one(identity)
    if existing:
        if existing.get("removed_at"):
            schedule_updates_collection.update_one(
                {"update_id": existing["update_id"]},
                {"$set": {"removed_at": None, "removed_by_email": ""}},
            )
            existing["removed_at"] = None
        return {"status": "already_imported", "update": public_update(existing)}
    try:
        if len(_parse_tables(_decode_xer(raw_bytes)).get("PROJECT", [])) != 1:
            raise XerParseError("Export exactly one project per schedule update.")
        parsed = parse_xer(raw_bytes, filename=safe_name)
    except (XerParseError, ValueError) as error:
        raise HTTPException(422, str(error)) from error
    original = list(
        schedule_activities_collection.find(
            {"baseline_id": baseline["baseline_id"]}, {"_id": 0}
        )
    )
    review = build_review(parsed, original, baseline)
    named_date = re.search(r"(\d{1,2})[-_](\d{1,2})[-_](\d{4})", safe_name)
    if named_date:
        try:
            day, month, year = (int(value) for value in named_date.groups())
            filename_date = datetime(year, month, day).date().isoformat()
        except ValueError:
            filename_date = ""
        if filename_date and filename_date != review["data_date"]:
            review["warnings"].append(
                {
                    "code": "filename_date_mismatch",
                    "message": f"Filename date {filename_date} differs from the P6 reporting date {review['data_date']}. The P6 reporting date is used.",
                }
            )
    now = datetime.now(timezone.utc)
    uid = f"update_{uuid4().hex}"
    document = {
        **identity,
        **review,
        "update_id": uid,
        "project_id": project["project_id"],
        "floorplan_id": project["floorplan_id"],
        "site_name": project["site_name"],
        "source_filename": safe_name,
        "status": "needs_review",
        "uploaded_at": now,
        "uploaded_by_email": reviewer_email,
        "source_project_name": parsed["project"].get("name", ""),
    }
    if len(BSON.encode(document)) > 12 * 1024 * 1024:
        raise HTTPException(413, "This schedule contains too much data for one update.")
    directory = Path(site_baseline_dir(project["project_id"])) / "updates"
    directory.mkdir(parents=True, exist_ok=True)
    storage_name = re.sub(r"[^\w. -]", "_", safe_name)
    path = directory / f"{uid}_{storage_name}"
    document["source_url"] = (
        f"/sites/{project['project_id']}/baseline/updates/{path.name}"
    )
    path.write_bytes(raw_bytes)
    try:
        schedule_updates_collection.insert_one(document)
    except DuplicateKeyError:
        path.unlink(missing_ok=True)
        existing = schedule_updates_collection.find_one(identity)
        if existing:
            return {"status": "already_imported", "update": public_update(existing)}
        raise
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {"status": "needs_review", "update": public_update(document)}


def list_schedule_updates(project_ref: str) -> dict:
    project = resolve_project(project_ref)
    baseline = active_schedule_baseline(project_ref)
    latest = latest_accepted(baseline["baseline_id"]) if baseline else None
    documents = schedule_updates_collection.find(
        {"project_id": project["project_id"], "removed_at": None},
        {"activities": 0},
    ).sort([("data_date", -1), ("uploaded_at", -1)])
    return {
        "updates": [public_update(d) for d in documents],
        "active_baseline_id": baseline["baseline_id"] if baseline else "",
        "latest_update_id": latest["update_id"] if latest else "",
    }


def remove_schedule_update(*, project_ref: str, update_id: str, reviewer_email: str = "") -> dict:
    project = resolve_project(project_ref)
    update = schedule_updates_collection.find_one(
        {"project_id": project["project_id"], "update_id": update_id, "removed_at": None}
    )
    if not update:
        raise HTTPException(404, "Schedule update not found for this project")
    schedule_updates_collection.update_one(
        {"project_id": project["project_id"], "update_id": update_id, "removed_at": None},
        {"$set": {
            "removed_at": datetime.now(timezone.utc),
            "removed_by_email": reviewer_email,
        }},
    )
    return {"status": "removed", "update_id": update_id}


def accept_schedule_update(
    *, project_ref: str, update_id: str, acknowledge_warnings: bool, reviewer_email: str
) -> dict:
    project = resolve_project(project_ref)
    document = schedule_updates_collection.find_one(
        {"project_id": project["project_id"], "update_id": update_id, "removed_at": None}
    )
    if not document:
        raise HTTPException(404, "Schedule update not found")
    baseline = active_schedule_baseline(project_ref)
    if not baseline or baseline["baseline_id"] != document["baseline_id"]:
        raise HTTPException(
            409, "The baseline changed. Reimport this XER against the active baseline."
        )
    if document["status"] == "accepted":
        return {"status": "already_accepted", "update": public_update(document)}
    if document["warnings"] and not acknowledge_warnings:
        raise HTTPException(
            409, "Review and acknowledge the import warnings before accepting."
        )
    latest = latest_accepted(baseline["baseline_id"])
    if latest and document["data_date"] <= latest["data_date"]:
        raise HTTPException(
            409,
            "This reporting date is not newer than the accepted update. It remains available in history.",
        )
    changes = {
        "status": "accepted",
        "accepted_at": datetime.now(timezone.utc),
        "accepted_by_email": reviewer_email,
        "warnings_acknowledged": acknowledge_warnings,
    }
    schedule_updates_collection.update_one(
        {"update_id": update_id, "status": "needs_review"}, {"$set": changes}
    )
    saved = schedule_updates_collection.find_one({"update_id": update_id})
    if not saved:
        raise HTTPException(404, "Schedule update was removed during review")
    return {"status": saved["status"], "update": public_update(saved)}


def attach_reported_progress(
    payload: dict,
    baseline_id: str,
    as_of: str,
    calendars: dict | None = None,
    baseline: dict | None = None,
) -> dict:
    update = latest_accepted(baseline_id, as_of)
    inherited_ids = None
    if not update and baseline and baseline.get("progress_parent"):
        from .baseline_progress_service import inherited_sources

        candidates = [
            (latest_accepted(source["baseline_id"], as_of), ids)
            for source, ids in inherited_sources(baseline)
        ]
        candidates = [(u, ids) for u, ids in candidates if u]
        if candidates:
            update, inherited_ids = max(
                candidates, key=lambda pair: pair[0]["data_date"]
            )
    if not update:
        return payload
    by_id = {
        a["activity_id"]: a
        for a in update["activities"]
        if a["matched"] and (inherited_ids is None or a["activity_id"] in inherited_ids)
    }
    differences = 0
    from .analytics_service import _planned_percent

    reporting_date = datetime.fromisoformat(update["data_date"]).date()
    for activity in payload["activities"]:
        reported = by_id.get(activity["activity_id"])
        if not reported:
            continue
        approved = [
            e
            for e in activity.get("evidence", [])
            if e.get("status") == "approved" and e.get("approved_percent") is not None
        ]
        different = bool(
            (activity.get("has_verified_progress") or approved)
            and reported["reported_percent"] is not None
            and abs(
                float(activity.get("actual_percent") or 0)
                - reported["reported_percent"]
            )
            > 0.1
        )
        differences += int(different)
        activity.update(
            {
                "reported_percent": reported["reported_percent"],
                "reported_status": reported["reported_status"],
                "reported_as_of": update["data_date"],
                "reported_planned_percent": _planned_percent(
                    activity, reporting_date, calendars
                ),
                "reported_actual_start_at": reported["actual_start_at"],
                "reported_actual_end_at": reported["actual_end_at"],
                "reported_forecast_finish_at": reported["forecast_finish_at"],
                "progress_difference": different,
            }
        )
    payload["reported_update"] = {
        **public_update(update),
        "difference_count": differences,
    }
    return payload
