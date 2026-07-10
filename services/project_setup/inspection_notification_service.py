from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from fastapi import HTTPException

from core.auth_context import AuthenticatedUser
from core.database import (
    notifications_collection,
    raw_floorplans_collection,
    raw_inspections_collection,
    raw_users_collection,
)
from services.notifications.push_notification_service import dispatch_notification_push_async
from services.progress.work_schedule.work_schedule_service import (
    parse_work_schedule_date,
)


SYSTEM_SENDER_EMAIL = "system@conscout.local"
SYSTEM_SENDER_NAME = "Conscout System"


def _normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def _normalize_person_token(value: str) -> str:
    return str(value or "").strip().lower()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _project_filter(project_id: str) -> Dict[str, Any]:
    normalized = str(project_id or "").strip()
    return {"$or": [{"site_name": normalized}, {"dxf_project_id": normalized}]}


def _project_doc(project_id: str) -> Dict[str, Any]:
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


def _site_name(project: Dict[str, Any], project_id: str) -> str:
    return str(project.get("site_name") or project.get("dxf_project_id") or project_id).strip()


def _display_name(email: str, name: str) -> str:
    resolved_name = str(name or "").strip()
    if resolved_name:
        return resolved_name
    normalized_email = _normalize_email(email)
    if "@" in normalized_email:
        return normalized_email.split("@", 1)[0]
    return "Team member"


def _resolve_project_users(
    project: Dict[str, Any],
    fallback_user: Optional[AuthenticatedUser] = None,
) -> List[Dict[str, str]]:
    emails: List[str] = []

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

    user_lookup: Dict[str, Dict[str, Any]] = {}
    for user in raw_users_collection.find(
        {"email": {"$in": deduped_emails}},
        {"email": 1, "user_id": 1, "name": 1},
    ):
        email = _normalize_email(str(user.get("email") or ""))
        if email:
            user_lookup[email] = user

    users: List[Dict[str, str]] = []
    for email in deduped_emails:
        user = user_lookup.get(email, {})
        users.append(
            {
                "email": email,
                "user_id": str(user.get("user_id") or "").strip(),
                "name": str(user.get("name") or "").strip(),
            }
        )
    return users


def _user_tokens(user: Dict[str, str]) -> Set[str]:
    email = _normalize_email(user.get("email", ""))
    name = _normalize_person_token(user.get("name", ""))
    tokens: Set[str] = set()
    if email:
        tokens.add(email)
        if "@" in email:
            tokens.add(email.split("@", 1)[0])
    if name:
        tokens.add(name)
    return tokens


def _resolve_user_by_email(
    project_users: List[Dict[str, str]],
    email: str,
) -> Optional[Dict[str, str]]:
    normalized_email = _normalize_email(email)
    if not normalized_email:
        return None

    for user in project_users:
        if _normalize_email(user.get("email", "")) == normalized_email:
            return user

    user = raw_users_collection.find_one(
        {"email": normalized_email},
        {"email": 1, "user_id": 1, "name": 1},
    )
    if not user:
        return None
    return {
        "email": normalized_email,
        "user_id": str(user.get("user_id") or "").strip(),
        "name": str(user.get("name") or "").strip(),
    }


def _resolve_user_by_person(
    project_users: List[Dict[str, str]],
    person_value: str,
) -> Optional[Dict[str, str]]:
    token = _normalize_person_token(person_value)
    if not token:
        return None
    for user in project_users:
        if token in _user_tokens(user):
            return user
    return None


def _parse_date(value: str) -> Optional[datetime]:
    return parse_work_schedule_date(str(value or ""))


def _overdue_days(due_date: Optional[datetime], now: datetime) -> int:
    if due_date is None:
        return 0
    return max((now.date() - due_date.date()).days, 0)


def _is_completed_status(value: str) -> bool:
    normalized = _normalize_person_token(value)
    return normalized in {"completed", "done", "resolved", "closed"}


def _inspection_title(inspection: Dict[str, Any]) -> str:
    return str(inspection.get("title") or "Inspection").strip() or "Inspection"


def _inspection_route(site_name: str) -> str:
    return "/projects/{}/inspection".format(site_name.strip())


