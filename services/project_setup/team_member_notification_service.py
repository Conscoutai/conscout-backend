from __future__ import annotations

import time
from typing import Any, Dict, List

from core.auth_context import AuthenticatedUser
from core.database import (
    notifications_collection,
    raw_floorplans_collection,
    raw_users_collection,
)


def _normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _display_name_from_email(email: str) -> str:
    normalized_email = _normalize_email(email)
    if "@" in normalized_email:
        return normalized_email.split("@", 1)[0]
    return "Team member"


def _display_name(user: AuthenticatedUser) -> str:
    name = str(user.name or "").strip()
    if name:
        return name
    return _display_name_from_email(user.email)


def _project_doc(site_name: str) -> Dict[str, Any]:
    normalized_site_name = str(site_name or "").strip()
    if not normalized_site_name:
        return {}

    return raw_floorplans_collection.find_one(
        {
            "$or": [
                {"site_name": normalized_site_name},
                {"dxf_project_id": normalized_site_name},
            ]
        },
        {
            "site_name": 1,
            "dxf_project_id": 1,
            "owner_email": 1,
            "stakeholder_emails": 1,
        },
        sort=[("_id", -1)],
    ) or {}


def _resolve_user_name(email: str) -> str:
    normalized_email = _normalize_email(email)
    if not normalized_email:
        return ""

    user = raw_users_collection.find_one(
        {"email": normalized_email},
        {"name": 1, "email": 1},
    )
    resolved_name = str((user or {}).get("name") or "").strip()
    if resolved_name:
        return resolved_name
    return _display_name_from_email(normalized_email)


def _resolve_recipients(project: Dict[str, Any]) -> List[Dict[str, str]]:
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
        {"email": 1, "user_id": 1},
    ):
        normalized_email = _normalize_email(str(user.get("email") or ""))
        if normalized_email:
            user_lookup[normalized_email] = user

    recipients: List[Dict[str, str]] = []
    for email in deduped_emails:
        user = user_lookup.get(email, {})
        recipients.append(
            {
                "email": email,
                "user_id": str(user.get("user_id") or "").strip(),
            }
        )
    return recipients


def notify_team_member_added(
    *,
    site_name: str,
    added_member_email: str,
    current_user: AuthenticatedUser,
) -> Dict[str, int]:
    normalized_site_name = str(site_name or "").strip()
    normalized_added_member_email = _normalize_email(added_member_email)
    actor_email = _normalize_email(current_user.email)
    if not normalized_site_name or not normalized_added_member_email:
        return {
            "created_count": 0,
            "duplicate_count": 0,
            "skipped_count": 0,
        }

    project = _project_doc(normalized_site_name)
    if not project:
        return {
            "created_count": 0,
            "duplicate_count": 0,
            "skipped_count": 0,
        }

    resolved_site_name = str(
        project.get("site_name") or project.get("dxf_project_id") or normalized_site_name
    ).strip()
    actor_name = _display_name(current_user)
    added_member_name = _resolve_user_name(normalized_added_member_email)
    if actor_email == normalized_added_member_email:
        message = f"{added_member_name} joined {resolved_site_name}."
    else:
        message = f"{actor_name} added {added_member_name} to {resolved_site_name}."

    route = f"/projects/{resolved_site_name}/team"
    metadata = {
        "project_name": resolved_site_name,
        "site_name": resolved_site_name,
        "member_email": normalized_added_member_email,
        "member_name": added_member_name,
        "actor_email": actor_email,
        "actor_name": actor_name,
    }

    created_count = 0
    duplicate_count = 0
    skipped_count = 0

    for recipient in _resolve_recipients(project):
        recipient_email = _normalize_email(recipient.get("email", ""))
        if not recipient_email or recipient_email == actor_email:
            skipped_count += 1
            continue

        existing = notifications_collection.find_one(
            {
                "type": "team_member_added",
                "site_name": resolved_site_name,
                "recipient_email": recipient_email,
                "entity_id": normalized_added_member_email,
            },
            sort=[("created_at", -1)],
        )
        if existing:
            duplicate_count += 1
            continue

        now_ms = _now_ms()
        notifications_collection.insert_one(
            {
                "type": "team_member_added",
                "title": "Team member added",
                "message": message,
                "site_name": resolved_site_name,
                "recipient_email": recipient_email,
                "recipient_user_id": str(recipient.get("user_id") or "").strip(),
                "sender_email": actor_email,
                "sender_name": actor_name,
                "status": "pending",
                "severity": "success",
                "is_read": False,
                "primary_action_label": "Open team",
                "primary_action_type": "open_team",
                "secondary_action_label": "",
                "secondary_action_type": "",
                "entity_id": normalized_added_member_email,
                "entity_type": "team_member",
                "route": route,
                "metadata": metadata,
                "created_at": now_ms,
                "updated_at": now_ms,
                "acted_at": 0,
            }
        )
        created_count += 1

    return {
        "created_count": created_count,
        "duplicate_count": duplicate_count,
        "skipped_count": skipped_count,
    }
