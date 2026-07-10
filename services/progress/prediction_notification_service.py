from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from core.auth_context import AuthenticatedUser
from core.database import (
    notifications_collection,
    raw_floorplans_collection,
    raw_users_collection,
)
from services.progress.work_schedule.work_schedule_service import (
    parse_work_schedule_date,
    work_schedule_comparison,
)


SYSTEM_SENDER_EMAIL = "system@conscout.local"
SYSTEM_SENDER_NAME = "Conscout System"
PREDICTION_TYPE = "prediction_alert"
MIN_PROGRESS_GAP_PERCENT = 8.0
MIN_PREDICTED_DELAY_DAYS = 2
MIN_RISK_SCORE = 70


def _normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _project_filter(project_id: str) -> dict[str, Any]:
    normalized = str(project_id or "").strip()
    return {"$or": [{"site_name": normalized}, {"dxf_project_id": normalized}]}


def _project_doc(project_id: str) -> dict[str, Any]:
    doc = raw_floorplans_collection.find_one(
        _project_filter(project_id),
        {
            "site_name": 1,
            "dxf_project_id": 1,
            "stakeholder_emails": 1,
            "owner_email": 1,
            "owner_user_id": 1,
        },
        sort=[("_id", -1)],
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found")
    return doc


def _site_name(project: dict[str, Any], project_id: str) -> str:
    return str(project.get("site_name") or project.get("dxf_project_id") or project_id).strip()


def _resolve_project_recipients(
    project: dict[str, Any],
    fallback_user: AuthenticatedUser | None = None,
) -> list[dict[str, str]]:
    emails: list[str] = []

    owner_email = _normalize_email(str(project.get("owner_email") or ""))
    if owner_email:
        emails.append(owner_email)

    for email in project.get("stakeholder_emails", []) or []:
        normalized = _normalize_email(str(email))
        if normalized:
            emails.append(normalized)

    if fallback_user is not None:
        fallback_email = _normalize_email(fallback_user.email)
        if fallback_email:
            emails.append(fallback_email)

    deduped_emails = sorted({email for email in emails if email})
    if not deduped_emails:
        return []

    user_lookup: dict[str, dict[str, Any]] = {}
    for user in raw_users_collection.find(
        {"email": {"$in": deduped_emails}},
        {"email": 1, "user_id": 1, "name": 1},
    ):
        email = _normalize_email(str(user.get("email") or ""))
        if email:
            user_lookup[email] = user

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


def _parse_date(value: Any) -> datetime | None:
    return parse_work_schedule_date(str(value or ""))


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _status_done(value: Any) -> bool:
    normalized = str(value or "").strip().upper()
    return normalized in {"DONE", "COMPLETE", "COMPLETED", "RESOLVED", "CLOSED"}


def _prediction_payload(
    *,
    activity: dict[str, Any],
    site_name: str,
    now: datetime,
) -> dict[str, Any] | None:
    activity_name = str(activity.get("activity_name") or "Work activity").strip()
    activity_id = str(activity.get("activity_id") or activity_name).strip()
    if not activity_id:
        return None

    status = activity.get("primary_status") or activity.get("status")
    if _status_done(status):
        return None

    start_date = _parse_date(activity.get("start_date"))
    end_date = _parse_date(activity.get("end_date"))
    if start_date is None or end_date is None:
        return None

    today = now.date()
    start_day = start_date.date()
    end_day = end_date.date()

    # Past-due work is handled by schedule_delay; prediction is for likely future misses.
    if today > end_day or today < start_day:
        return None

    duration_days = max((end_day - start_day).days + 1, 1)
    elapsed_days = max((today - start_day).days + 1, 1)
    remaining_days = max((end_day - today).days, 0)
    planned_percent = max(0.0, min(_as_float(activity.get("planned_percent")), 100.0))
    actual_percent = max(0.0, min(_as_float(activity.get("actual_percent")), 100.0))
    if planned_percent <= 0:
        return None
    if elapsed_days < 2 and remaining_days > 3:
        return None
    if elapsed_days / duration_days < 0.25 and actual_percent <= 0 and remaining_days > 3:
        return None

    expected_percent = min(
        planned_percent,
        planned_percent * min(elapsed_days / duration_days, 1.0),
    )
    progress_gap = max(expected_percent - actual_percent, 0.0)
    observed_daily_rate = actual_percent / elapsed_days if elapsed_days > 0 else 0.0
    planned_daily_rate = planned_percent / duration_days
    conservative_rate = max(observed_daily_rate, planned_daily_rate * 0.25, 0.1)
    remaining_progress = max(planned_percent - actual_percent, 0.0)
    predicted_days_needed = math.ceil(remaining_progress / conservative_rate)
    predicted_delay_days = max(predicted_days_needed - remaining_days, 0)
    evidence = activity.get("evidence") or []

    risk_score = round(
        min(
            99.0,
            35.0
            + (progress_gap * 1.7)
            + (predicted_delay_days * 8.0)
            + (10.0 if actual_percent <= 0 and elapsed_days >= 2 else 0.0)
            + (8.0 if remaining_days <= 3 else 0.0),
        )
    )

    if (
        progress_gap < MIN_PROGRESS_GAP_PERCENT
        and predicted_delay_days < MIN_PREDICTED_DELAY_DAYS
        and risk_score < MIN_RISK_SCORE
    ):
        return None

    severity = "critical" if predicted_delay_days >= 7 or risk_score >= 85 else "warning"
    delay_label = f"{predicted_delay_days} day{'s' if predicted_delay_days != 1 else ''}"
    message = (
        f"{activity_name} may finish {delay_label} late in {site_name}."
        if predicted_delay_days > 0
        else f"{activity_name} is trending behind plan in {site_name}."
    )

    return {
        "type": PREDICTION_TYPE,
        "title": "Predicted schedule delay",
        "message": message,
        "severity": severity,
        "entity_id": activity_id,
        "entity_type": "work_activity",
        "route": f"/projects/{site_name}/progress/schedule",
        "metadata": {
            "prediction_kind": "schedule_delay",
            "project_name": site_name,
            "activity_id": activity_id,
            "activity_name": activity_name,
            "zone": str(activity.get("zone") or "").strip(),
            "start_date": str(activity.get("start_date") or "").strip(),
            "end_date": str(activity.get("end_date") or "").strip(),
            "planned_progress_percent": round(planned_percent, 1),
            "actual_progress_percent": round(actual_percent, 1),
            "expected_progress_percent": round(expected_percent, 1),
            "progress_gap_percent": round(progress_gap, 1),
            "remaining_days": remaining_days,
            "predicted_delay_days": predicted_delay_days,
            "risk_score": int(risk_score),
            "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
            "related_tour_ids": activity.get("related_tour_ids") or [],
            "calculated_at": now.isoformat(),
        },
    }


def _upsert_notification(
    *,
    recipient: dict[str, str],
    site_name: str,
    payload: dict[str, Any],
) -> str:
    recipient_email = _normalize_email(recipient.get("email", ""))
    if not recipient_email:
        return "skipped"

    now_ms = _now_ms()
    existing = notifications_collection.find_one(
        {
            "type": payload["type"],
            "site_name": site_name,
            "recipient_email": recipient_email,
            "entity_id": payload["entity_id"],
            "status": "pending",
        },
        sort=[("created_at", -1)],
    )

    if existing:
        notifications_collection.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {
                    "title": payload["title"],
                    "message": payload["message"],
                    "severity": payload["severity"],
                    "entity_type": payload["entity_type"],
                    "route": payload["route"],
                    "metadata": payload["metadata"],
                    "is_read": False,
                    "updated_at": now_ms,
                }
            },
        )
        return "updated"

    notifications_collection.insert_one(
        {
            "type": payload["type"],
            "title": payload["title"],
            "message": payload["message"],
            "site_name": site_name,
            "recipient_email": recipient_email,
            "recipient_user_id": str(recipient.get("user_id") or "").strip(),
            "sender_email": SYSTEM_SENDER_EMAIL,
            "sender_name": SYSTEM_SENDER_NAME,
            "status": "pending",
            "severity": payload["severity"],
            "is_read": False,
            "primary_action_label": "Review progress",
            "primary_action_type": "open_progress",
            "secondary_action_label": "",
            "secondary_action_type": "",
            "entity_id": payload["entity_id"],
            "entity_type": payload["entity_type"],
            "route": payload["route"],
            "metadata": payload["metadata"],
            "created_at": now_ms,
            "updated_at": now_ms,
            "acted_at": 0,
        }
    )
    return "created"


