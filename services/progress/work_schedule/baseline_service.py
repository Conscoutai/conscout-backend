from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from core.database import (
    floorplans_collection,
    schedule_activities_collection,
    schedule_assignments_collection,
    schedule_baselines_collection,
    schedule_evidence_collection,
    schedule_progress_snapshots_collection,
    schedule_relationships_collection,
)
from core.config import site_baseline_dir

from .pdf_parser import PdfScheduleParseError, parse_pdf_schedule
from .xer_parser import XerParseError, parse_xer


def project_filter(project_ref: str) -> dict[str, Any]:
    value = str(project_ref or "").strip()
    return {
        "$or": [
            {"id": value},
            {"project_id": value},
            {"site_name": value},
            {"dxf_project_id": value},
        ]
    }


def resolve_project(project_ref: str) -> dict[str, Any]:
    value = str(project_ref or "").strip()
    if not value:
        raise HTTPException(400, "project_id is required")
    project = floorplans_collection.find_one(project_filter(value), sort=[("_id", -1)])
    if not project:
        raise HTTPException(404, "Project not found")
    canonical_id = str(
        project.get("project_id")
        or project.get("id")
        or project.get("dxf_project_id")
        or value
    ).strip()
    site_name = str(project.get("site_name") or project.get("display_name") or value).strip()
    return {
        "document": project,
        "project_id": canonical_id,
        "site_name": site_name,
        "floorplan_id": str(project.get("id") or "").strip(),
    }


def _public_baseline(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "baseline_id": str(document.get("baseline_id") or ""),
        "project_id": str(document.get("project_id") or ""),
        "site_name": str(document.get("site_name") or ""),
        "version": int(document.get("version") or 0),
        "name": str(document.get("name") or ""),
        "source_type": str(document.get("source_type") or ""),
        "source_filename": str(document.get("source_filename") or ""),
        "source_url": str(document.get("source_url") or ""),
        "status": str(document.get("status") or "needs_review"),
        "is_active": document.get("is_active") is True,
        "timezone": str(document.get("timezone") or "UTC"),
        "project": document.get("project") or {},
        "summary": document.get("summary") or {},
        "warnings": document.get("warnings") or [],
        "calendars": document.get("calendars") or [],
        "uploaded_at": document.get("uploaded_at"),
        "activated_at": document.get("activated_at"),
    }


def _next_version(project_id: str) -> int:
    latest = schedule_baselines_collection.find_one(
        {"project_id": project_id}, sort=[("version", -1)]
    )
    return int((latest or {}).get("version") or 0) + 1