def _upsert_notification(
    *,
    recipient: Dict[str, str],
    sender_email: str,
    sender_name: str,
    site_name: str,
    payload: Dict[str, Any],
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
                    "sender_email": sender_email,
                    "sender_name": sender_name,
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

    notification = {
        "type": payload["type"],
        "title": payload["title"],
        "message": payload["message"],
        "site_name": site_name,
        "recipient_email": recipient_email,
        "recipient_user_id": str(recipient.get("user_id") or "").strip(),
        "sender_email": sender_email,
        "sender_name": sender_name,
        "status": "pending",
        "severity": payload["severity"],
        "is_read": False,
        "primary_action_label": "Open inspection",
        "primary_action_type": "open_inspection",
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
    inserted = notifications_collection.insert_one(notification)
    notification["_id"] = inserted.inserted_id
    dispatch_notification_push_async(notification)
    return "created"


def _resolve_notification_for_recipients(
    *,
    site_name: str,
    notification_type: str,
    inspection_id: str,
    keep_recipient_emails: Optional[Set[str]] = None,
) -> int:
    query: Dict[str, Any] = {
        "type": notification_type,
        "site_name": site_name,
        "entity_id": inspection_id,
        "status": "pending",
    }
    if keep_recipient_emails:
        query["recipient_email"] = {"$nin": sorted(keep_recipient_emails)}

    now_ms = _now_ms()
    result = notifications_collection.update_many(
        query,
        {
            "$set": {
                "status": "resolved",
                "acted_at": now_ms,
                "updated_at": now_ms,
            }
        },
    )
    return int(result.modified_count or 0)


def _resolve_stale_delay_notifications(
    *,
    site_name: str,
    active_entity_ids: Set[str],
) -> int:
    pending = list(
        notifications_collection.find(
            {
                "type": "inspection_delay",
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

    now_ms = _now_ms()
    result = notifications_collection.update_many(
        {"_id": {"$in": stale_ids}},
        {
            "$set": {
                "status": "resolved",
                "acted_at": now_ms,
                "updated_at": now_ms,
            }
        },
    )
    return int(result.modified_count or 0)


def create_inspection_assignment_notification(
    *,
    project_id: str,
    inspection: Dict[str, Any],
    sender_email: str,
    sender_name: str,
    current_user: Optional[AuthenticatedUser] = None,
) -> str:
    project = _project_doc(project_id)
    site_name = _site_name(project, project_id)
    project_users = _resolve_project_users(project, fallback_user=current_user)
    assignee = _resolve_user_by_person(project_users, str(inspection.get("assigned_to") or ""))
    if not assignee:
        return "skipped"

    if _normalize_email(assignee.get("email", "")) == _normalize_email(sender_email):
        return "skipped"

    title = _inspection_title(inspection)
    payload = {
        "type": "inspection_assigned",
        "title": "Inspection assigned",
        "message": "{} assigned {} to you in {}.".format(
            sender_name or "A teammate",
            title,
            site_name,
        ),
        "severity": "info",
        "entity_id": str(inspection.get("inspection_id") or "").strip(),
        "entity_type": "inspection",
        "route": _inspection_route(site_name),
        "metadata": {
            "project_name": site_name,
            "inspection_id": str(inspection.get("inspection_id") or "").strip(),
            "inspection_title": title,
            "assigned_to": str(inspection.get("assigned_to") or "").strip(),
            "due_date": str(inspection.get("due_date") or "").strip(),
            "department": str(inspection.get("department") or "").strip(),
        },
    }
    return _upsert_notification(
        recipient=assignee,
        sender_email=_normalize_email(sender_email),
        sender_name=sender_name or "Conscout System",
        site_name=site_name,
        payload=payload,
    )


def create_inspection_completion_notification(
    *,
    project_id: str,
    inspection: Dict[str, Any],
    sender_email: str,
    sender_name: str,
    current_user: Optional[AuthenticatedUser] = None,
) -> str:
    project = _project_doc(project_id)
    site_name = _site_name(project, project_id)
    project_users = _resolve_project_users(project, fallback_user=current_user)
    creator = _resolve_user_by_email(
        project_users,
        str(inspection.get("created_by_email") or ""),
    )
    if not creator:
        return "skipped"

    if _normalize_email(creator.get("email", "")) == _normalize_email(sender_email):
        return "skipped"

    title = _inspection_title(inspection)
    payload = {
        "type": "inspection_completed",
        "title": "Inspection completed",
        "message": "{} marked {} as completed in {}.".format(
            sender_name or "A teammate",
            title,
            site_name,
        ),
        "severity": "success",
        "entity_id": str(inspection.get("inspection_id") or "").strip(),
        "entity_type": "inspection",
        "route": _inspection_route(site_name),
        "metadata": {
            "project_name": site_name,
            "inspection_id": str(inspection.get("inspection_id") or "").strip(),
            "inspection_title": title,
            "assigned_to": str(inspection.get("assigned_to") or "").strip(),
            "due_date": str(inspection.get("due_date") or "").strip(),
            "department": str(inspection.get("department") or "").strip(),
            "completion_by": str(inspection.get("completion_by") or sender_name or "").strip(),
        },
    }
    return _upsert_notification(
        recipient=creator,
        sender_email=_normalize_email(sender_email),
        sender_name=sender_name or "Conscout System",
        site_name=site_name,
        payload=payload,
    )


def sync_inspection_delay_notifications(
    *,
    project_id: str,
    current_user: Optional[AuthenticatedUser] = None,
) -> Dict[str, Any]:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        raise HTTPException(status_code=400, detail="project_id is required")

    project = _project_doc(normalized_project_id)
    site_name = _site_name(project, normalized_project_id)
    project_users = _resolve_project_users(project, fallback_user=current_user)
    inspections = list(raw_inspections_collection.find({"site_name": site_name}))
    now = datetime.now()

    created_count = 0
    updated_count = 0
    resolved_count = 0
    active_entity_ids: Set[str] = set()

    for inspection in inspections:
        inspection_id = str(inspection.get("inspection_id") or "").strip()
        if not inspection_id:
            continue

        if _is_completed_status(str(inspection.get("status") or "")):
            resolved_count += _resolve_notification_for_recipients(
                site_name=site_name,
                notification_type="inspection_delay",
                inspection_id=inspection_id,
            )
            continue

        overdue_days = _overdue_days(_parse_date(str(inspection.get("due_date") or "")), now)
        if overdue_days <= 0:
            resolved_count += _resolve_notification_for_recipients(
                site_name=site_name,
                notification_type="inspection_delay",
                inspection_id=inspection_id,
            )
            continue

        active_entity_ids.add(inspection_id)
        title = _inspection_title(inspection)
        severity = "critical" if overdue_days >= 3 else "warning"
        payload = {
            "type": "inspection_delay",
            "title": "Inspection overdue",
            "message": "{} is overdue by {} day{} in {}.".format(
                title,
                overdue_days,
                "s" if overdue_days != 1 else "",
                site_name,
            ),
            "severity": severity,
            "entity_id": inspection_id,
            "entity_type": "inspection",
            "route": _inspection_route(site_name),
            "metadata": {
                "project_name": site_name,
                "inspection_id": inspection_id,
                "inspection_title": title,
                "assigned_to": str(inspection.get("assigned_to") or "").strip(),
                "created_by_email": str(inspection.get("created_by_email") or "").strip(),
                "due_date": str(inspection.get("due_date") or "").strip(),
                "department": str(inspection.get("department") or "").strip(),
                "overdue_days": overdue_days,
            },
        }

        recipients: List[Dict[str, str]] = []
        creator = _resolve_user_by_email(
            project_users,
            str(inspection.get("created_by_email") or ""),
        )
        if creator:
            recipients.append(creator)

        assignee = _resolve_user_by_person(
            project_users,
            str(inspection.get("assigned_to") or ""),
        )
        if assignee:
            recipients.append(assignee)

        keep_emails: Set[str] = set()
        seen_emails: Set[str] = set()
        for recipient in recipients:
            recipient_email = _normalize_email(recipient.get("email", ""))
            if not recipient_email or recipient_email in seen_emails:
                continue
            seen_emails.add(recipient_email)
            keep_emails.add(recipient_email)
            result = _upsert_notification(
                recipient=recipient,
                sender_email=SYSTEM_SENDER_EMAIL,
                sender_name=SYSTEM_SENDER_NAME,
                site_name=site_name,
                payload=payload,
            )
            if result == "created":
                created_count += 1
            elif result == "updated":
                updated_count += 1

        resolved_count += _resolve_notification_for_recipients(
            site_name=site_name,
            notification_type="inspection_delay",
            inspection_id=inspection_id,
            keep_recipient_emails=keep_emails,
        )

    resolved_count += _resolve_stale_delay_notifications(
        site_name=site_name,
        active_entity_ids=active_entity_ids,
    )

    return {
        "status": "synced",
        "site_name": site_name,
        "created_count": created_count,
        "updated_count": updated_count,
        "resolved_count": resolved_count,
    }
