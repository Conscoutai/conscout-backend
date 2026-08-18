from __future__ import annotations

import os
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from uuid import uuid4

import requests
from fastapi import HTTPException
from fpdf import FPDF
from pymongo.errors import DuplicateKeyError

from core.auth_context import AuthenticatedUser
from core.config import WEATHER_API_KEY, WEATHER_API_URL, site_dir
from core.database import (
    raw_safety_analysis_jobs_collection,
    raw_safety_records_collection,
    raw_tours_collection,
    safety_analysis_jobs_collection,
    safety_audit_events_collection,
    safety_records_collection,
)
from services.progress.work_schedule.baseline_service import resolve_project
from services.project_setup.safety_notification_service import (
    sync_safety_record_notification,
)


ANALYSIS_VERSION = "phase1-existing-worker-v1"
WEATHER_CACHE_MINUTES = 15
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".mp4", ".mov"}

DEFAULT_SAFETY_CONFIG: dict[str, Any] = {
    "wind_warning_kph": 30.0,
    "wind_stop_kph": 45.0,
    "heat_warning_c": 38.0,
    "heat_stop_c": 45.0,
    "rain_warning_mm_h": 2.5,
    "rain_stop_mm_h": 10.0,
    "weather_provider": "open_meteo",
    "required_ppe": ["helmet", "safety_vest"],
    "timezone": "UTC",
    "latitude": None,
    "longitude": None,
}

