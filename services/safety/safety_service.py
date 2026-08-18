from __future__ import annotations

import os
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from services.progress.work_schedule.analytics_service import build_baseline_comparison
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
    "daily_report_cutoff": "18:00",
    "auto_daily_reports": False,
    "report_recipients": [],
    "hazard_categories": [
        "unsafe_condition",
        "unsafe_act",
        "housekeeping",
        "access_egress",
        "work_at_height",
        "plant_equipment",
    ],
    "hazard_resolution_hours": 24,
    "permit_types": [
        "hot_work",
        "work_at_height",
        "lifting",
        "confined_space",
        "excavation",
        "electrical",
    ],
    "required_permit_approvals": 1,
    "shift_names": ["day"],
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


def validate_safety_config(config: dict[str, Any]) -> dict[str, Any]:
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
    timezone_name = str(config.get("timezone") or "UTC").strip()
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise HTTPException(422, "Project timezone is invalid") from error
    cutoff = str(config.get("daily_report_cutoff") or "").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", cutoff):
        raise HTTPException(422, "Daily report cutoff must use 24-hour HH:MM format")
    required_approvals = int(config.get("required_permit_approvals") or 0)
    if required_approvals < 0:
        raise HTTPException(422, "Required permit approvals cannot be negative")
    resolution_hours = int(config.get("hazard_resolution_hours") or 0)
    if resolution_hours <= 0:
        raise HTTPException(422, "Hazard resolution SLA must be greater than zero hours")
    return config


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
    validate_safety_config(config)
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
    project = context.get("document") or {}
    client_reference_id = str(payload.get("client_reference_id") or "").strip()
    if client_reference_id:
        existing = safety_records_collection.find_one(
            {
                "project_id": context["project_id"],
                "record_type": record_type,
                "client_reference_id": client_reference_id,
            }
        )
        if existing:
            return public_document(existing)
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
    owner_user_id = str(project.get("owner_user_id") or "").strip()
    owner_email = str(project.get("owner_email") or "").strip().lower()
    if owner_user_id:
        document.setdefault("owner_user_id", owner_user_id)
    if owner_email:
        document.setdefault("owner_email", owner_email)
    try:
        safety_records_collection.insert_one(document)
    except DuplicateKeyError:
        if not client_reference_id:
            raise
        concurrent = safety_records_collection.find_one(
            {
                "project_id": context["project_id"],
                "record_type": record_type,
                "client_reference_id": client_reference_id,
            }
        )
        if concurrent:
            return public_document(concurrent)
        raise
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
    if record_type == "daily_report" and str(existing.get("status") or "").lower() == "finalized":
        raise HTTPException(409, "Finalized daily reports are immutable; generate a new revision")
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
    now = utc_now()
    previous_status = str(existing.get("status") or "").lower()
    next_status = str(changes.get("status") or previous_status).lower()
    if record_type == "hazard" and next_status != previous_status:
        if next_status in {"resolved", "closed", "verified"}:
            changes.setdefault("resolved_at", now.isoformat())
            changes.setdefault("resolved_by", _actor(user))
        elif previous_status in {"resolved", "closed", "verified"}:
            changes["reopened_at"] = now.isoformat()
            changes["reopened_by"] = _actor(user)
            changes["resolved_at"] = None
            changes["resolved_by"] = None
    if record_type in {"workforce_observation", "safety_finding"} and next_status in {
        "confirmed",
        "dismissed",
        "verified",
    }:
        changes.setdefault("requires_review", False)
        changes.setdefault("reviewed_at", now.isoformat())
        changes.setdefault("reviewed_by", _actor(user))
    changes.update({"updated_at": now, "updated_by": _actor(user)})
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
    for existing_attachment in record.get("attachments") or []:
        if (
            isinstance(existing_attachment, dict)
            and str(existing_attachment.get("filename") or "") == safe_name
            and int(existing_attachment.get("size_bytes") or -1) == len(content)
        ):
            return public_document(existing_attachment)
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
                "temperature_2m,apparent_temperature,relative_humidity_2m,"
                "precipitation,wind_speed_10m,"
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
            "temperature_c": _float(current.get("temperature_2m")),
            "relative_humidity_percent": _float(
                current.get("relative_humidity_2m")
            ),
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


