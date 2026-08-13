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
from core.config import site_baseline_dir, site_storage_roots, site_zone_plan_dir

from .pdf_parser import PdfScheduleParseError, parse_pdf_schedule
from .xer_parser import XerParseError, parse_xer
from .zone_plan_parser import ZonePlanParseError, parse_zone_plan_pdf


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
    site_name = str(
        project.get("site_name") or project.get("display_name") or value
    ).strip()
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


def activate_schedule_baseline(*, project_ref: str, baseline_id: str) -> dict[str, Any]:
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


def _zone_plan_state(
    project: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
]:
    current_zones = list(project.get("schedule_zones") or [])
    current_plan = dict(project.get("schedule_zone_plan") or {})
    explicit_proposed_zones = list(project.get("proposed_schedule_zones") or [])
    explicit_proposed_plan = dict(project.get("proposed_schedule_zone_plan") or {})

    current_confirmed = (
        str(current_plan.get("confirmation_status") or "").strip().lower()
        == "confirmed"
    )
    active_zones = current_zones if current_confirmed else []
    active_plan = current_plan if current_confirmed else {}
    proposed_zones = explicit_proposed_zones or (
        current_zones if current_plan and not current_confirmed else []
    )
    proposed_plan = explicit_proposed_plan or (
        current_plan if current_plan and not current_confirmed else {}
    )
    if (
        proposed_plan
        and not str(proposed_plan.get("confirmation_status") or "").strip()
    ):
        proposed_plan["confirmation_status"] = "needs_review"
    return active_zones, active_plan, proposed_zones, proposed_plan


def _floorplan_asset_available(project: dict[str, Any]) -> bool:
    image_url = str(project.get("imageUrl") or project.get("image_url") or "").strip()
    if not image_url:
        return False
    if image_url.startswith("http://") or image_url.startswith("https://"):
        return True
    normalized = image_url.split("?", 1)[0].strip().lstrip("/")
    if normalized.startswith("sites/"):
        normalized = normalized[len("sites/") :]
    for root in site_storage_roots(
        owner_email=project.get("owner_email"),
        owner_user_id=project.get("owner_user_id"),
    ):
        root_path = os.path.abspath(root)
        candidate = os.path.abspath(os.path.join(root_path, normalized))
        if candidate.startswith(root_path) and os.path.isfile(candidate):
            return True
    return False


def get_schedule_zones(project_ref: str) -> dict[str, Any]:
    project_context = resolve_project(project_ref)
    project = project_context["document"]
    active_zones, active_plan, proposed_zones, proposed_plan = _zone_plan_state(project)
    zones = proposed_zones or active_zones
    zone_plan = proposed_plan or active_plan
    activity_mapping = _schedule_zone_activity_mapping(
        project_id=project_context["project_id"],
        zones=zones,
    )
    zone_plan["activity_mapping"] = activity_mapping
    return {
        "project_id": project_context["project_id"],
        "floorplan_id": project_context["floorplan_id"],
        "bounds": project.get("bounds") or {},
        "floorplan": {
            "id": project_context["floorplan_id"],
            "name": str(project.get("name") or project_context["site_name"]),
            "image_url": str(project.get("imageUrl") or project.get("image_url") or ""),
            "bounds": project.get("bounds") or {},
            "updated_at": project.get("updated_at"),
            "asset_available": _floorplan_asset_available(project),
        },
        "zones": zones,
        "zone_plan": zone_plan,
        "has_proposed_revision": bool(proposed_plan),
        "active_zones": active_zones,
        "active_zone_plan": active_plan,
        "proposed_zones": proposed_zones,
        "proposed_zone_plan": proposed_plan,
        "activity_mapping": activity_mapping,
    }


