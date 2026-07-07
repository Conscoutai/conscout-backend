from __future__ import annotations

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
PROGRESS_GAP_THRESHOLD = 20.0


def _normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _project_filter(project_id: str) -> dict[str, Any]:
    normalized = (project_id or "").strip()
    return {"$or": [{"site_name": normalized}, {"dxf_project_id": normalized}]}


def _parse_date(value: str) -> datetime | None:
    return parse_work_schedule_date(str(value or ""))


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


def _overdue_days(end_date: datetime | None, now: datetime) -> int:
    if end_date is None:
        return 0
    return max((now.date() - end_date.date()).days, 0)


def _delay_days(start_date: datetime | None, now: datetime) -> int:
    if start_date is None:
        return 0
    return max((now.date() - start_date.date()).days, 0)


def _activity_notification_payload(
    activity: dict[str, Any],
    now: datetime,
) -> dict[str, Any] | None:
    activity_name = str(activity.get("activity_name") or "Work activity").strip()
    activity_id = str(activity.get("activity_id") or activity_name).strip()
    start_date = _parse_date(activity.get("start_date", ""))
    end_date = _parse_date(activity.get("end_date", ""))
    status = str(activity.get("primary_status") or activity.get("status") or "").strip().upper()
    planned_percent = float(activity.get("planned_percent") or 0)
    actual_percent = float(activity.get("actual_percent") or 0)
    progress_gap = round(max(planned_percent - actual_percent, 0), 2)
    evidence = activity.get("evidence") or []

    overdue_days = _overdue_days(end_date, now)
    if overdue_days > 0 and status != "DONE":
        severity = "critical" if overdue_days >= 3 else "warning"
        return {
            "type": "work_activity_delay",
            "entity_id": activity_id,
            "entity_type": "work_activity",
            "severity": severity,
            "title": "Work activity overdue",
            "message": f"{activity_name} is overdue by {overdue_days} day{'s' if overdue_days != 1 else ''}.",
            "route": f"/projects/{activity.get('site_name') or ''}/progress/schedule",
            "metadata": {
                "subtype": "overdue",
                "activity_id": activity_id,
                "activity_name": activity_name,
                "zone": str(activity.get("zone") or "").strip(),
                "start_date": str(activity.get("start_date") or "").strip(),
                "end_date": str(activity.get("end_date") or "").strip(),
                "planned_percent": planned_percent,
                "actual_percent": actual_percent,
                "progress_gap": progress_gap,
                "overdue_days": overdue_days,
                "evidence_count": len(evidence),
                "related_tour_ids": activity.get("related_tour_ids") or [],
            },
        }

    started_late_days = _delay_days(start_date, now)
    if started_late_days > 0 and status == "NOT STARTED":
        return {
            "type": "work_activity_delay",
            "entity_id": activity_id,
            "entity_type": "work_activity",
            "severity": "warning",
            "title": "Work activity not started",
            "message": f"{activity_name} should have started {started_late_days} day{'s' if started_late_days != 1 else ''} ago, but no progress was detected.",
            "route": f"/projects/{activity.get('site_name') or ''}/progress/schedule",
            "metadata": {
                "subtype": "not_started",
                "activity_id": activity_id,
                "activity_name": activity_name,
                "zone": str(activity.get("zone") or "").strip(),
                "start_date": str(activity.get("start_date") or "").strip(),
                "end_date": str(activity.get("end_date") or "").strip(),
                "planned_percent": planned_percent,
                "actual_percent": actual_percent,
                "progress_gap": progress_gap,
                "delay_days": started_late_days,
                "evidence_count": len(evidence),
                "related_tour_ids": activity.get("related_tour_ids") or [],
            },
        }

    if progress_gap >= PROGRESS_GAP_THRESHOLD and status != "DONE":
        return {
            "type": "work_activity_delay",
            "entity_id": activity_id,
            "entity_type": "work_activity",
            "severity": "warning",
            "title": "Work activity behind plan",
            "message": f"{activity_name} is behind plan by {progress_gap:.0f}%.",
            "route": f"/projects/{activity.get('site_name') or ''}/progress/schedule",
            "metadata": {
                "subtype": "progress_gap",
                "activity_id": activity_id,
                "activity_name": activity_name,
                "zone": str(activity.get("zone") or "").strip(),
                "start_date": str(activity.get("start_date") or "").strip(),
                "end_date": str(activity.get("end_date") or "").strip(),
                "planned_percent": planned_percent,
                "actual_percent": actual_percent,
                "progress_gap": progress_gap,
                "evidence_count": len(evidence),
                "related_tour_ids": activity.get("related_tour_ids") or [],
            },
        }

    return None


