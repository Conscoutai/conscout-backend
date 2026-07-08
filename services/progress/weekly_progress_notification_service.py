from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime
from typing import Any

from core.database import (
    notifications_collection,
    raw_floorplans_collection,
    raw_tours_collection,
    raw_users_collection,
)
from services.progress.work_schedule.work_schedule_service import (
    parse_work_schedule_date,
    work_schedule_comparison,
)


logger = logging.getLogger(__name__)

SYSTEM_SENDER_EMAIL = "system@conscout.local"
SYSTEM_SENDER_NAME = "Conscout System"
WEEKLY_PROGRESS_TYPE = "progress_weekly"
SCHEDULED_WEEKDAY = 5  # Saturday
SCHEDULED_HOUR = 18
SCHEDULED_MINUTE = 0
TOUR_WEIGHT = 0.45
ACTIVITY_WEIGHT = 0.40
MATERIAL_WEIGHT = 0.15
_scheduler_started = False
_scheduler_lock = threading.Lock()


def _normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def _normalized_date(value: datetime) -> datetime:
    return datetime(value.year, value.month, value.day)


def _parse_project_date(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return parse_work_schedule_date(raw)


def _parse_timestamp(value: Any) -> datetime | None:
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
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return parse_work_schedule_date(raw)


def _now_ms(now: datetime | None = None) -> int:
    reference = now or datetime.now()
    return int(reference.timestamp() * 1000)


def _project_filter(project_id: str) -> dict[str, Any]:
    normalized = (project_id or "").strip()
    return {"$or": [{"site_name": normalized}, {"dxf_project_id": normalized}]}


def _resolve_project_recipients(project: dict[str, Any]) -> list[dict[str, str]]:
    emails: list[str] = []

    owner_email = _normalize_email(str(project.get("owner_email") or ""))
    if owner_email:
        emails.append(owner_email)

    for email in project.get("stakeholder_emails", []) or []:
        normalized = _normalize_email(str(email))
        if normalized:
            emails.append(normalized)

    deduped_emails = sorted({email for email in emails if email})
    if not deduped_emails:
        return []

    user_lookup: dict[str, dict[str, Any]] = {}
    for user in raw_users_collection.find(
        {"email": {"$in": deduped_emails}},
        {"email": 1, "user_id": 1, "name": 1},
    ):
        normalized_email = _normalize_email(str(user.get("email") or ""))
        if normalized_email:
            user_lookup[normalized_email] = user

    recipients: list[dict[str, str]] = []
    for email in deduped_emails:
        user = user_lookup.get(email, {})
        recipients.append(
            {
                "email": email,
                "user_id": str(user.get("user_id") or "").strip(),
                "name": str(user.get("name") or "").strip(),
            }
        )
    return recipients


def _list_project_docs(project_id: str | None = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {}
    if project_id and project_id.strip():
        query = _project_filter(project_id.strip())

    seen_keys: set[str] = set()
    projects: list[dict[str, Any]] = []
    for project in raw_floorplans_collection.find(
        query,
        {
            "site_name": 1,
            "dxf_project_id": 1,
            "owner_email": 1,
            "owner_user_id": 1,
            "stakeholder_emails": 1,
            "project_start_date": 1,
            "projectStartDate": 1,
            "start_date": 1,
            "created_at": 1,
            "updated_at": 1,
            "id": 1,
        },
    ).sort("_id", -1):
        resolved_site = str(
            project.get("site_name") or project.get("dxf_project_id") or ""
        ).strip()
        if not resolved_site:
            continue
        site_key = resolved_site.lower()
        if site_key in seen_keys:
            continue
        seen_keys.add(site_key)
        projects.append(project)
    return projects


def _tour_summary_percentage(tour: dict[str, Any]) -> float | None:
    summary = tour.get("progress", {}).get("summary")
    if not isinstance(summary, dict):
        return None
    value = summary.get("percentage")
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed * 100 if parsed <= 1 else parsed


def _latest_tour_progress(project: dict[str, Any]) -> float | None:
    floorplan_ids = []
    for floorplan in raw_floorplans_collection.find(
        _project_filter(
            str(project.get("site_name") or project.get("dxf_project_id") or "").strip()
        ),
        {"id": 1},
    ):
        floorplan_id = str(floorplan.get("id") or "").strip()
        if floorplan_id:
            floorplan_ids.append(floorplan_id)

    if not floorplan_ids:
        return None

    tours = list(
        raw_tours_collection.find(
            {"floorplan_id": {"$in": floorplan_ids}},
            {"progress": 1, "captured_at": 1, "created_at": 1},
        )
    )
    if not tours:
        return None

    tours.sort(
        key=lambda item: (
            _parse_timestamp(item.get("captured_at"))
            or _parse_timestamp(item.get("created_at"))
            or datetime.min
        ),
        reverse=True,
    )

    for tour in tours:
        percentage = _tour_summary_percentage(tour)
        if percentage is not None:
            return max(0.0, min(percentage, 100.0))
    return None


def _activity_progress(project_id: str) -> float | None:
    try:
        comparison = work_schedule_comparison(project_id)
    except Exception:
        return None

    activities = comparison.get("activities") or []
    if not activities:
        return None

    total_weight = 0.0
    weighted_progress = 0.0
    for activity in activities:
        planned_percent = float(activity.get("planned_percent") or 0)
        weight = max(1.0, planned_percent)
        actual_percent = max(0.0, min(float(activity.get("actual_percent") or 0), 100.0))
        total_weight += weight
        weighted_progress += actual_percent * weight

    if total_weight <= 0:
        return None
    return max(0.0, min(weighted_progress / total_weight, 100.0))


def _material_progress(project: dict[str, Any]) -> float | None:
    material_setup = project.get("progress_materials") or project.get("materials_progress")
    if not isinstance(material_setup, dict):
        return None

    entries = material_setup.get("entries")
    if not isinstance(entries, list) or not entries:
        return None

    total_score = 0.0
    counted = 0
    today = _normalized_date(datetime.now())
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        total_quantity = float(entry.get("totalQuantity") or 0)
        quantity_used = float(entry.get("quantityUsed") or 0)
        delivery_date = _parse_project_date(entry.get("deliveryDate"))
        if total_quantity > 0 and quantity_used >= total_quantity:
            total_score += 100.0
            counted += 1
            continue
        if delivery_date is None:
            continue
        delta = (_normalized_date(delivery_date) - today).days
        if delta < 0:
            total_score += 35.0
        elif delta <= 7:
            total_score += 75.0
        else:
            total_score += 100.0
        counted += 1

    if counted <= 0:
        return None
    return max(0.0, min(total_score / counted, 100.0))


def _calculate_overall_progress(project: dict[str, Any]) -> dict[str, Any] | None:
    project_name = str(project.get("site_name") or project.get("dxf_project_id") or "").strip()
    if not project_name:
        return None

    components = [
        {
            "key": "tour",
            "label": "Tour verified",
            "value": _latest_tour_progress(project),
            "weight": TOUR_WEIGHT,
        },
        {
            "key": "activity",
            "label": "Work activity",
            "value": _activity_progress(project_name),
            "weight": ACTIVITY_WEIGHT,
        },
        {
            "key": "material",
            "label": "Material readiness",
            "value": _material_progress(project),
            "weight": MATERIAL_WEIGHT,
        },
    ]

    available_components = [component for component in components if component["value"] is not None]
    if not available_components:
        return None

    total_weight = sum(float(component["weight"]) for component in available_components)
    if total_weight <= 0:
        return None

    overall_score = 0.0
    resolved_components: list[dict[str, Any]] = []
    for component in components:
        value = component["value"]
        if value is None:
            resolved_components.append(
                {
                    **component,
                    "effective_weight": 0.0,
                    "contribution": 0.0,
                    "available": False,
                }
            )
            continue

        effective_weight = float(component["weight"]) / total_weight
        contribution = float(value) * effective_weight
        overall_score += contribution
        resolved_components.append(
            {
                **component,
                "effective_weight": effective_weight,
                "contribution": contribution,
                "available": True,
            }
        )

    return {
        "overall_score": max(0.0, min(overall_score, 100.0)),
        "components": resolved_components,
    }


def _project_start_date(project: dict[str, Any], now: datetime) -> datetime:
    parsed = (
        _parse_project_date(project.get("project_start_date"))
        or _parse_project_date(project.get("projectStartDate"))
        or _parse_project_date(project.get("start_date"))
        or _parse_timestamp(project.get("created_at"))
        or _parse_timestamp(project.get("updated_at"))
    )
    if parsed is None:
        return _normalized_date(now)
    return _normalized_date(parsed)


def _project_week_window(project: dict[str, Any], now: datetime) -> dict[str, Any]:
    normalized_now = _normalized_date(now)
    project_start = _project_start_date(project, now)

    if normalized_now.isBefore(project_start):
        week_start = project_start
        week_number = 1
    else:
        days_from_start = normalized_now.difference(project_start).inDays
        week_index = days_from_start // 7
        week_start = project_start + timedelta_days(week_index * 7)
        week_number = week_index + 1

    week_end = week_start + timedelta_days(6)
    week_key = week_start.date().isoformat()
    return {
        "week_key": week_key,
        "week_number": week_number,
        "week_start": week_start,
        "week_end": week_end,
    }


def timedelta_days(days: int) -> Any:
    from datetime import timedelta

    return timedelta(days=days)


def _latest_previous_week_notification(site_name: str, week_key: str) -> dict[str, Any] | None:
    return notifications_collection.find_one(
        {
            "type": WEEKLY_PROGRESS_TYPE,
            "site_name": site_name,
            "entity_id": {"$ne": week_key},
        },
        sort=[("created_at", -1)],
    )


def _notification_title(delta: float | None) -> str:
    if delta is None:
        return "Weekly progress update"
    if delta > 0.1:
        return "Weekly progress up"
    if delta < -0.1:
        return "Weekly progress down"
    return "Weekly progress update"


def _notification_message(site_name: str, score: float, delta: float | None) -> str:
    if delta is None:
        return f"{site_name} closed the week at {score:.1f}% overall progress."
    if delta > 0.1:
        return f"{site_name} improved by {abs(delta):.1f}% this week and is now at {score:.1f}%."
    if delta < -0.1:
        return f"{site_name} dropped by {abs(delta):.1f}% this week and is now at {score:.1f}%."
    return f"{site_name} stayed steady this week at {score:.1f}% overall progress."


def _notification_severity(delta: float | None) -> str:
    if delta is None:
        return "info"
    if delta < -0.1:
        return "warning"
    return "info"


def _component_contribution(components: list[dict[str, Any]], key: str) -> float:
    for component in components:
        if component.get("key") == key:
            return round(float(component.get("contribution") or 0), 1)
    return 0.0


def _create_weekly_notification(
    *,
    project: dict[str, Any],
    recipient: dict[str, str],
    site_name: str,
    week_window: dict[str, Any],
    calculation: dict[str, Any],
    previous_notification: dict[str, Any] | None,
    now: datetime,
) -> str:
    existing = notifications_collection.find_one(
        {
            "type": WEEKLY_PROGRESS_TYPE,
            "site_name": site_name,
            "recipient_email": recipient["email"],
            "entity_id": week_window["week_key"],
        },
        sort=[("created_at", -1)],
    )
    if existing:
        return "duplicate"

    previous_progress = None
    if isinstance(previous_notification, dict):
        previous_progress = (
            (previous_notification.get("metadata") or {}).get("current_progress")
            if isinstance(previous_notification.get("metadata"), dict)
            else None
        )
    try:
        previous_progress_value = (
            float(previous_progress) if previous_progress is not None else None
        )
    except (TypeError, ValueError):
        previous_progress_value = None

    current_progress = round(float(calculation["overall_score"]), 1)
    delta = (
        round(current_progress - previous_progress_value, 1)
        if previous_progress_value is not None
        else None
    )
    components = calculation["components"]
    metadata = {
        "project_name": site_name,
        "site_name": site_name,
        "week_key": week_window["week_key"],
        "week_number": int(week_window["week_number"]),
        "week_start": week_window["week_start"].date().isoformat(),
        "week_end": week_window["week_end"].date().isoformat(),
        "current_progress": current_progress,
        "previous_progress": previous_progress_value,
        "progress_delta": delta,
        "tour_verified": _component_contribution(components, "tour"),
        "work_activity": _component_contribution(components, "activity"),
        "material_readiness": _component_contribution(components, "material"),
        "available_inputs": [
            str(component.get("key"))
            for component in components
            if component.get("available") is True
        ],
        "calculated_at": now.isoformat(),
        "project_start_date": _project_start_date(project, now).date().isoformat(),
    }
    notifications_collection.insert_one(
        {
            "type": WEEKLY_PROGRESS_TYPE,
            "title": _notification_title(delta),
            "message": _notification_message(site_name, current_progress, delta),
            "site_name": site_name,
            "recipient_email": recipient["email"],
            "recipient_user_id": recipient["user_id"],
            "sender_email": SYSTEM_SENDER_EMAIL,
            "sender_name": SYSTEM_SENDER_NAME,
            "status": "pending",
            "severity": _notification_severity(delta),
            "is_read": False,
            "primary_action_label": "Open progress",
            "primary_action_type": "open_progress",
            "secondary_action_label": "",
            "secondary_action_type": "",
            "entity_id": week_window["week_key"],
            "entity_type": "weekly_progress",
            "route": f"/projects/{site_name}/progress/overview",
            "metadata": metadata,
            "created_at": _now_ms(now),
            "updated_at": _now_ms(now),
            "acted_at": 0,
        }
    )
    return "created"


def sync_weekly_progress_notifications(
    *,
    project_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    reference_time = now or datetime.now()
    projects = _list_project_docs(project_id)

    project_results: list[dict[str, Any]] = []
    created_count = 0
    duplicate_count = 0
    skipped_count = 0

    for project in projects:
        site_name = str(project.get("site_name") or project.get("dxf_project_id") or "").strip()
        if not site_name:
            skipped_count += 1
            continue

        recipients = _resolve_project_recipients(project)
        if not recipients:
            skipped_count += 1
            project_results.append(
                {
                    "project_id": site_name,
                    "status": "skipped",
                    "reason": "no_recipients",
                }
            )
            continue

        calculation = _calculate_overall_progress(project)
        if calculation is None:
            skipped_count += len(recipients)
            project_results.append(
                {
                    "project_id": site_name,
                    "status": "skipped",
                    "reason": "no_progress_inputs",
                }
            )
            continue

        week_window = _project_week_window(project, reference_time)
        previous_notification = _latest_previous_week_notification(
            site_name,
            week_window["week_key"],
        )

        project_created = 0
        project_duplicates = 0
        for recipient in recipients:
            outcome = _create_weekly_notification(
                project=project,
                recipient=recipient,
                site_name=site_name,
                week_window=week_window,
                calculation=calculation,
                previous_notification=previous_notification,
                now=reference_time,
            )
            if outcome == "created":
                created_count += 1
                project_created += 1
            else:
                duplicate_count += 1
                project_duplicates += 1

        project_results.append(
            {
                "project_id": site_name,
                "status": "processed",
                "week_key": week_window["week_key"],
                "week_number": week_window["week_number"],
                "overall_progress": round(float(calculation["overall_score"]), 1),
                "created_count": project_created,
                "duplicate_count": project_duplicates,
            }
        )

    return {
        "scheduled_for": reference_time.isoformat(),
        "project_count": len(projects),
        "created_count": created_count,
        "duplicate_count": duplicate_count,
        "skipped_count": skipped_count,
        "projects": project_results,
    }


def _scheduler_window_key(now: datetime) -> str:
    return now.date().isoformat()


def _should_run_scheduler(now: datetime) -> bool:
    return (
        now.weekday() == SCHEDULED_WEEKDAY
        and now.hour == SCHEDULED_HOUR
        and now.minute >= SCHEDULED_MINUTE
    )


def run_weekly_progress_scheduler(poll_seconds: int = 60) -> None:
    last_run_key = ""
    while True:
        try:
            now = datetime.now()
            if _should_run_scheduler(now):
                window_key = _scheduler_window_key(now)
                if last_run_key != window_key:
                    logger.info("Running weekly progress notification scheduler")
                    result = sync_weekly_progress_notifications(now=now)
                    logger.info(
                        "Weekly progress notifications processed: created=%s duplicates=%s skipped=%s",
                        result.get("created_count"),
                        result.get("duplicate_count"),
                        result.get("skipped_count"),
                    )
                    last_run_key = window_key
        except Exception:
            logger.exception("Weekly progress scheduler failed")

        time.sleep(max(15, int(poll_seconds)))


def ensure_weekly_progress_scheduler_started() -> None:
    global _scheduler_started

    with _scheduler_lock:
        if _scheduler_started:
            return

        thread = threading.Thread(
            target=run_weekly_progress_scheduler,
            name="weekly-progress-scheduler",
            daemon=True,
        )
        thread.start()
        _scheduler_started = True
        logger.info("Weekly progress scheduler started")