def _schedule_zone_activity_mapping(
    *, project_id: str, zones: list[dict[str, Any]]
) -> dict[str, Any]:
    mapped_baseline = schedule_baselines_collection.find_one(
        {"project_id": project_id, "is_active": True},
        sort=[("version", -1)],
    ) or schedule_baselines_collection.find_one(
        {"project_id": project_id}, sort=[("version", -1)]
    )
    baseline_id = str((mapped_baseline or {}).get("baseline_id") or "")
    zone_names = [
        str(zone.get("name") or "").strip()
        for zone in zones
        if isinstance(zone, dict) and str(zone.get("name") or "").strip()
    ]
    if not baseline_id:
        return {
            "baseline_id": "",
            "matched_activity_count": 0,
            "total_activity_count": 0,
            "unmapped_activity_count": 0,
            "zone_activity_counts": [
                {"zone": zone_name, "activity_count": 0} for zone_name in zone_names
            ],
        }

    activity_filter = {"baseline_id": baseline_id}
    total_activities = schedule_activities_collection.count_documents(activity_filter)
    zone_activity_counts = [
        {
            "zone": zone_name,
            "activity_count": schedule_activities_collection.count_documents(
                {**activity_filter, "zone": zone_name}
            ),
        }
        for zone_name in zone_names
    ]
    matched_activities = sum(
        int(item["activity_count"]) for item in zone_activity_counts
    )
    return {
        "baseline_id": baseline_id,
        "matched_activity_count": matched_activities,
        "total_activity_count": total_activities,
        "unmapped_activity_count": max(total_activities - matched_activities, 0),
        "zone_activity_counts": zone_activity_counts,
    }


def import_schedule_zone_plan(
    *, project_ref: str, filename: str, raw_bytes: bytes
) -> dict[str, Any]:
    project_context = resolve_project(project_ref)
    project = project_context["document"]
    project_id = project_context["project_id"]
    safe_filename = Path(filename or "zone-plan.pdf").name
    if Path(safe_filename).suffix.lower() != ".pdf":
        raise HTTPException(400, "Zone plan must be a PDF file")

    digest = hashlib.sha256(raw_bytes).hexdigest()
    active_zones, active_plan, proposed_zones, proposed_plan = _zone_plan_state(project)
    matching_plan = next(
        (
            (plan, zones)
            for plan, zones in (
                (proposed_plan, proposed_zones),
                (active_plan, active_zones),
            )
            if plan.get("source_sha256") == digest and zones
        ),
        None,
    )
    if matching_plan is not None:
        existing_plan, existing_zones = matching_plan
        return {
            "status": "already_imported",
            "project_id": project_id,
            "floorplan_id": project_context["floorplan_id"],
            "zone_plan": existing_plan,
            "zones": existing_zones,
            "summary": existing_plan.get("summary") or {},
            "warnings": existing_plan.get("warnings") or [],
            "activity_mapping": existing_plan.get("activity_mapping") or {},
        }

    try:
        parsed = parse_zone_plan_pdf(
            raw_bytes,
            filename=safe_filename,
            floorplan_bounds=project.get("bounds") or {},
        )
    except ZonePlanParseError as error:
        raise HTTPException(422, str(error)) from error

    now = datetime.now(timezone.utc)
    version = (
        max(
            int(active_plan.get("version") or 0),
            int(proposed_plan.get("version") or 0),
        )
        + 1
    )
    zone_plan_id = f"zoneplan_{uuid4().hex}"
    zone_directory = site_zone_plan_dir(
        project_id,
        owner_email=project.get("owner_email"),
        owner_user_id=project.get("owner_user_id"),
    )
    os.makedirs(zone_directory, exist_ok=True)
    stored_filename = f"v{version}_{zone_plan_id}_{safe_filename}"
    stored_path = os.path.join(zone_directory, stored_filename)
    with open(stored_path, "wb") as zone_file:
        zone_file.write(raw_bytes)

    zones = []
    for zone in parsed.get("zones") or []:
        if not isinstance(zone, dict):
            continue
        points = [dict(point) for point in zone.get("points") or []]
        zones.append(
            {
                **zone,
                "points": points,
                "source_points": [dict(point) for point in points],
                "updated_at": now,
            }
        )
    activity_mapping = _schedule_zone_activity_mapping(
        project_id=project_id,
        zones=zones,
    )
    zone_plan = {
        "zone_plan_id": zone_plan_id,
        "version": version,
        "source_filename": safe_filename,
        "source_url": f"/sites/{project_id}/zone-plans/{stored_filename}",
        "source_sha256": digest,
        "source_type": parsed.get("source_type") or "zone_plan_pdf",
        "page_count": parsed.get("page_count") or 1,
        "page_index": parsed.get("page_index") or 0,
        "page_width": parsed.get("page_width"),
        "page_height": parsed.get("page_height"),
        "orientation": parsed.get("orientation") or "",
        "summary": parsed.get("summary") or {},
        "warnings": parsed.get("warnings") or [],
        "activity_mapping": activity_mapping,
        "confirmation_status": "needs_review",
        "confirmed_at": None,
        "confirmed_by_user_id": "",
        "confirmed_by_email": "",
        "confirmation_note": "",
        "uploaded_at": now,
    }

    try:
        result = floorplans_collection.update_many(
            project_filter(project_ref),
            {
                "$set": {
                    "proposed_schedule_zones": zones,
                    "proposed_schedule_zone_plan": zone_plan,
                    "proposed_schedule_zones_updated_at": now,
                    "updated_at": now,
                }
            },
        )
        if result.matched_count == 0:
            raise HTTPException(404, "Project not found")
    except Exception:
        try:
            os.remove(stored_path)
        except OSError:
            pass
        raise

    return {
        "status": "imported",
        "project_id": project_id,
        "floorplan_id": project_context["floorplan_id"],
        "zone_plan": zone_plan,
        "zones": zones,
        "summary": zone_plan["summary"],
        "warnings": zone_plan["warnings"],
        "activity_mapping": activity_mapping,
    }