def _resolve_stale_prediction_notifications(
    *,
    site_name: str,
    active_entity_ids: set[str],
) -> int:
    pending = list(
        notifications_collection.find(
            {
                "type": PREDICTION_TYPE,
                "site_name": site_name,
                "status": "pending",
                "metadata.prediction_kind": "schedule_delay",
            },
            {"_id": 1, "entity_id": 1},
        )
    )
    stale_ids = [
        doc["_id"]
        for doc in pending
        if str(doc.get("entity_id") or "").strip() not in active_entity_ids
    ]
    if not stale_ids:
        return 0

    result = notifications_collection.update_many(
        {"_id": {"$in": stale_ids}},
        {
            "$set": {
                "status": "resolved",
                "acted_at": _now_ms(),
                "updated_at": _now_ms(),
            }
        },
    )
    return int(result.modified_count or 0)


def sync_prediction_notifications(
    *,
    project_id: str,
    current_user: AuthenticatedUser | None = None,
) -> dict[str, Any]:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        raise HTTPException(status_code=400, detail="project_id is required")

    project = _project_doc(normalized_project_id)
    site_name = _site_name(project, normalized_project_id)
    comparison = work_schedule_comparison(normalized_project_id)
    recipients = _resolve_project_recipients(project, fallback_user=current_user)
    if not recipients:
        raise HTTPException(status_code=400, detail="No project recipients found")

    now = datetime.now()
    prediction_payloads: list[dict[str, Any]] = []
    for activity in comparison.get("activities", []) or []:
        payload = _prediction_payload(activity=activity, site_name=site_name, now=now)
        if payload is not None:
            prediction_payloads.append(payload)

    active_entity_ids = {
        str(payload.get("entity_id") or "").strip()
        for payload in prediction_payloads
        if str(payload.get("entity_id") or "").strip()
    }

    created_count = 0
    updated_count = 0
    skipped_count = 0
    for payload in prediction_payloads:
        for recipient in recipients:
            outcome = _upsert_notification(
                recipient=recipient,
                site_name=site_name,
                payload=payload,
            )
            if outcome == "created":
                created_count += 1
            elif outcome == "updated":
                updated_count += 1
            else:
                skipped_count += 1

    resolved_count = _resolve_stale_prediction_notifications(
        site_name=site_name,
        active_entity_ids=active_entity_ids,
    )

    return {
        "status": "synced",
        "project_id": normalized_project_id,
        "site_name": site_name,
        "prediction_type": "schedule_delay",
        "created_count": created_count,
        "updated_count": updated_count,
        "resolved_count": resolved_count,
        "skipped_count": skipped_count,
        "predictions": prediction_payloads,
    }
