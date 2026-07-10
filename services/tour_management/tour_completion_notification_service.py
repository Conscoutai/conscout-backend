from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from core.auth_context import AuthenticatedUser
from core.database import (
    notifications_collection,
    raw_floorplans_collection,
    raw_users_collection,
)
from services.notifications.push_notification_service import dispatch_notification_push_async


def _normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def _display_name(user: AuthenticatedUser) -> str:
    name = str(user.name or "").strip()
    if name:
        return name
    email = _normalize_email(user.email)
    if "@" in email:
        return email.split("@", 1)[0]
    return "Team member"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _project_doc(
    *,
    site_name: str,
    floorplan_id: str,
    floorplan: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if isinstance(floorplan, dict) and floorplan:
        return floorplan

    normalized_site_name = str(site_name or "").strip()
    normalized_floorplan_id = str(floorplan_id or "").strip()

    query: Dict[str, Any] = {}
    clauses: List[Dict[str, Any]] = []
    if normalized_floorplan_id:
        clauses.append({"id": normalized_floorplan_id})
    if normalized_site_name:
        clauses.extend(
            [
                {"site_name": normalized_site_name},
                {"dxf_project_id": normalized_site_name},
            ]
        )

    if not clauses:
        return {}
    if len(clauses) == 1:
        query = clauses[0]
    else:
        query = {"$or": clauses}

    return raw_floorplans_collection.find_one(
        query,
        {
            "id": 1,
            "site_name": 1,
            "dxf_project_id": 1,
            "owner_email": 1,
            "owner_user_id": 1,
            "stakeholder_emails": 1,
        },
        sort=[("_id", -1)],
    ) or {}


def _site_name(
    *,
    tour: Dict[str, Any],
    project: Dict[str, Any],
) -> str:
    return str(
        tour.get("site_name")
        or project.get("site_name")
        or project.get("dxf_project_id")
        or ""
    ).strip()


def _resolve_project_users(project: Dict[str, Any]) -> List[Dict[str, str]]:
    emails: List[str] = []

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

    user_lookup: Dict[str, Dict[str, Any]] = {}
    for user in raw_users_collection.find(
        {"email": {"$in": deduped_emails}},
        {"email": 1, "user_id": 1, "name": 1},
    ):
        normalized_email = _normalize_email(str(user.get("email") or ""))
        if normalized_email:
            user_lookup[normalized_email] = user

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


def _tour_route(site_name: str, tour_id: str) -> str:
    return "/projects/{}/tours/{}".format(site_name.strip(), str(tour_id or "").strip())


def notify_tour_completion(
    *,
    tour: Dict[str, Any],
    floorplan: Optional[Dict[str, Any]],
    current_user: AuthenticatedUser,
) -> Dict[str, int]:
    normalized_tour_id = str(tour.get("tour_id") or "").strip()
    if not normalized_tour_id:
        return {
            "created_count": 0,
            "duplicate_count": 0,
            "skipped_count": 0,
        }

    project = _project_doc(
        site_name=str(tour.get("site_name") or "").strip(),
        floorplan_id=str(tour.get("floorplan_id") or "").strip(),
        floorplan=floorplan,
    )
    resolved_site_name = _site_name(tour=tour, project=project)
    if not resolved_site_name:
        return {
            "created_count": 0,
            "duplicate_count": 0,
            "skipped_count": 0,
        }

    sender_email = _normalize_email(current_user.email)
    sender_name = _display_name(current_user)
    tour_name = str(tour.get("name") or "Tour").strip() or "Tour"
    route = _tour_route(resolved_site_name, normalized_tour_id)
    metadata = {
        "project_name": resolved_site_name,
        "site_name": resolved_site_name,
        "tour_id": normalized_tour_id,
        "tour_name": tour_name,
        "actor_name": sender_name,
        "actor_email": sender_email,
    }

    created_count = 0
    duplicate_count = 0
    skipped_count = 0

    for recipient in _resolve_project_users(project):
        recipient_email = _normalize_email(recipient.get("email", ""))
        if not recipient_email or recipient_email == sender_email:
            skipped_count += 1
            continue

        existing = notifications_collection.find_one(
            {
                "type": "tour_completion",
                "site_name": resolved_site_name,
                "recipient_email": recipient_email,
                "entity_id": normalized_tour_id,
            },
            sort=[("created_at", -1)],
        )
        if existing:
            duplicate_count += 1
            continue

        now_ms = _now_ms()
        notification = {
            "type": "tour_completion",
            "title": "Tour completed",
            "message": f"{sender_name} completed {tour_name} in {resolved_site_name}.",
            "site_name": resolved_site_name,
            "recipient_email": recipient_email,
            "recipient_user_id": str(recipient.get("user_id") or "").strip(),
            "sender_email": sender_email,
            "sender_name": sender_name,
            "status": "pending",
            "severity": "success",
            "is_read": False,
            "primary_action_label": "Open tour",
            "primary_action_type": "open_tour",
            "secondary_action_label": "",
            "secondary_action_type": "",
            "entity_id": normalized_tour_id,
            "entity_type": "tour",
            "route": route,
            "metadata": metadata,
            "created_at": now_ms,
            "updated_at": now_ms,
            "acted_at": 0,
        }
        inserted = notifications_collection.insert_one(notification)
        notification["_id"] = inserted.inserted_id
        dispatch_notification_push_async(notification)
        created_count += 1

    return {
        "created_count": created_count,
        "duplicate_count": duplicate_count,
        "skipped_count": skipped_count,
    }