def confirm_schedule_zone_plan(
    *,
    project_ref: str,
    reviewer_user_id: str,
    reviewer_email: str,
    expected_zone_plan_id: str = "",
    expected_version: Optional[int] = None,
    floorplan_loaded: bool = False,
    note: str = "",
) -> dict[str, Any]:
    project_context = resolve_project(project_ref)
    project = project_context["document"]
    active_zones, active_plan, proposed_zones, proposed_plan = _zone_plan_state(project)
    zones = proposed_zones or active_zones
    zone_plan = dict(proposed_plan or active_plan)
    if not zone_plan or not zones:
        raise HTTPException(409, "Upload and review a Zone Plan PDF before confirming")
    current_zone_plan_id = str(zone_plan.get("zone_plan_id") or "").strip()
    current_version = int(zone_plan.get("version") or 0)
    if (
        expected_zone_plan_id and expected_zone_plan_id.strip() != current_zone_plan_id
    ) or (expected_version is not None and expected_version != current_version):
        raise HTTPException(
            409,
            "The Zone Plan PDF changed during review. Refresh and verify the current overlay.",
        )
    bounds = project.get("bounds") or {}
    try:
        floorplan_width = float(bounds.get("width") or 0)
        floorplan_height = float(bounds.get("height") or 0)
    except (TypeError, ValueError):
        floorplan_width = 0
        floorplan_height = 0
    floorplan_image = str(project.get("imageUrl") or project.get("image_url") or "")
    if not floorplan_image.strip() or floorplan_width <= 0 or floorplan_height <= 0:
        raise HTTPException(
            409,
            "The project floorplan preview is unavailable. Update it before confirming zones.",
        )
    if not _floorplan_asset_available(project):
        raise HTTPException(
            409,
            "The project floorplan file is unavailable in this backend storage. Re-upload it before confirming zones.",
        )
    if not floorplan_loaded:
        raise HTTPException(
            409,
            "Load and visually review the project floorplan before confirming zones.",
        )

    now = datetime.now(timezone.utc)
    confirmation = {
        "confirmation_status": "confirmed",
        "confirmed_at": now,
        "confirmed_by_user_id": str(reviewer_user_id or "").strip(),
        "confirmed_by_email": str(reviewer_email or "").strip(),
        "confirmation_note": str(note or "").strip(),
    }
    zone_plan.update(confirmation)
    has_explicit_proposal = bool(project.get("proposed_schedule_zone_plan"))
    plan_field = (
        "proposed_schedule_zone_plan" if has_explicit_proposal else "schedule_zone_plan"
    )
    update_document: dict[str, Any] = {
        "$set": {
            "schedule_zones": zones,
            "schedule_zone_plan": zone_plan,
            "schedule_zones_confirmed_at": now,
            "updated_at": now,
        }
    }
    if has_explicit_proposal:
        update_document["$unset"] = {
            "proposed_schedule_zones": "",
            "proposed_schedule_zone_plan": "",
            "proposed_schedule_zones_updated_at": "",
        }
    result = floorplans_collection.update_many(
        {
            "$and": [
                project_filter(project_ref),
                {f"{plan_field}.zone_plan_id": current_zone_plan_id},
                {f"{plan_field}.version": current_version},
            ]
        },
        update_document,
    )
    if result.matched_count == 0:
        raise HTTPException(
            409,
            "The Zone Plan PDF changed during review. Refresh and verify the current overlay.",
        )

    activity_mapping = _schedule_zone_activity_mapping(
        project_id=project_context["project_id"],
        zones=zones,
    )
    zone_plan["activity_mapping"] = activity_mapping
    return {
        "status": "confirmed",
        "project_id": project_context["project_id"],
        "floorplan_id": project_context["floorplan_id"],
        "zone_plan": zone_plan,
        "zones": zones,
        "activity_mapping": activity_mapping,
    }