RECORD_PREFIXES = {
    "safety_config": "config",
    "workforce_plan": "plan",
    "workforce_observation": "observation",
    "safety_finding": "finding",
    "weather_observation": "weather",
    "safety_zone": "zone",
    "permit": "permit",
    "check_template": "template",
    "check_run": "check",
    "hazard": "hazard",
    "daily_report": "report",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def today_iso() -> str:
    return date.today().isoformat()


def public_document(document: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not document:
        return {}
    result: dict[str, Any] = {}
    for key, value in document.items():
        if key == "_id":
            continue
        if isinstance(value, datetime):
            result[key] = value.isoformat()
        elif isinstance(value, list):
            result[key] = [
                public_document(item) if isinstance(item, dict) else item
                for item in value
            ]
        elif isinstance(value, dict):
            result[key] = public_document(value)
        else:
            result[key] = value
    return result


def project_context(project_ref: str) -> dict[str, Any]:
    """Resolve and authorize a project through the request's scoped collection."""
    return resolve_project(project_ref)


def _identity(context: dict[str, Any]) -> dict[str, str]:
    return {
        "project_id": context["project_id"],
        "site_name": context["site_name"],
        "floorplan_id": context["floorplan_id"],
    }


def _record_id(record_type: str) -> str:
    prefix = RECORD_PREFIXES.get(record_type, "safety")
    return f"{prefix}_{uuid4().hex}"


def _actor(user: AuthenticatedUser) -> dict[str, str]:
    return {
        "user_id": user.user_id,
        "email": user.email,
        "name": user.name,
    }


def write_audit_event(
    *,
    context: dict[str, Any],
    user: AuthenticatedUser,
    action: str,
    entity_type: str,
    entity_id: str,
    details: Optional[dict[str, Any]] = None,
) -> None:
    safety_audit_events_collection.insert_one(
        {
            "event_id": f"audit_{uuid4().hex}",
            **_identity(context),
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor": _actor(user),
            "details": details or {},
            "created_at": utc_now(),
        }
    )


def list_audit_events(project_ref: str, *, limit: int = 100) -> list[dict[str, Any]]:
    context = project_context(project_ref)
    documents = safety_audit_events_collection.find(
        {"project_id": context["project_id"]}
    ).sort("created_at", -1).limit(max(1, min(limit, 500)))
    return [public_document(document) for document in documents]


def get_config(project_ref: str) -> dict[str, Any]:
    context = project_context(project_ref)
    document = safety_records_collection.find_one(
        {"project_id": context["project_id"], "record_type": "safety_config"}
    )
    config = {**DEFAULT_SAFETY_CONFIG, **((document or {}).get("config") or {})}
    return {
        **_identity(context),
        "record_id": str((document or {}).get("record_id") or ""),
        "config": config,
        "configured": bool(document),
        "updated_at": public_document(document).get("updated_at") if document else None,
    }


def update_config(
    project_ref: str, *, payload: dict[str, Any], user: AuthenticatedUser
) -> dict[str, Any]:
    context = project_context(project_ref)
    now = utc_now()
    existing = safety_records_collection.find_one(
        {"project_id": context["project_id"], "record_type": "safety_config"}
    )
    record_id = str((existing or {}).get("record_id") or _record_id("safety_config"))
    config = {**DEFAULT_SAFETY_CONFIG, **((existing or {}).get("config") or {})}
    config.update({key: value for key, value in payload.items() if value is not None})
    for warning_key, stop_key, label in (
        ("wind_warning_kph", "wind_stop_kph", "Wind"),
        ("heat_warning_c", "heat_stop_c", "Heat"),
        ("rain_warning_mm_h", "rain_stop_mm_h", "Rain"),
    ):
        if float(config[warning_key]) > float(config[stop_key]):
            raise HTTPException(
                422,
                f"{label} warning threshold cannot exceed its stop threshold",
            )
    safety_records_collection.update_one(
        {"record_id": record_id},
        {
            "$set": {
                **_identity(context),
                "record_type": "safety_config",
                "config": config,
                "updated_at": now,
                "updated_by": _actor(user),
            },
            "$setOnInsert": {
                "record_id": record_id,
                "record_date": today_iso(),
                "created_at": now,
                "created_by": _actor(user),
            },
        },
        upsert=True,
    )
    write_audit_event(
        context=context,
        user=user,
        action="updated",
        entity_type="safety_config",
        entity_id=record_id,
    )
    return get_config(project_ref)


def list_records(
    project_ref: str,
    record_type: str,
    *,
    status: str = "",
    record_date: str = "",
    limit: int = 250,
) -> list[dict[str, Any]]:
    context = project_context(project_ref)
    query: dict[str, Any] = {
        "project_id": context["project_id"],
        "record_type": record_type,
    }
    if status:
        query["status"] = status
    if record_date:
        query["record_date"] = record_date
    documents = safety_records_collection.find(query).sort(
        [("record_date", -1), ("updated_at", -1), ("created_at", -1)]
    ).limit(max(1, min(limit, 1000)))
    return [public_document(document) for document in documents]


def create_record(
    project_ref: str,
    *,
    record_type: str,
    payload: dict[str, Any],
    user: AuthenticatedUser,
) -> dict[str, Any]:
    context = project_context(project_ref)
    now = utc_now()
    record_id = _record_id(record_type)
    document = {
        **_identity(context),
        **payload,
        "record_id": record_id,
        "record_type": record_type,
        "record_date": str(payload.get("record_date") or today_iso()),
        "status": str(payload.get("status") or "open"),
        "created_at": now,
        "updated_at": now,
        "created_by": _actor(user),
        "updated_by": _actor(user),
    }
    safety_records_collection.insert_one(document)
    write_audit_event(
        context=context,
        user=user,
        action="created",
        entity_type=record_type,
        entity_id=record_id,
    )
    if record_type in {"hazard", "safety_finding"}:
        try:
            sync_safety_record_notification(
                project_id=context["site_name"],
                record=document,
                current_user=user,
            )
        except Exception:
            pass
    return public_document(document)


def update_record(
    project_ref: str,
    *,
    record_type: str,
    record_id: str,
    payload: dict[str, Any],
    user: AuthenticatedUser,
) -> dict[str, Any]:
    context = project_context(project_ref)
    query = {
        "project_id": context["project_id"],
        "record_type": record_type,
        "record_id": record_id,
    }
    existing = safety_records_collection.find_one(query)
    if not existing:
        raise HTTPException(404, f"{record_type.replace('_', ' ').title()} not found")
    protected = {
        "_id",
        "record_id",
        "record_type",
        "project_id",
        "site_name",
        "floorplan_id",
        "owner_user_id",
        "owner_email",
        "created_at",
        "created_by",
    }
    changes = {key: value for key, value in payload.items() if key not in protected}
    changes.update({"updated_at": utc_now(), "updated_by": _actor(user)})
    safety_records_collection.update_one(query, {"$set": changes})
    write_audit_event(
        context=context,
        user=user,
        action="updated",
        entity_type=record_type,
        entity_id=record_id,
        details={"fields": sorted(changes.keys())},
    )
    updated = safety_records_collection.find_one(query)
    if record_type in {"hazard", "safety_finding"}:
        try:
            sync_safety_record_notification(
                project_id=context["site_name"],
                record=updated or {},
                current_user=user,
            )
        except Exception:
            pass
    return public_document(updated)


def delete_record(
    project_ref: str,
    *,
    record_type: str,
    record_id: str,
    user: AuthenticatedUser,
) -> dict[str, Any]:
    context = project_context(project_ref)
    result = safety_records_collection.delete_one(
        {
            "project_id": context["project_id"],
            "record_type": record_type,
            "record_id": record_id,
        }
    )
    if not result.deleted_count:
        raise HTTPException(404, f"{record_type.replace('_', ' ').title()} not found")
    write_audit_event(
        context=context,
        user=user,
        action="deleted",
        entity_type=record_type,
        entity_id=record_id,
    )
    return {"status": "deleted", "record_id": record_id}


def save_record_attachment(
    project_ref: str,
    *,
    record_type: str,
    record_id: str,
    filename: str,
    content_type: str,
    content: bytes,
    user: AuthenticatedUser,
) -> dict[str, Any]:
    context = project_context(project_ref)
    record = safety_records_collection.find_one(
        {
            "project_id": context["project_id"],
            "record_type": record_type,
            "record_id": record_id,
        }
    )
    if not record:
        raise HTTPException(404, f"{record_type.replace('_', ' ').title()} not found")
    if not content:
        raise HTTPException(400, "Attachment is empty")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, "Attachment exceeds the 20 MB limit")
    safe_name = Path(filename or "attachment").name
    extension = Path(safe_name).suffix.lower()
    if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise HTTPException(415, "Unsupported safety attachment type")
    attachment_id = f"attachment_{uuid4().hex}"
    stored_name = f"{attachment_id}{extension}"
    project = context.get("document") or {}
    directory = os.path.join(
        site_dir(
            context["project_id"],
            owner_email=project.get("owner_email"),
            owner_user_id=project.get("owner_user_id"),
        ),
        "safety",
    )
    os.makedirs(directory, exist_ok=True)
    stored_path = os.path.join(directory, stored_name)
    with open(stored_path, "wb") as output:
        output.write(content)
    attachment = {
        "attachment_id": attachment_id,
        "filename": safe_name,
        "content_type": content_type or "application/octet-stream",
        "size_bytes": len(content),
        "url": f"/sites/{context['project_id']}/safety/{stored_name}",
        "created_at": utc_now().isoformat(),
        "created_by": _actor(user),
    }
    safety_records_collection.update_one(
        {
            "project_id": context["project_id"],
            "record_type": record_type,
            "record_id": record_id,
        },
        {
            "$push": {"attachments": attachment},
            "$set": {"updated_at": utc_now(), "updated_by": _actor(user)},
        },
    )
    write_audit_event(
        context=context,
        user=user,
        action="attachment_added",
        entity_type=record_type,
        entity_id=record_id,
        details={"attachment_id": attachment_id, "filename": safe_name},
    )
    return attachment


def _float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _weather_coordinates(
    context: dict[str, Any], config: dict[str, Any]
) -> tuple[Optional[float], Optional[float]]:
    latitude = _float(config.get("latitude"))
    longitude = _float(config.get("longitude"))
    if latitude is not None and longitude is not None:
        return latitude, longitude
    project = context.get("document") or {}
    origin = project.get("origin") if isinstance(project.get("origin"), dict) else {}
    latitude = _float(origin.get("latitude"))
    longitude = _float(origin.get("longitude"))
    if latitude is not None and longitude is not None:
        return latitude, longitude
    points = project.get("calibration_points") or []
    if points and isinstance(points[0], dict):
        return _float(points[0].get("latitude")), _float(points[0].get("longitude"))
    return None, None


def evaluate_weather_risk(
    *, wind_kph: Optional[float], heat_c: Optional[float], rain_mm_h: Optional[float], config: dict[str, Any]
) -> dict[str, Any]:
    if wind_kph is None and heat_c is None and rain_mm_h is None:
        return {"work_state": "unknown", "reasons": ["Weather data unavailable"]}
    stop_reasons: list[str] = []
    warning_reasons: list[str] = []
    metrics = (
        (wind_kph, "wind", "wind_stop_kph", "wind_warning_kph", "km/h"),
        (heat_c, "heat", "heat_stop_c", "heat_warning_c", "°C"),
        (rain_mm_h, "rain", "rain_stop_mm_h", "rain_warning_mm_h", "mm/h"),
    )
    for value, label, stop_key, warning_key, unit in metrics:
        if value is None:
            continue
        stop = float(config.get(stop_key) or DEFAULT_SAFETY_CONFIG[stop_key])
        warning = float(config.get(warning_key) or DEFAULT_SAFETY_CONFIG[warning_key])
        if value >= stop:
            stop_reasons.append(f"{label.title()} {value:g} {unit} reached stop threshold")
        elif value >= warning:
            warning_reasons.append(f"{label.title()} {value:g} {unit} reached warning threshold")
    if stop_reasons:
        return {"work_state": "stop_work", "reasons": stop_reasons}
    if warning_reasons:
        return {"work_state": "caution", "reasons": warning_reasons}
    return {"work_state": "safe", "reasons": []}


def _latest_weather(context: dict[str, Any]) -> Optional[dict[str, Any]]:
    return safety_records_collection.find_one(
        {"project_id": context["project_id"], "record_type": "weather_observation"},
        sort=[("observed_at", -1), ("created_at", -1)],
    )


def get_weather(project_ref: str, *, refresh: bool = False) -> dict[str, Any]:
    context = project_context(project_ref)
    config_payload = get_config(project_ref)
    config = config_payload["config"]
    latest = _latest_weather(context)
    latest_created = as_utc_datetime((latest or {}).get("created_at"))
    if (
        not refresh
        and latest_created is not None
        and latest_created >= utc_now() - timedelta(minutes=WEATHER_CACHE_MINUTES)
    ):
        return public_document(latest)

    latitude, longitude = _weather_coordinates(context, config)
    if latitude is None or longitude is None:
        if latest:
            return public_document(latest)
        return {
            **_identity(context),
            "record_type": "weather_observation",
            "provider": "unavailable",
            "work_state": "unknown",
            "reasons": ["Project GPS coordinates are not configured"],
            "observed_at": None,
        }

    try:
        weather_params = {
            "latitude": latitude,
            "longitude": longitude,
            "current": (
                "apparent_temperature,precipitation,wind_speed_10m,"
                "wind_gusts_10m,weather_code"
            ),
            "wind_speed_unit": "kmh",
            "timezone": "auto",
        }
        if WEATHER_API_KEY:
            weather_params["apikey"] = WEATHER_API_KEY
        response = requests.get(
            WEATHER_API_URL,
            params=weather_params,
            timeout=8,
        )
        response.raise_for_status()
        body = response.json()
        current = body.get("current") if isinstance(body, dict) else None
        if not isinstance(current, dict):
            raise ValueError("Weather provider returned no current conditions")
        wind_speed = _float(current.get("wind_speed_10m"))
        wind_gust = _float(current.get("wind_gusts_10m"))
        wind = (
            max(value for value in (wind_speed, wind_gust) if value is not None)
            if any(value is not None for value in (wind_speed, wind_gust))
            else None
        )
        heat = _float(current.get("apparent_temperature"))
        rain = _float(current.get("precipitation"))
        risk = evaluate_weather_risk(
            wind_kph=wind, heat_c=heat, rain_mm_h=rain, config=config
        )
        document = {
            **_identity(context),
            "record_id": _record_id("weather_observation"),
            "record_type": "weather_observation",
            "record_date": today_iso(),
            "status": risk["work_state"],
            "provider": "open_meteo",
            "attribution": "Weather data by Open-Meteo",
            "license_url": "https://open-meteo.com/en/license",
            "latitude": latitude,
            "longitude": longitude,
            "wind_kph": wind,
            "wind_speed_kph": wind_speed,
            "wind_gust_kph": wind_gust,
            "apparent_temperature_c": heat,
            "precipitation_mm_h": rain,
            "weather_code": current.get("weather_code"),
            "work_state": risk["work_state"],
            "reasons": risk["reasons"],
            "observed_at": str(current.get("time") or utc_now().isoformat()),
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        safety_records_collection.insert_one(document)
        return public_document(document)
    except Exception as error:
        if latest:
            cached = public_document(latest)
            cached["stale"] = True
            cached["refresh_error"] = str(error)
            return cached
        return {
            **_identity(context),
            "record_type": "weather_observation",
            "provider": "open_meteo",
            "attribution": "Weather data by Open-Meteo",
            "license_url": "https://open-meteo.com/en/license",
            "work_state": "unknown",
            "reasons": ["Live weather is temporarily unavailable"],
            "observed_at": None,
            "refresh_error": str(error),
        }


def create_manual_weather(
    project_ref: str, *, payload: dict[str, Any], user: AuthenticatedUser
) -> dict[str, Any]:
    config = get_config(project_ref)["config"]
    wind = _float(payload.get("wind_kph"))
    heat = _float(payload.get("apparent_temperature_c"))
    rain = _float(payload.get("precipitation_mm_h"))
    risk = evaluate_weather_risk(
        wind_kph=wind, heat_c=heat, rain_mm_h=rain, config=config
    )
    return create_record(
        project_ref,
        record_type="weather_observation",
        payload={
            **payload,
            "provider": "manual",
            "wind_kph": wind,
            "apparent_temperature_c": heat,
            "precipitation_mm_h": rain,
            "work_state": risk["work_state"],
            "reasons": risk["reasons"],
            "status": risk["work_state"],
            "observed_at": str(payload.get("observed_at") or utc_now().isoformat()),
        },
        user=user,
    )


def preliminary_worker_count(nodes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    counts: list[int] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        value = node.get("worker_count")
        try:
            parsed = max(0, int(value))
        except (TypeError, ValueError):
            continue
        counts.append(parsed)
    if not counts:
        return {"observed_workers": None, "sample_count": 0, "method": "unavailable"}
    return {
        "observed_workers": max(counts),
        "median_workers_per_frame": statistics.median(counts),
        "sample_count": len(counts),
        "method": "max_existing_worker_count_per_tour",
    }


def point_in_polygon(point: dict[str, Any], polygon: Iterable[dict[str, Any]]) -> bool:
    """Return True for a point inside or on the boundary of a floor-plan polygon."""
    x = _float(point.get("x"))
    y = _float(point.get("y"))
    vertices = [
        (_float(vertex.get("x")), _float(vertex.get("y")))
        for vertex in polygon
        if isinstance(vertex, dict)
    ]
    vertices = [(vx, vy) for vx, vy in vertices if vx is not None and vy is not None]
    if x is None or y is None or len(vertices) < 3:
        return False
    inside = False
    previous_x, previous_y = vertices[-1]
    for current_x, current_y in vertices:
        cross = (x - previous_x) * (current_y - previous_y) - (y - previous_y) * (
            current_x - previous_x
        )
        if abs(cross) <= 1e-9 and min(previous_x, current_x) <= x <= max(
            previous_x, current_x
        ) and min(previous_y, current_y) <= y <= max(previous_y, current_y):
            return True
        intersects = (current_y > y) != (previous_y > y) and x < (
            (previous_x - current_x) * (y - current_y)
            / ((previous_y - current_y) or 1e-12)
            + current_x
        )
        if intersects:
            inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


def evaluate_geofence(
    project_ref: str,
    *,
    point: dict[str, Any],
    entity_type: str,
    entity_id: str,
    create_findings: bool,
    user: AuthenticatedUser,
) -> dict[str, Any]:
    context = project_context(project_ref)
    zones = list_records(project_ref, "safety_zone", status="active", limit=1000)
    breaches = [
        zone
        for zone in zones
        if point_in_polygon(point, zone.get("polygon") or [])
    ]
    finding_ids: list[str] = []
    if create_findings:
        for zone in breaches:
            finding = create_record(
                project_ref,
                record_type="safety_finding",
                payload={
                    "record_date": today_iso(),
                    "title": f"{entity_type.replace('_', ' ').title()} entered exclusion zone",
                    "description": f"{entity_id or 'Tracked entity'} entered {zone.get('title') or 'an active exclusion zone'}.",
                    "severity": zone.get("severity") or "high",
                    "status": "open",
                    "source": "spatial_geofence",
                    "point": point,
                    "zone_id": zone.get("record_id"),
                    "tracked_entity_type": entity_type,
                    "tracked_entity_id": entity_id,
                    "requires_review": True,
                },
                user=user,
            )
            finding_ids.append(str(finding.get("record_id") or ""))
    write_audit_event(
        context=context,
        user=user,
        action="geofence_evaluated",
        entity_type=entity_type or "tracked_entity",
        entity_id=entity_id,
        details={
            "point": point,
            "breach_zone_ids": [zone.get("record_id") for zone in breaches],
            "finding_ids": finding_ids,
        },
    )
    return {
        "project_id": context["project_id"],
        "point": point,
        "breached": bool(breaches),
        "breaches": breaches,
        "finding_ids": finding_ids,
        "evaluated_at": utc_now().isoformat(),
    }


def create_analysis_job(
    project_ref: str, *, tour_id: str, user: AuthenticatedUser
) -> tuple[dict[str, Any], bool]:
    context = project_context(project_ref)
    normalized_tour_id = str(tour_id or "").strip()
    if not normalized_tour_id:
        raise HTTPException(400, "tour_id is required")
    tour = raw_tours_collection.find_one(
        {
            "tour_id": normalized_tour_id,
            "$or": [
                {"site_name": context["site_name"]},
                {"site": context["site_name"]},
                {"project_id": context["project_id"]},
                {"floorplan_id": context["floorplan_id"]},
            ],
        },
        {"_id": 1},
    )
    if not tour:
        raise HTTPException(404, "Tour not found in this project")
    existing = safety_analysis_jobs_collection.find_one(
        {
            "project_id": context["project_id"],
            "tour_id": normalized_tour_id,
            "analysis_version": ANALYSIS_VERSION,
        }
    )
    if existing and str(existing.get("status") or "") not in {"failed", "cancelled"}:
        return public_document(existing), False
    now = utc_now()
    job_id = str((existing or {}).get("job_id") or f"safety_job_{uuid4().hex}")
    job = {
        **_identity(context),
        "job_id": job_id,
        "tour_id": normalized_tour_id,
        "analysis_version": ANALYSIS_VERSION,
        "status": "queued",
        "attempts": int((existing or {}).get("attempts") or 0),
        "requested_at": now,
        "updated_at": now,
        "requested_by": _actor(user),
    }
    try:
        if existing:
            safety_analysis_jobs_collection.update_one(
                {"job_id": job_id}, {"$set": job}
            )
        else:
            safety_analysis_jobs_collection.insert_one(job)
    except DuplicateKeyError:
        duplicate = safety_analysis_jobs_collection.find_one(
            {
                "project_id": context["project_id"],
                "tour_id": normalized_tour_id,
                "analysis_version": ANALYSIS_VERSION,
            }
        )
        return public_document(duplicate), False
    write_audit_event(
        context=context,
        user=user,
        action="queued",
        entity_type="safety_analysis_job",
        entity_id=job_id,
        details={"tour_id": normalized_tour_id, "analysis_version": ANALYSIS_VERSION},
    )
    return public_document(job), True


def run_analysis_job(job_id: str) -> None:
    """Background-safe Phase 1 adapter over the already stored worker detections."""
    job = raw_safety_analysis_jobs_collection.find_one({"job_id": job_id})
    if not job:
        return
    now = utc_now()
    raw_safety_analysis_jobs_collection.update_one(
        {"job_id": job_id},
        {"$set": {"status": "processing", "started_at": now, "updated_at": now}, "$inc": {"attempts": 1}},
    )
    try:
        tour = raw_tours_collection.find_one({"tour_id": job["tour_id"]})
        if not tour:
            raise ValueError("Tour no longer exists")
        metric = preliminary_worker_count(tour.get("nodes") or [])
        result_status = "completed" if metric["observed_workers"] is not None else "partially_completed"
        observation_id = ""
        if metric["observed_workers"] is not None:
            observation_id = _record_id("workforce_observation")
            observed_at = tour.get("created_at") or tour.get("date") or utc_now().isoformat()
            record_date = str(observed_at)[:10] if str(observed_at) else today_iso()
            observation = {
                "record_id": observation_id,
                "record_type": "workforce_observation",
                "record_date": record_date,
                "status": "needs_review",
                "observed_workers": metric["observed_workers"],
                "source": "existing_tour_ai",
                "confidence_label": "preliminary",
                "requires_review": True,
                "tour_id": job["tour_id"],
                "analysis_job_id": job_id,
                "analysis_version": job["analysis_version"],
                "calculation": metric,
                "observed_at": observed_at,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "owner_user_id": job.get("owner_user_id"),
                "owner_email": job.get("owner_email"),
                **{key: job.get(key) for key in ("project_id", "site_name", "floorplan_id")},
            }
            raw_safety_records_collection.update_one(
                {"project_id": job["project_id"], "record_type": "workforce_observation", "analysis_job_id": job_id},
                {"$set": observation},
                upsert=True,
            )
        finished = utc_now()
        raw_safety_analysis_jobs_collection.update_one(
            {"job_id": job_id},
            {"$set": {
                "status": result_status,
                "result": {**metric, "observation_id": observation_id, "ppe_status": "not_available_in_phase1_model"},
                "finished_at": finished,
                "updated_at": finished,
                "error": None,
            }},
        )
    except Exception as error:
        failed_at = utc_now()
        raw_safety_analysis_jobs_collection.update_one(
            {"job_id": job_id},
            {"$set": {"status": "failed", "error": str(error), "finished_at": failed_at, "updated_at": failed_at}},
        )


def list_analysis_jobs(project_ref: str, *, limit: int = 100) -> list[dict[str, Any]]:
    context = project_context(project_ref)
    jobs = safety_analysis_jobs_collection.find(
        {"project_id": context["project_id"]}
    ).sort("requested_at", -1).limit(max(1, min(limit, 500)))
    return [public_document(job) for job in jobs]


def get_analysis_job(project_ref: str, job_id: str) -> dict[str, Any]:
    context = project_context(project_ref)
    job = safety_analysis_jobs_collection.find_one(
        {"project_id": context["project_id"], "job_id": job_id}
    )
    if not job:
        raise HTTPException(404, "Safety analysis job not found")
    return public_document(job)


def verify_permit_for_start(permit: dict[str, Any], *, at: Optional[datetime] = None) -> dict[str, Any]:
    now = at or utc_now()
    reasons: list[str] = []
    status = str(permit.get("status") or "draft").lower()
    if status not in {"approved", "active"}:
        reasons.append("Permit is not approved or active")
    for key, label, is_start in (
        ("valid_from", "Permit validity has not started", True),
        ("valid_until", "Permit has expired", False),
    ):
        raw = str(permit.get(key) or "").strip()
        if not raw:
            reasons.append(f"{key.replace('_', ' ').title()} is missing")
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if is_start and now < parsed:
                reasons.append(label)
            if not is_start and now > parsed:
                reasons.append(label)
        except ValueError:
            reasons.append(f"{key.replace('_', ' ').title()} is invalid")
    controls = permit.get("controls") or []
    incomplete = [
        str(control.get("label") or control.get("name") or "Unnamed control")
        for control in controls
        if isinstance(control, dict) and control.get("confirmed") is not True
    ]
    if incomplete:
        reasons.append("Unconfirmed controls: " + ", ".join(incomplete))
    return {"allowed": not reasons, "status": "verified" if not reasons else "blocked", "reasons": reasons, "checked_at": now.isoformat()}


def verify_permit_record(
    project_ref: str, permit_id: str, *, user: AuthenticatedUser
) -> dict[str, Any]:
    context = project_context(project_ref)
    permit = safety_records_collection.find_one(
        {"project_id": context["project_id"], "record_type": "permit", "record_id": permit_id}
    )
    if not permit:
        raise HTTPException(404, "Permit not found")
    result = {"permit_id": permit_id, **verify_permit_for_start(permit)}
    safety_records_collection.update_one(
        {
            "project_id": context["project_id"],
            "record_type": "permit",
            "record_id": permit_id,
        },
        {
            "$set": {
                "last_start_verification": result,
                "updated_at": utc_now(),
                "updated_by": _actor(user),
            }
        },
    )
    write_audit_event(
        context=context,
        user=user,
        action="start_verified" if result["allowed"] else "start_blocked",
        entity_type="permit",
        entity_id=permit_id,
        details={"reasons": result["reasons"]},
    )
    return result


def _latest_by_date(records: list[dict[str, Any]], record_date: str) -> Optional[dict[str, Any]]:
    return next((record for record in records if record.get("record_date") == record_date), None)


def build_dashboard(project_ref: str, *, record_date: str = "") -> dict[str, Any]:
    context = project_context(project_ref)
    project = context.get("document") or {}
    bounds = project.get("bounds") if isinstance(project.get("bounds"), dict) else {}
    day = record_date or today_iso()
    plans = list_records(project_ref, "workforce_plan", limit=1000)
    observations = list_records(project_ref, "workforce_observation", limit=1000)
    findings = list_records(project_ref, "safety_finding", limit=1000)
    hazards = list_records(project_ref, "hazard", limit=1000)
    permits = list_records(project_ref, "permit", limit=1000)
    checks = list_records(project_ref, "check_run", limit=1000)
    zones = list_records(project_ref, "safety_zone", limit=1000)
    jobs = list_analysis_jobs(project_ref, limit=10)
    weather = get_weather(project_ref)
    plan = _latest_by_date(plans, day)
    observation = _latest_by_date(observations, day)
    planned = int((plan or {}).get("planned_workers") or 0)
    observed_raw = (observation or {}).get("observed_workers")
    observed = int(observed_raw) if observed_raw is not None else None
    variance = observed - planned if observed is not None else None
    open_findings = [item for item in findings if str(item.get("status") or "open").lower() not in {"closed", "resolved", "verified"}]
    open_hazards = [item for item in hazards if str(item.get("status") or "open").lower() not in {"closed", "resolved", "verified"}]
    critical = [
        item for item in [*open_findings, *open_hazards]
        if str(item.get("severity") or "").lower() in {"critical", "high"}
    ]
    active_permits = [item for item in permits if str(item.get("status") or "").lower() in {"approved", "active"}]
    overdue_checks = [item for item in checks if str(item.get("status") or "").lower() == "overdue"]
    work_state = str(weather.get("work_state") or "unknown")
    reasons = list(weather.get("reasons") or [])
    if critical:
        work_state = "stop_work"
        reasons.append(f"{len(critical)} high/critical unresolved safety item(s)")
    elif work_state == "safe" and (open_findings or open_hazards or overdue_checks):
        work_state = "caution"
        reasons.append("Open safety actions require attention")
    return {
        **_identity(context),
        "record_date": day,
        "generated_at": utc_now().isoformat(),
        "work_state": {"status": work_state, "reasons": reasons},
        "manpower": {
            "planned_workers": planned,
            "observed_workers": observed,
            "variance": variance,
            "observation_source": (observation or {}).get("source"),
            "requires_review": bool((observation or {}).get("requires_review")),
        },
        "weather": weather,
        "ppe": {
            "status": "preliminary" if findings else "unknown",
            "open_findings": len(open_findings),
            "high_or_critical": len(critical),
            "model_note": "Dedicated PPE model upgrade is deferred to Phase 2.",
        },
        "counts": {
            "open_hazards": len(open_hazards),
            "active_permits": len(active_permits),
            "overdue_checks": len(overdue_checks),
            "active_zones": len([zone for zone in zones if str(zone.get("status") or "active").lower() == "active"]),
        },
        "spatial": {
            "floorplan_width": _float(bounds.get("width")),
            "floorplan_height": _float(bounds.get("height")),
            "hazards": [
                {
                    "record_id": item.get("record_id"),
                    "title": item.get("title"),
                    "severity": item.get("severity"),
                    "point": item.get("point"),
                }
                for item in open_hazards
                if isinstance(item.get("point"), dict)
            ],
            "zones": [
                {
                    "record_id": item.get("record_id"),
                    "title": item.get("title"),
                    "severity": item.get("severity"),
                    "polygon": item.get("polygon"),
                }
                for item in zones
                if str(item.get("status") or "active").lower() == "active"
                and isinstance(item.get("polygon"), list)
            ],
        },
        "recent": {
            "findings": open_findings[:5],
            "hazards": open_hazards[:5],
            "permits": permits[:5],
            "checks": checks[:5],
            "analysis_jobs": jobs[:5],
        },
    }


def generate_daily_report(
    project_ref: str, *, record_date: str, user: AuthenticatedUser
) -> dict[str, Any]:
    context = project_context(project_ref)
    day = record_date or today_iso()
    snapshot = build_dashboard(project_ref, record_date=day)
    existing = safety_records_collection.find_one(
        {"project_id": context["project_id"], "record_type": "daily_report", "record_date": day}
    )
    if existing and str(existing.get("status") or "") == "finalized":
        return public_document(existing)
    if existing:
        return update_record(
            project_ref,
            record_type="daily_report",
            record_id=existing["record_id"],
            payload={"snapshot": snapshot, "status": "draft", "generated_at": utc_now().isoformat()},
            user=user,
        )
    return create_record(
        project_ref,
        record_type="daily_report",
        payload={"record_date": day, "status": "draft", "snapshot": snapshot, "generated_at": utc_now().isoformat()},
        user=user,
    )


def finalize_daily_report(
    project_ref: str, report_id: str, *, user: AuthenticatedUser
) -> dict[str, Any]:
    return update_record(
        project_ref,
        record_type="daily_report",
        record_id=report_id,
        payload={"status": "finalized", "finalized_at": utc_now().isoformat(), "finalized_by": _actor(user)},
        user=user,
    )


def render_daily_report_pdf(project_ref: str, report_id: str) -> tuple[bytes, str]:
    context = project_context(project_ref)
    report = safety_records_collection.find_one(
        {"project_id": context["project_id"], "record_type": "daily_report", "record_id": report_id}
    )
    if not report:
        raise HTTPException(404, "Daily safety report not found")
    snapshot = report.get("snapshot") or {}
    manpower = snapshot.get("manpower") or {}
    ppe = snapshot.get("ppe") or {}
    counts = snapshot.get("counts") or {}
    weather = snapshot.get("weather") or {}
    state = snapshot.get("work_state") or {}
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, "Safety and Manpower Daily Report", ln=1)
    pdf.set_font("Arial", size=10)
    lines = [
        f"Project: {context['site_name']}",
        f"Date: {report.get('record_date') or 'N/A'}",
        f"Work state: {str(state.get('status') or 'unknown').upper()}",
        f"Planned / observed workforce: {manpower.get('planned_workers', 0)} / {manpower.get('observed_workers', 'N/A')}",
        f"Workforce variance: {manpower.get('variance', 'N/A')}",
        f"PPE status: {ppe.get('status', 'unknown')} ({ppe.get('open_findings', 0)} open findings)",
        f"Weather: wind {weather.get('wind_kph', 'N/A')} km/h, apparent heat {weather.get('apparent_temperature_c', 'N/A')} C, rain {weather.get('precipitation_mm_h', 'N/A')} mm/h",
        f"Open hazards: {counts.get('open_hazards', 0)}",
        f"Active permits: {counts.get('active_permits', 0)}",
        f"Overdue checks: {counts.get('overdue_checks', 0)}",
        f"Active exclusion zones: {counts.get('active_zones', 0)}",
    ]
    for line in lines:
        pdf.multi_cell(0, 7, str(line).encode("latin-1", errors="replace").decode("latin-1"))
    reasons = state.get("reasons") or []
    if reasons:
        pdf.ln(2)
        pdf.set_font("Arial", "B", 11)
        pdf.cell(0, 8, "Safety reasons / actions", ln=1)
        pdf.set_font("Arial", size=10)
        for reason in reasons:
            pdf.multi_cell(0, 7, f"- {reason}".encode("latin-1", errors="replace").decode("latin-1"))
    output = pdf.output(dest="S")
    content = output.encode("latin-1") if isinstance(output, str) else bytes(output)
    filename = f"safety-manpower-{report.get('record_date') or today_iso()}.pdf"
    return content, filename