def verify_permit_for_start(
    permit: dict[str, Any],
    *,
    at: Optional[datetime] = None,
    required_approvals: int = 0,
    weather_state: str = "",
    expected_activity_type: str = "",
    expected_zone_id: str = "",
    valid_check_ids: Optional[set[str]] = None,
    critical_block_count: int = 0,
) -> dict[str, Any]:
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
    approvals = [
        approval
        for approval in (permit.get("approvals") or [])
        if isinstance(approval, dict)
        and str(approval.get("status") or "approved").lower() == "approved"
        and str(approval.get("approver_name") or approval.get("name") or "").strip()
        and str(approval.get("signature") or approval.get("signed_at") or "").strip()
    ]
    if len(approvals) < max(0, required_approvals):
        reasons.append(
            f"Permit requires {required_approvals} signed approval(s); {len(approvals)} recorded"
        )
    checklist = permit.get("checklist_items") or []
    incomplete_checks = [
        str(item.get("label") or item.get("name") or "Unnamed check")
        for item in checklist
        if isinstance(item, dict) and item.get("confirmed") is not True
    ]
    if incomplete_checks:
        reasons.append("Incomplete permit checks: " + ", ".join(incomplete_checks))
    if permit.get("weather_sensitive") is True and weather_state in {"stop_work", "unknown"}:
        reasons.append(
            "Weather-dependent work is blocked because current weather is "
            + ("unavailable" if weather_state == "unknown" else "at stop-work level")
        )
    permit_activity = str(permit.get("activity_type") or "").strip()
    if expected_activity_type and permit_activity != expected_activity_type:
        reasons.append("Permit does not match the requested high-risk activity")
    permit_zone = str(permit.get("zone_id") or "").strip()
    if expected_zone_id and permit_zone != expected_zone_id:
        reasons.append("Permit does not match the requested safety zone")
    required_check_ids = {
        str(value).strip()
        for value in (permit.get("required_check_ids") or [])
        if str(value).strip()
    }
    missing_checks = sorted(required_check_ids - (valid_check_ids or set()))
    if missing_checks:
        reasons.append("Required safety checks are incomplete: " + ", ".join(missing_checks))
    if critical_block_count > 0:
        reasons.append(f"{critical_block_count} unresolved critical safety block(s) apply")
    return {"allowed": not reasons, "status": "verified" if not reasons else "blocked", "reasons": reasons, "checked_at": now.isoformat()}


def verify_permit_record(
    project_ref: str,
    permit_id: str,
    *,
    user: AuthenticatedUser,
    expected_activity_type: str = "",
    expected_zone_id: str = "",
) -> dict[str, Any]:
    context = project_context(project_ref)
    permit = safety_records_collection.find_one(
        {"project_id": context["project_id"], "record_type": "permit", "record_id": permit_id}
    )
    if not permit:
        raise HTTPException(404, "Permit not found")
    config = get_config(project_ref)["config"]
    weather_state = ""
    if permit.get("weather_sensitive") is True:
        weather_state = str(get_weather(project_ref).get("work_state") or "unknown")
    required_check_ids = {
        str(value).strip()
        for value in (permit.get("required_check_ids") or [])
        if str(value).strip()
    }
    valid_check_ids: set[str] = set()
    if required_check_ids:
        valid_checks = safety_records_collection.find(
            {
                "project_id": context["project_id"],
                "record_type": "check_run",
                "record_id": {"$in": list(required_check_ids)},
                "status": {"$in": ["completed", "verified", "ready"]},
            },
            {"record_id": 1},
        )
        valid_check_ids = {str(item.get("record_id") or "") for item in valid_checks}
    block_query: dict[str, Any] = {
        "project_id": context["project_id"],
        "record_type": {"$in": ["hazard", "safety_finding"]},
        "severity": {"$in": ["high", "critical"]},
        "status": {"$nin": ["closed", "resolved", "verified", "dismissed"]},
    }
    zone_to_check = expected_zone_id or str(permit.get("zone_id") or "")
    if zone_to_check:
        block_query["$or"] = [
            {"zone_id": zone_to_check},
            {"zone_id": {"$in": [None, ""]}},
            {"zone_id": {"$exists": False}},
        ]
    critical_block_count = sum(
        1 for _ in safety_records_collection.find(block_query, {"_id": 1})
    )
    result = {
        "permit_id": permit_id,
        **verify_permit_for_start(
            permit,
            required_approvals=int(config.get("required_permit_approvals") or 0),
            weather_state=weather_state,
            expected_activity_type=expected_activity_type,
            expected_zone_id=expected_zone_id,
            valid_check_ids=valid_check_ids,
            critical_block_count=critical_block_count,
        ),
    }
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