def update_schedule_zones(
    *, project_ref: str, zones: list[dict[str, Any]]
) -> dict[str, Any]:
    project_context = resolve_project(project_ref)
    project = project_context["document"]
    active_zones, active_plan, proposed_zones, proposed_plan = _zone_plan_state(project)
    current_plan = dict(proposed_plan or active_plan)
    existing_by_name = {
        str(zone.get("name") or "").strip().lower(): zone
        for zone in (proposed_zones or active_zones)
        if isinstance(zone, dict)
    }
    normalized: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for zone in zones:
        name = str(zone.get("name") or "").strip()
        if not name or name.lower() in seen_names:
            raise HTTPException(400, "Each schedule zone must have a unique name")
        points = zone.get("points") or []
        if len(points) < 3:
            raise HTTPException(
                400, f"{name} must contain at least three polygon points"
            )
        normalized_points = []
        for point in points:
            try:
                x = float(point.get("x"))
                y = float(point.get("y"))
            except (AttributeError, TypeError, ValueError) as error:
                raise HTTPException(
                    400, f"{name} contains an invalid polygon point"
                ) from error
            normalized_points.append({"x": x, "y": y})
        seen_names.add(name.lower())
        existing = existing_by_name.get(name.lower()) or {}
        normalized.append(
            {
                **{
                    key: value
                    for key, value in existing.items()
                    if key not in {"points", "updated_at"}
                },
                "name": name,
                "points": normalized_points,
                "updated_at": datetime.now(timezone.utc),
            }
        )
    now = datetime.now(timezone.utc)
    next_plan_id = f"zoneplan_{uuid4().hex}"
    next_version = int(current_plan.get("version") or 0) + 1
    next_plan = {
        **current_plan,
        "zone_plan_id": next_plan_id,
        "version": next_version,
        "parent_zone_plan_id": str(current_plan.get("zone_plan_id") or ""),
        "source_type": "manual_polygon_edit",
        "confirmation_status": "needs_review",
        "confirmed_at": None,
        "confirmed_by_user_id": "",
        "confirmed_by_email": "",
        "confirmation_note": "",
        "updated_at": now,
    }
    result = floorplans_collection.update_many(
        project_filter(project_ref),
        {
            "$set": {
                "proposed_schedule_zones": normalized,
                "proposed_schedule_zones_updated_at": now,
                "proposed_schedule_zone_plan": next_plan,
                "updated_at": now,
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Project not found")
    return {
        "status": "updated",
        "project_id": project_context["project_id"],
        "zone_plan_id": next_plan_id,
        "version": next_version,
        "confirmation_status": "needs_review",
        "zone_plan": next_plan,
        "zones": normalized,
    }


def _solve_affine_coefficients(
    source_points: list[dict[str, float]], target_values: list[float]
) -> tuple[float, float, float]:
    matrix = [
        [point["x"], point["y"], 1.0, target_values[index]]
        for index, point in enumerate(source_points)
    ]
    for pivot in range(3):
        pivot_row = max(range(pivot, 3), key=lambda row: abs(matrix[row][pivot]))
        if abs(matrix[pivot_row][pivot]) < 1e-9:
            raise HTTPException(
                422, "Choose three control points that are not on one straight line"
            )
        matrix[pivot], matrix[pivot_row] = matrix[pivot_row], matrix[pivot]
        divisor = matrix[pivot][pivot]
        matrix[pivot] = [value / divisor for value in matrix[pivot]]
        for row in range(3):
            if row == pivot:
                continue
            factor = matrix[row][pivot]
            matrix[row] = [
                matrix[row][column] - factor * matrix[pivot][column]
                for column in range(4)
            ]
    return matrix[0][3], matrix[1][3], matrix[2][3]


def align_schedule_zones(
    *,
    project_ref: str,
    expected_zone_plan_id: str,
    expected_version: Optional[int],
    source_points: list[dict[str, float]],
    floorplan_points: list[dict[str, float]],
) -> dict[str, Any]:
    if len(source_points) != 3 or len(floorplan_points) != 3:
        raise HTTPException(
            422, "Exactly three source and floorplan points are required"
        )
    project_context = resolve_project(project_ref)
    project = project_context["document"]
    active_zones, active_plan, proposed_zones, proposed_plan = _zone_plan_state(project)
    zones = proposed_zones or active_zones
    current_plan = dict(proposed_plan or active_plan)
    if not zones or not current_plan:
        raise HTTPException(409, "Upload a Zone Plan PDF before aligning zones")
    current_plan_id = str(current_plan.get("zone_plan_id") or "").strip()
    current_version = int(current_plan.get("version") or 0)
    if (expected_zone_plan_id and expected_zone_plan_id.strip() != current_plan_id) or (
        expected_version is not None and expected_version != current_version
    ):
        raise HTTPException(
            409,
            "The Zone Plan PDF changed during alignment. Refresh and try again.",
        )

    bounds = project.get("bounds") or {}
    try:
        width = float(bounds.get("width") or 0)
        height = float(bounds.get("height") or 0)
    except (TypeError, ValueError) as error:
        raise HTTPException(
            422, "The project floorplan has invalid dimensions"
        ) from error
    if width <= 0 or height <= 0:
        raise HTTPException(422, "The project floorplan has invalid dimensions")

    normalized_source = []
    normalized_target = []
    for source, target in zip(source_points, floorplan_points):
        try:
            source_x = float(source.get("x"))
            source_y = float(source.get("y"))
            target_x = float(target.get("x"))
            target_y = float(target.get("y"))
        except (AttributeError, TypeError, ValueError) as error:
            raise HTTPException(
                422, "Control points contain invalid coordinates"
            ) from error
        if not 0 <= source_x <= 1 or not 0 <= source_y <= 1:
            raise HTTPException(422, "Source PDF control points must be normalized")
        if not 0 <= target_x <= width or not 0 <= target_y <= height:
            raise HTTPException(422, "Floorplan control points are outside its bounds")
        normalized_source.append({"x": source_x * width, "y": source_y * height})
        normalized_target.append({"x": target_x, "y": target_y})

    x_coefficients = _solve_affine_coefficients(
        normalized_source, [point["x"] for point in normalized_target]
    )
    y_coefficients = _solve_affine_coefficients(
        normalized_source, [point["y"] for point in normalized_target]
    )
    matrix = {
        "a": x_coefficients[0],
        "b": x_coefficients[1],
        "c": x_coefficients[2],
        "d": y_coefficients[0],
        "e": y_coefficients[1],
        "f": y_coefficients[2],
    }
    aligned_zones: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for zone in zones:
        base_points = zone.get("source_points") or zone.get("points") or []
        aligned_points = []
        for point in base_points:
            x = float(point.get("x") or 0)
            y = float(point.get("y") or 0)
            aligned_points.append(
                {
                    "x": max(
                        0.0, min(width, matrix["a"] * x + matrix["b"] * y + matrix["c"])
                    ),
                    "y": max(
                        0.0,
                        min(height, matrix["d"] * x + matrix["e"] * y + matrix["f"]),
                    ),
                }
            )
        aligned_zones.append({**zone, "points": aligned_points, "updated_at": now})

    next_plan_id = f"zoneplan_{uuid4().hex}"
    next_plan = {
        **current_plan,
        "zone_plan_id": next_plan_id,
        "parent_zone_plan_id": current_plan_id,
        "confirmation_status": "needs_review",
        "confirmed_at": None,
        "confirmed_by_user_id": "",
        "confirmed_by_email": "",
        "confirmation_note": "",
        "alignment": {
            "method": "three_point_affine",
            "source_points": source_points,
            "floorplan_points": floorplan_points,
            "matrix": matrix,
            "aligned_at": now,
        },
        "updated_at": now,
    }
    result = floorplans_collection.update_many(
        project_filter(project_ref),
        {
            "$set": {
                "proposed_schedule_zones": aligned_zones,
                "proposed_schedule_zone_plan": next_plan,
                "proposed_schedule_zones_updated_at": now,
                "updated_at": now,
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Project not found")
    return {
        "status": "aligned",
        "project_id": project_context["project_id"],
        "zone_plan": next_plan,
        "zones": aligned_zones,
        "alignment": next_plan["alignment"],
    }


def delete_baseline_data(baseline_id: str) -> None:
    schedule_activities_collection.delete_many({"baseline_id": baseline_id})
    schedule_relationships_collection.delete_many({"baseline_id": baseline_id})
    schedule_assignments_collection.delete_many({"baseline_id": baseline_id})
    schedule_evidence_collection.delete_many({"baseline_id": baseline_id})
    schedule_progress_snapshots_collection.delete_many({"baseline_id": baseline_id})
    schedule_baselines_collection.delete_one({"baseline_id": baseline_id})