def import_schedule_baseline(
    *,
    project_ref: str,
    filename: str,
    raw_bytes: bytes,
    timezone_name: str = "UTC",
    activate: bool = False,
) -> dict[str, Any]:
    project_context = resolve_project(project_ref)
    project_id = project_context["project_id"]
    site_name = project_context["site_name"]
    safe_filename = Path(filename or "baseline.xer").name
    suffix = Path(safe_filename).suffix.lower()
    if suffix not in {".xer", ".pdf"}:
        raise HTTPException(400, "Schedule baseline must be an .xer or .pdf file")

    digest = hashlib.sha256(raw_bytes).hexdigest()
    resolved_timezone = str(timezone_name or "UTC").strip() or "UTC"
    try:
        ZoneInfo(resolved_timezone)
    except ZoneInfoNotFoundError as error:
        raise HTTPException(
            400,
            "Invalid project timezone. Use an IANA name such as Asia/Riyadh.",
        ) from error
    existing = schedule_baselines_collection.find_one(
        {"project_id": project_id, "source_sha256": digest}
    )
    if existing:
        return {
            "status": "already_imported",
            "baseline": _public_baseline(existing),
        }

    try:
        parsed = (
            parse_xer(raw_bytes, filename=safe_filename)
            if suffix == ".xer"
            else parse_pdf_schedule(raw_bytes, filename=safe_filename)
        )
    except (XerParseError, PdfScheduleParseError) as error:
        raise HTTPException(422, str(error)) from error

    now = datetime.now(timezone.utc)
    baseline_id = f"baseline_{uuid4().hex}"
    version = _next_version(project_id)
    baseline_directory = site_baseline_dir(project_id)
    os.makedirs(baseline_directory, exist_ok=True)
    stored_filename = f"v{version}_{baseline_id}_{safe_filename}"
    stored_path = os.path.join(baseline_directory, stored_filename)
    with open(stored_path, "wb") as baseline_file:
        baseline_file.write(raw_bytes)
    baseline_document = {
        "baseline_id": baseline_id,
        "project_id": project_id,
        "site_name": site_name,
        "floorplan_id": project_context["floorplan_id"],
        "version": version,
        "name": parsed["project"].get("name") or safe_filename,
        "source_type": parsed["source_type"],
        "source_filename": safe_filename,
        "source_url": f"/sites/{project_id}/baseline/{stored_filename}",
        "source_sha256": digest,
        "status": "needs_review",
        "is_active": False,
        "timezone": resolved_timezone,
        "project": parsed["project"],
        "summary": parsed["summary"],
        "warnings": parsed["warnings"],
        "calendars": parsed["calendars"],
        "uploaded_at": now,
        "updated_at": now,
    }
    try:
        schedule_baselines_collection.insert_one(baseline_document)
        activities = [
            {
                **activity,
                "baseline_id": baseline_id,
                "project_id": project_id,
                "site_name": site_name,
                "floorplan_id": project_context["floorplan_id"],
                "created_at": now,
                "updated_at": now,
            }
            for activity in parsed["activities"]
        ]
        relationships = [
            {
                **relationship,
                "baseline_id": baseline_id,
                "project_id": project_id,
                "floorplan_id": project_context["floorplan_id"],
                "created_at": now,
            }
            for relationship in parsed["relationships"]
        ]
        assignments = [
            {
                **assignment,
                "baseline_id": baseline_id,
                "project_id": project_id,
                "floorplan_id": project_context["floorplan_id"],
                "created_at": now,
            }
            for assignment in parsed["assignments"]
        ]
        if activities:
            schedule_activities_collection.insert_many(activities, ordered=False)
        if relationships:
            schedule_relationships_collection.insert_many(relationships, ordered=False)
        if assignments:
            schedule_assignments_collection.insert_many(assignments, ordered=False)
    except Exception:
        schedule_activities_collection.delete_many({"baseline_id": baseline_id})
        schedule_relationships_collection.delete_many({"baseline_id": baseline_id})
        schedule_assignments_collection.delete_many({"baseline_id": baseline_id})
        schedule_baselines_collection.delete_one({"baseline_id": baseline_id})
        try:
            os.remove(stored_path)
        except OSError:
            pass
        raise

    if activate:
        return {
            "status": "active",
            "baseline": activate_schedule_baseline(
                project_ref=project_ref, baseline_id=baseline_id
            )["baseline"],
        }
    return {
        "status": "needs_review",
        "baseline": _public_baseline(baseline_document),
    }


def list_schedule_baselines(project_ref: str) -> dict[str, Any]:
    project_context = resolve_project(project_ref)
    documents = list(
        schedule_baselines_collection.find(
            {"project_id": project_context["project_id"]}
        ).sort("version", -1)
    )
    return {
        "project_id": project_context["project_id"],
        "site_name": project_context["site_name"],
        "baselines": [_public_baseline(document) for document in documents],
    }


def get_schedule_baseline(
    *, baseline_id: str, include_activities: bool = True
) -> dict[str, Any]:
    baseline = schedule_baselines_collection.find_one({"baseline_id": baseline_id})
    if not baseline:
        raise HTTPException(404, "Schedule baseline not found")
    payload = {"baseline": _public_baseline(baseline)}
    if include_activities:
        activities = list(
            schedule_activities_collection.find(
                {"baseline_id": baseline_id}, {"_id": 0}
            ).sort([("start_date", 1), ("activity_id", 1)])
        )
        payload["activities"] = activities
    return payload


def active_schedule_baseline(project_ref: str) -> Optional[dict[str, Any]]:
    project_context = resolve_project(project_ref)
    return schedule_baselines_collection.find_one(
        {"project_id": project_context["project_id"], "is_active": True},
        sort=[("version", -1)],
    )