def _filter_records(
    records: list[dict[str, Any]],
    *,
    shift: str = "",
    tour_id: str = "",
    zone_id: str = "",
) -> list[dict[str, Any]]:
    def matches(item: dict[str, Any]) -> bool:
        if shift and str(item.get("shift") or item.get("shift_name") or "") != shift:
            return False
        if tour_id and str(item.get("tour_id") or "") != tour_id:
            return False
        if zone_id and str(item.get("zone_id") or "") != zone_id:
            return False
        return True

    return [item for item in records if matches(item)]


def _automated_workforce_observations(
    records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only observations produced by tour/CV analysis."""
    accepted_sources = {
        "existing_tour_ai",
        "tour_ai",
        "ai_model",
        "computer_vision",
    }
    automated: list[dict[str, Any]] = []
    for item in records:
        source = str(item.get("source") or "").strip().lower()
        linked_analysis = bool(item.get("analysis_job_id")) and bool(
            item.get("tour_id")
        )
        if source in accepted_sources or linked_analysis:
            automated.append(item)
    return automated


def _manpower_history(
    plans: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    *,
    through_date: str,
    days: int = 14,
) -> list[dict[str, Any]]:
    try:
        through = date.fromisoformat(through_date)
    except ValueError:
        through = date.today()
    start = through - timedelta(days=max(1, days) - 1)
    latest_plans: dict[str, dict[str, Any]] = {}
    latest_observations: dict[str, dict[str, Any]] = {}
    for item in plans:
        record_day = str(item.get("record_date") or "")
        if record_day and record_day not in latest_plans:
            latest_plans[record_day] = item
    for item in observations:
        record_day = str(item.get("record_date") or "")
        if record_day and record_day not in latest_observations:
            latest_observations[record_day] = item
    history: list[dict[str, Any]] = []
    cursor = start
    while cursor <= through:
        record_day = cursor.isoformat()
        plan = latest_plans.get(record_day) or {}
        observation = latest_observations.get(record_day) or {}
        planned_raw = plan.get("planned_workers")
        observed_raw = observation.get("observed_workers")
        history.append(
            {
                "record_date": record_day,
                "planned_workers": int(planned_raw)
                if planned_raw is not None
                else None,
                "observed_workers": int(observed_raw) if observed_raw is not None else None,
                "source": observation.get("source"),
                "requires_review": bool(observation.get("requires_review")),
            }
        )
        cursor += timedelta(days=1)
    return history


def _schedule_manpower_for_dates(
    project_ref: str, record_dates: Iterable[str]
) -> tuple[dict[str, dict[str, Any]], bool]:
    try:
        comparison = build_baseline_comparison(project_ref)
    except Exception:
        return {}, False
    if not comparison:
        return {}, False
    manpower = comparison.get("manpower") or {}
    weekly_points = manpower.get("points") or []
    labor_loaded_activity_count = int(
        manpower.get("labor_loaded_activity_count") or 0
    )
    schedule_activity_count = int(manpower.get("activity_count") or 0)
    resolved: dict[str, dict[str, Any]] = {}
    for raw_day in record_dates:
        try:
            target = date.fromisoformat(raw_day)
        except ValueError:
            continue
        matching: Optional[dict[str, Any]] = None
        for point in weekly_points:
            try:
                start = date.fromisoformat(str(point.get("date") or ""))
            except ValueError:
                continue
            if start <= target <= start + timedelta(days=6):
                matching = point
                break
        if matching:
            resolved[raw_day] = {
                "planned_workers": int(round(float(matching.get("planned_workers") or 0))),
                "planned_labor_hours": float(
                    matching.get("planned_labor_hours") or 0
                ),
                "source": "active_schedule",
                "period_start": start.isoformat(),
                "period_end": str(
                    matching.get("period_end")
                    or (start + timedelta(days=6)).isoformat()
                ),
                "labor_loaded_activity_count": labor_loaded_activity_count,
                "schedule_activity_count": schedule_activity_count,
            }
    return resolved, bool(manpower.get("is_partial"))


def build_dashboard(
    project_ref: str,
    *,
    record_date: str = "",
    shift: str = "",
    tour_id: str = "",
    zone_id: str = "",
) -> dict[str, Any]:
    context = project_context(project_ref)
    project = context.get("document") or {}
    bounds = project.get("bounds") if isinstance(project.get("bounds"), dict) else {}
    day = record_date or today_iso()
    observations = _automated_workforce_observations(
        _filter_records(
            list_records(project_ref, "workforce_observation", limit=1000),
            shift=shift,
            tour_id=tour_id,
            zone_id=zone_id,
        )
    )
    findings = _filter_records(
        list_records(project_ref, "safety_finding", limit=1000),
        shift=shift,
        tour_id=tour_id,
        zone_id=zone_id,
    )
    hazards = _filter_records(
        list_records(project_ref, "hazard", limit=1000),
        shift=shift,
        tour_id=tour_id,
        zone_id=zone_id,
    )
    permits = list_records(project_ref, "permit", limit=1000)
    checks = list_records(project_ref, "check_run", limit=1000)
    check_templates = list_records(project_ref, "check_template", limit=250)
    zones = list_records(project_ref, "safety_zone", limit=1000)
    reports = list_records(project_ref, "daily_report", limit=25)
    weather_events = list_records(project_ref, "weather_observation", limit=25)
    jobs = list_analysis_jobs(project_ref, limit=10)
    weather = get_weather(project_ref)
    observation = _latest_by_date(observations, day)
    history = _manpower_history([], observations, through_date=day)
    schedule_plans, schedule_partial = _schedule_manpower_for_dates(
        project_ref, [item["record_date"] for item in history]
    )
    for item in history:
        record_day = item["record_date"]
        if record_day in schedule_plans:
            item.update(schedule_plans[record_day])
    schedule_plan = schedule_plans.get(day) or {}
    planned_raw = schedule_plan.get("planned_workers")
    planned = int(planned_raw) if planned_raw is not None else None
    observed_raw = (observation or {}).get("observed_workers")
    observed = int(observed_raw) if observed_raw is not None else None
    variance = (
        observed - planned
        if observed is not None and planned is not None
        else None
    )
    open_findings = [item for item in findings if str(item.get("status") or "open").lower() not in {"closed", "resolved", "verified"}]
    open_hazards = [item for item in hazards if str(item.get("status") or "open").lower() not in {"closed", "resolved", "verified"}]
    critical = [
        item for item in [*open_findings, *open_hazards]
        if str(item.get("severity") or "").lower() in {"critical", "high"}
    ]
    ppe_compliant = len(
        [item for item in findings if str(item.get("ppe_status") or "").lower() == "compliant"]
    )
    ppe_non_compliant = len(
        [
            item
            for item in findings
            if str(item.get("ppe_status") or "").lower()
            in {"non_compliant", "missing"}
        ]
    )
    ppe_unknown = len(
        [item for item in findings if str(item.get("ppe_status") or "").lower() == "unknown"]
    )
    ppe_evaluated = ppe_compliant + ppe_non_compliant
    active_permits = [item for item in permits if str(item.get("status") or "").lower() in {"approved", "active"}]
    overdue_checks = [item for item in checks if str(item.get("status") or "").lower() == "overdue"]
    work_state = str(weather.get("work_state") or "unknown")
    reasons = list(weather.get("reasons") or [])
    latest_job_status = str((jobs[0] if jobs else {}).get("status") or "").lower()
    analysis_unavailable = observation is None or bool(
        (observation or {}).get("requires_review")
    )
    if critical:
        work_state = "stop_work"
        reasons.append(f"{len(critical)} high/critical unresolved safety item(s)")
    elif work_state != "stop_work" and analysis_unavailable:
        work_state = "unknown"
        reasons.append(
            (
                "Workforce observation is awaiting human review"
                if observation is not None
                else f"Tour safety analysis is {latest_job_status.replace('_', ' ')}"
                if latest_job_status
                else "No tour AI workforce observation is available"
            )
            + "; compliance is unknown"
        )
    elif work_state == "safe" and (open_findings or open_hazards or overdue_checks):
        work_state = "caution"
        reasons.append("Open safety actions require attention")
    return {
        **_identity(context),
        "record_date": day,
        "filters": {"shift": shift, "tour_id": tour_id, "zone_id": zone_id},
        "generated_at": utc_now().isoformat(),
        "work_state": {"status": work_state, "reasons": reasons},
        "manpower": {
            "planned_workers": planned,
            "observed_workers": observed,
            "variance": variance,
            "observation_source": (observation or {}).get("source"),
            "requires_review": bool((observation or {}).get("requires_review")),
            "planned_source": schedule_plan.get("source", "unavailable"),
            "planned_labor_hours": schedule_plan.get("planned_labor_hours"),
            "plan_period_start": schedule_plan.get("period_start"),
            "plan_period_end": schedule_plan.get("period_end"),
            "labor_loaded_activity_count": schedule_plan.get(
                "labor_loaded_activity_count", 0
            ),
            "schedule_activity_count": schedule_plan.get(
                "schedule_activity_count", 0
            ),
            "schedule_resource_warning": (
                "Active schedule labor resources are partial."
                if schedule_plan and schedule_partial
                else ""
            ),
            "peak_observed_workers": max(
                [
                    int(item["observed_workers"])
                    for item in history
                    if item.get("observed_workers") is not None
                ]
                or [0]
            ),
            "observation_coverage": (observation or {}).get(
                "sample_count",
                ((observation or {}).get("calculation") or {}).get("sample_count"),
            ),
            "observation_confidence": (observation or {}).get("confidence"),
        },
        "weather": weather,
        "ppe": {
            "status": "preliminary" if ppe_evaluated else "unknown",
            "open_findings": len(open_findings),
            "high_or_critical": len(critical),
            "compliant": ppe_compliant,
            "non_compliant": ppe_non_compliant,
            "unknown": ppe_unknown,
            "compliance_percent": (
                round(ppe_compliant * 100 / ppe_evaluated, 1)
                if ppe_evaluated
                else None
            ),
            "model_note": "Dedicated PPE model upgrade is deferred to Phase 2.",
        },
        "counts": {
            "open_hazards": len(open_hazards),
            "active_permits": len(active_permits),
            "overdue_checks": len(overdue_checks),
            "active_zones": len([zone for zone in zones if str(zone.get("status") or "active").lower() == "active"]),
            "pending_reviews": len(
                [item for item in [*observations, *findings] if item.get("requires_review") is True]
            ),
            "failed_analysis_jobs": len(
                [item for item in jobs if str(item.get("status") or "").lower() in {"failed", "partially_completed"}]
            ),
        },
        "manpower_history": history,
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
            "observations": observations[:10],
            "findings": findings[:10],
            "hazards": hazards[:10],
            "permits": permits[:10],
            "checks": checks[:10],
            "check_templates": check_templates[:10],
            "zones": zones[:10],
            "reports": reports[:10],
            "weather_events": weather_events[:10],
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
        {"project_id": context["project_id"], "record_type": "daily_report", "record_date": day},
        sort=[("revision", -1), ("created_at", -1)],
    )
    existing_status = str((existing or {}).get("status") or "").lower()
    if existing and existing_status != "finalized":
        return update_record(
            project_ref,
            record_type="daily_report",
            record_id=existing["record_id"],
            payload={
                "snapshot": snapshot,
                "status": "draft",
                "generated_at": utc_now().isoformat(),
                "reviewed_at": None,
                "reviewed_by": None,
                "review_notes": "",
            },
            user=user,
        )
    revision = int((existing or {}).get("revision") or 0) + 1
    try:
        return create_record(
            project_ref,
            record_type="daily_report",
            payload={
                "record_date": day,
                "status": "draft",
                "revision": revision,
                "snapshot": snapshot,
                "generated_at": utc_now().isoformat(),
            },
            user=user,
        )
    except DuplicateKeyError:
        concurrent = safety_records_collection.find_one(
            {
                "project_id": context["project_id"],
                "record_type": "daily_report",
                "record_date": day,
                "revision": revision,
            }
        )
        if concurrent:
            return public_document(concurrent)
        raise


def review_daily_report(
    project_ref: str,
    report_id: str,
    *,
    notes: str,
    user: AuthenticatedUser,
) -> dict[str, Any]:
    context = project_context(project_ref)
    report = safety_records_collection.find_one(
        {
            "project_id": context["project_id"],
            "record_type": "daily_report",
            "record_id": report_id,
        }
    )
    if not report:
        raise HTTPException(404, "Daily safety report not found")
    status = str(report.get("status") or "draft").lower()
    if status == "finalized":
        raise HTTPException(409, "Finalized daily reports cannot be reviewed again")
    if status not in {"draft", "reviewed"}:
        raise HTTPException(409, "Only draft daily reports can be reviewed")
    return update_record(
        project_ref,
        record_type="daily_report",
        record_id=report_id,
        payload={
            "status": "reviewed",
            "review_notes": notes,
            "reviewed_at": utc_now().isoformat(),
            "reviewed_by": _actor(user),
        },
        user=user,
    )


def finalize_daily_report(
    project_ref: str, report_id: str, *, user: AuthenticatedUser
) -> dict[str, Any]:
    context = project_context(project_ref)
    report = safety_records_collection.find_one(
        {
            "project_id": context["project_id"],
            "record_type": "daily_report",
            "record_id": report_id,
        }
    )
    if not report:
        raise HTTPException(404, "Daily safety report not found")
    if str(report.get("status") or "draft").lower() != "reviewed":
        raise HTTPException(409, "Review the daily report before finalizing it")
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
    recent = snapshot.get("recent") or {}
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, "Safety and Manpower Daily Report", ln=1)
    pdf.set_font("Arial", size=10)
    lines = [
        f"Project: {context['site_name']}",
        f"Date: {report.get('record_date') or 'N/A'}",
        f"Revision / status: {report.get('revision', 1)} / {str(report.get('status') or 'draft').upper()}",
        f"Generated at: {report.get('generated_at') or 'N/A'}",
        f"Reviewed at: {report.get('reviewed_at') or 'N/A'}",
        f"Finalized at: {report.get('finalized_at') or 'N/A'}",
        f"Work state: {str(state.get('status') or 'unknown').upper()}",
        f"Planned / observed workforce: {manpower.get('planned_workers', 0)} / {manpower.get('observed_workers', 'N/A')}",
        f"Workforce variance: {manpower.get('variance', 'N/A')}",
        f"PPE status: {ppe.get('status', 'unknown')} ({ppe.get('open_findings', 0)} open findings)",
        f"Weather: wind {weather.get('wind_kph', 'N/A')} km/h, apparent heat {weather.get('apparent_temperature_c', 'N/A')} C, rain {weather.get('precipitation_mm_h', 'N/A')} mm/h",
        f"Open hazards: {counts.get('open_hazards', 0)}",
        f"Active permits: {counts.get('active_permits', 0)}",
        f"Overdue checks: {counts.get('overdue_checks', 0)}",
        f"Active exclusion zones: {counts.get('active_zones', 0)}",
        f"Workforce records included: {len(recent.get('observations') or [])}",
        f"Safety findings included: {len(recent.get('findings') or [])}",
        f"Permit records included: {len(recent.get('permits') or [])}",
        f"Safety checks included: {len(recent.get('checks') or [])}",
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
    revision = report.get("revision")
    suffix = f"-r{revision}" if revision else ""
    filename = f"safety-manpower-{report.get('record_date') or today_iso()}{suffix}.pdf"
    return content, filename