def _summary_notification_payload(
    site_name: str,
    activity_alerts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not activity_alerts:
        return None

    critical_count = sum(1 for alert in activity_alerts if alert.get("severity") == "critical")
    warning_count = len(activity_alerts) - critical_count
    severity = "critical" if critical_count > 0 else "warning"
    activity_count = len(activity_alerts)
    message = (
        f"{activity_count} work activit{'y' if activity_count == 1 else 'ies'} need attention in {site_name}."
    )
    if critical_count > 0:
        message = (
            f"{critical_count} critical and {warning_count} warning work activit"
            f"{'y' if activity_count == 1 else 'ies'} need attention in {site_name}."
        )

    return {
        "type": "schedule_delay",
        "entity_id": site_name,
        "entity_type": "project_schedule",
        "severity": severity,
        "title": "Schedule delay detected",
        "message": message,
        "route": f"/projects/{site_name}/progress/schedule",
        "metadata": {
            "project_name": site_name,
            "delayed_activities_count": activity_count,
            "critical_activities_count": critical_count,
            "warning_activities_count": warning_count,
        },
    }


def _upsert_notification(
    *,
    recipient: dict[str, str],
    site_name: str,
    payload: dict[str, Any],
) -> str:
    existing = notifications_collection.find_one(
        {
            "type": payload["type"],
            "site_name": site_name,
            "recipient_email": recipient["email"],
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
                    "updated_at": _now_ms(),
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
            "recipient_email": recipient["email"],
            "recipient_user_id": recipient["user_id"],
            "sender_email": SYSTEM_SENDER_EMAIL,
            "sender_name": SYSTEM_SENDER_NAME,
            "status": "pending",
            "severity": payload["severity"],
            "is_read": False,
            "primary_action_label": "Review schedule",
            "primary_action_type": "open_schedule",
            "entity_id": payload["entity_id"],
            "entity_type": payload["entity_type"],
            "route": payload["route"],
            "metadata": payload["metadata"],
            "created_at": _now_ms(),
            "updated_at": _now_ms(),
            "acted_at": 0,
        }
    )
    return "created"


def _resolve_stale_notifications(
    *,
    site_name: str,
    notification_type: str,
    active_entity_ids: set[str],
) -> int:
    pending = list(
        notifications_collection.find(
            {
                "type": notification_type,
                "site_name": site_name,
                "status": "pending",
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


def sync_schedule_delay_notifications(
    *,
    project_id: str,
    current_user: AuthenticatedUser | None = None,
) -> dict[str, Any]:
    normalized_project_id = (project_id or "").strip()
    if not normalized_project_id:
        raise HTTPException(status_code=400, detail="project_id is required")

    project = _project_doc(normalized_project_id)
    site_name = _site_name(project, normalized_project_id)
    comparison = work_schedule_comparison(normalized_project_id)
    recipients = _resolve_project_recipients(project, current_user)
    if not recipients:
        raise HTTPException(status_code=400, detail="No project recipients found")

    now = datetime.now()
    activity_payloads: list[dict[str, Any]] = []
    for activity in comparison.get("activities", []) or []:
        enriched = {**activity, "site_name": site_name}
        payload = _activity_notification_payload(enriched, now)
        if payload is not None:
            activity_payloads.append(payload)

    activity_active_ids = {
        str(payload.get("entity_id") or "").strip()
        for payload in activity_payloads
        if str(payload.get("entity_id") or "").strip()
    }

    created_count = 0
    updated_count = 0
    for payload in activity_payloads:
        for recipient in recipients:
            outcome = _upsert_notification(
                recipient=recipient,
                site_name=site_name,
                payload=payload,
            )
            if outcome == "created":
                created_count += 1
            else:
                updated_count += 1

    resolved_count = _resolve_stale_notifications(
        site_name=site_name,
        notification_type="work_activity_delay",
        active_entity_ids=activity_active_ids,
    )

    summary_payload = _summary_notification_payload(site_name, activity_payloads)
    summary_active_ids: set[str] = set()
    if summary_payload is not None:
        summary_active_ids.add(str(summary_payload["entity_id"]))
        for recipient in recipients:
            outcome = _upsert_notification(
                recipient=recipient,
                site_name=site_name,
                payload=summary_payload,
            )
            if outcome == "created":
                created_count += 1
            else:
                updated_count += 1

    resolved_count += _resolve_stale_notifications(
        site_name=site_name,
        notification_type="schedule_delay",
        active_entity_ids=summary_active_ids,
    )

    return {
        "project_id": normalized_project_id,
        "site_name": site_name,
        "recipients": [
            {
                "email": recipient["email"],
                "user_id": recipient["user_id"],
            }
            for recipient in recipients
        ],
        "activity_alerts": activity_payloads,
        "summary_alert": summary_payload,
        "created_count": created_count,
        "updated_count": updated_count,
        "resolved_count": resolved_count,
    }