def activate_schedule_baseline(
    *, project_ref: str, baseline_id: str
) -> dict[str, Any]:
    project_context = resolve_project(project_ref)
    project_id = project_context["project_id"]
    baseline = schedule_baselines_collection.find_one(
        {"baseline_id": baseline_id, "project_id": project_id}
    )
    if not baseline:
        raise HTTPException(404, "Schedule baseline not found for this project")

    now = datetime.now(timezone.utc)
    schedule_baselines_collection.update_many(
        {"project_id": project_id, "is_active": True},
        {"$set": {"is_active": False, "status": "superseded", "updated_at": now}},
    )
    schedule_baselines_collection.update_one(
        {"baseline_id": baseline_id},
        {
            "$set": {
                "is_active": True,
                "status": "active",
                "activated_at": now,
                "updated_at": now,
            }
        },
    )
    floorplans_collection.update_many(
        project_filter(project_ref),
        {
            "$set": {
                "active_schedule_baseline_id": baseline_id,
                "schedule_baseline": {
                    "baseline_id": baseline_id,
                    "version": int(baseline.get("version") or 0),
                    "source_type": baseline.get("source_type"),
                    "source_filename": baseline.get("source_filename"),
                    "status": "active",
                    "summary": baseline.get("summary") or {},
                    "warnings": baseline.get("warnings") or [],
                    "activated_at": now,
                },
                "updated_at": now,
            }
        },
    )
    activated = schedule_baselines_collection.find_one({"baseline_id": baseline_id})
    return {"status": "active", "baseline": _public_baseline(activated or baseline)}


def update_activity_mapping(
    *, baseline_id: str, activity_id: str, updates: dict[str, Any]
) -> dict[str, Any]:
    allowed = {
        "zone",
        "work_category",
        "photo_trackable",
        "planned_quantity",
        "quantity_unit",
        "weight",
        "weight_source",
        "mapping_status",
    }
    payload = {key: value for key, value in updates.items() if key in allowed}
    if not payload:
        raise HTTPException(400, "No supported activity mapping fields were supplied")
    payload["updated_at"] = datetime.now(timezone.utc)
    result = schedule_activities_collection.update_one(
        {"baseline_id": baseline_id, "activity_id": activity_id},
        {"$set": payload},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Schedule activity not found")
    activity = schedule_activities_collection.find_one(
        {"baseline_id": baseline_id, "activity_id": activity_id}, {"_id": 0}
    )
    return {"status": "updated", "activity": activity}


def get_schedule_zones(project_ref: str) -> dict[str, Any]:
    project_context = resolve_project(project_ref)
    project = project_context["document"]
    return {
        "project_id": project_context["project_id"],
        "floorplan_id": project_context["floorplan_id"],
        "bounds": project.get("bounds") or {},
        "zones": project.get("schedule_zones") or [],
    }


def update_schedule_zones(
    *, project_ref: str, zones: list[dict[str, Any]]
) -> dict[str, Any]:
    project_context = resolve_project(project_ref)
    normalized: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for zone in zones:
        name = str(zone.get("name") or "").strip()
        if not name or name.lower() in seen_names:
            raise HTTPException(400, "Each schedule zone must have a unique name")
        points = zone.get("points") or []
        if len(points) < 3:
            raise HTTPException(400, f"{name} must contain at least three polygon points")
        normalized_points = []
        for point in points:
            try:
                x = float(point.get("x"))
                y = float(point.get("y"))
            except (AttributeError, TypeError, ValueError) as error:
                raise HTTPException(400, f"{name} contains an invalid polygon point") from error
            normalized_points.append({"x": x, "y": y})
        seen_names.add(name.lower())
        normalized.append(
            {
                "name": name,
                "points": normalized_points,
                "updated_at": datetime.now(timezone.utc),
            }
        )
    result = floorplans_collection.update_many(
        project_filter(project_ref),
        {
            "$set": {
                "schedule_zones": normalized,
                "schedule_zones_updated_at": datetime.now(timezone.utc),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Project not found")
    return {
        "status": "updated",
        "project_id": project_context["project_id"],
        "zones": normalized,
    }


def delete_baseline_data(baseline_id: str) -> None:
    schedule_activities_collection.delete_many({"baseline_id": baseline_id})
    schedule_relationships_collection.delete_many({"baseline_id": baseline_id})
    schedule_assignments_collection.delete_many({"baseline_id": baseline_id})
    schedule_evidence_collection.delete_many({"baseline_id": baseline_id})
    schedule_progress_snapshots_collection.delete_many({"baseline_id": baseline_id})
    schedule_baselines_collection.delete_one({"baseline_id": baseline_id})
