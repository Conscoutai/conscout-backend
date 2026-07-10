from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException

from core.auth_context import AuthenticatedUser
from core.database import (
    notifications_collection,
    raw_floorplans_collection,
    raw_inspections_collection,
    raw_tours_collection,
    raw_users_collection,
)
from services.notifications.push_notification_service import dispatch_notification_push_async


SYSTEM_SENDER_EMAIL = "system@conscout.local"
SYSTEM_SENDER_NAME = "Conscout System"
SAFETY_NOTIFICATION_TYPE = "safety_issue"

SAFETY_KEYWORDS = {
    "accident",
    "barricade",
    "blocked exit",
    "chemical",
    "compliance",
    "confined space",
    "danger",
    "dangerous",
    "electrical panel",
    "emergency exit",
    "edge protection",
    "extinguisher",
    "fall",
    "fire",
    "gas",
    "guardrail",
    "harness",
    "hazard",
    "helmet",
    "hot work",
    "injury",
    "leakage",
    "live wire",
    "missing ppe",
    "permit",
    "ppe",
    "safety",
    "scaffold",
    "smoke",
    "spill",
    "unsafe",
}
RISK_KEYWORDS = {"critical", "danger", "high", "urgent"}
CLOSED_STATUSES = {"closed", "completed", "complete", "done", "resolved"}


def _normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def _normalize_token(value: Any) -> str:
    return str(value or "").strip().lower()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _project_filter(project_id: str) -> dict[str, Any]:
    normalized = str(project_id or "").strip()
    return {"$or": [{"site_name": normalized}, {"dxf_project_id": normalized}]}


def _project_doc(project_id: str) -> dict[str, Any]:
    project = raw_floorplans_collection.find_one(
        _project_filter(project_id),
        {
            "id": 1,
            "site_name": 1,
            "dxf_project_id": 1,
            "stakeholder_emails": 1,
            "owner_email": 1,
            "owner_user_id": 1,
        },
        sort=[("_id", -1)],
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _site_name(project: dict[str, Any], project_id: str) -> str:
    return str(project.get("site_name") or project.get("dxf_project_id") or project_id).strip()


def _project_users(
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

    users: list[dict[str, str]] = []
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


def _user_tokens(user: dict[str, str]) -> set[str]:
    email = _normalize_email(user.get("email", ""))
    name = _normalize_token(user.get("name", ""))
    tokens: set[str] = set()
    if email:
        tokens.add(email)
        if "@" in email:
            tokens.add(email.split("@", 1)[0])
    if name:
        tokens.add(name)
    return tokens


def _resolve_user_by_email(users: list[dict[str, str]], email: str) -> dict[str, str] | None:
    normalized = _normalize_email(email)
    if not normalized:
        return None
    for user in users:
        if _normalize_email(user.get("email", "")) == normalized:
            return user
    found = raw_users_collection.find_one(
        {"email": normalized},
        {"email": 1, "user_id": 1, "name": 1},
    )
    if not found:
        return None
    return {
        "email": normalized,
        "user_id": str(found.get("user_id") or "").strip(),
        "name": str(found.get("name") or "").strip(),
    }


def _resolve_user_by_person(users: list[dict[str, str]], value: str) -> dict[str, str] | None:
    token = _normalize_token(value)
    if not token:
        return None
    for user in users:
        if token in _user_tokens(user):
            return user
    return None


def _first_text(source: dict[str, Any], keys: tuple[str, ...], fallback: str) -> str:
    for key in keys:
        value = str(source.get(key) or "").strip()
        if value:
            return value
    return fallback


def _combined_text(source: dict[str, Any], keys: tuple[str, ...]) -> str:
    values = []
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        parsed = str(value).strip()
        if parsed:
            values.append(parsed)
    return " ".join(values).lower()


def _is_closed(value: Any) -> bool:
    return _normalize_token(value) in CLOSED_STATUSES


def _safety_match(source: dict[str, Any], keys: tuple[str, ...]) -> tuple[bool, str]:
    text = _combined_text(source, keys)
    if text:
        for keyword in sorted(SAFETY_KEYWORDS, key=len, reverse=True):
            if keyword in text:
                return True, keyword
    risk_level = _normalize_token(source.get("risk_level") or source.get("riskLevel"))
    if risk_level in RISK_KEYWORDS:
        return True, risk_level
    return False, ""


def _comment_status(comment: dict[str, Any]) -> str:
    return _first_text(
        comment,
        ("status", "state", "issue_type", "issueType", "type"),
        "",
    )


def _comment_assignee(comment: dict[str, Any]) -> str:
    return _first_text(
        comment,
        (
            "assigned_to",
            "assignedTo",
            "response_by",
            "responseBy",
            "responsible_party",
            "responsibleParty",
        ),
        "",
    )


def _comment_title(comment: dict[str, Any]) -> str:
    return _first_text(
        comment,
        ("title", "subject", "problem_description", "description", "comment"),
        "Safety issue",
    )


def _comment_location(comment: dict[str, Any], node: dict[str, Any] | None) -> str:
    direct = _first_text(
        comment,
        ("location", "area", "location_area", "locationArea", "zone"),
        "",
    )
    if direct:
        return direct
    if node is None:
        return ""
    return str(node.get("id") or node.get("name") or "").strip()


def _comment_route(site_name: str, tour_id: str) -> str:
    normalized_tour_id = str(tour_id or "").strip()
    if normalized_tour_id:
        return f"/projects/{site_name}/tours/{normalized_tour_id}"
    return f"/projects/{site_name}"


def _inspection_title(inspection: dict[str, Any]) -> str:
    return _first_text(inspection, ("title", "description", "department"), "Safety inspection")


def _inspection_route(site_name: str) -> str:
    return f"/projects/{site_name}/inspection"


def _dedup_recipients(recipients: list[dict[str, str]]) -> list[dict[str, str]]:
    resolved: list[dict[str, str]] = []
    seen: set[str] = set()
    for recipient in recipients:
        email = _normalize_email(recipient.get("email", ""))
        if not email or email in seen:
            continue
        seen.add(email)
        resolved.append({**recipient, "email": email})
    return resolved


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
            "type": SAFETY_NOTIFICATION_TYPE,
            "site_name": site_name,
            "recipient_email": recipient_email,
            "entity_id": payload["entity_id"],
            "status": "pending",
        },
        sort=[("created_at", -1)],
    )

    fields = {
        "title": payload["title"],
        "message": payload["message"],
        "severity": payload["severity"],
        "entity_type": payload["entity_type"],
        "route": payload["route"],
        "metadata": payload["metadata"],
        "is_read": False,
        "updated_at": now_ms,
    }
    if existing:
        notifications_collection.update_one({"_id": existing["_id"]}, {"$set": fields})
        return "updated"

    notification = {
        "type": SAFETY_NOTIFICATION_TYPE,
        **fields,
        "site_name": site_name,
        "recipient_email": recipient_email,
        "recipient_user_id": str(recipient.get("user_id") or "").strip(),
        "sender_email": SYSTEM_SENDER_EMAIL,
        "sender_name": SYSTEM_SENDER_NAME,
        "status": "pending",
        "primary_action_label": "Open issue",
        "primary_action_type": "open_safety_issue",
        "secondary_action_label": "",
        "secondary_action_type": "",
        "entity_id": payload["entity_id"],
        "created_at": now_ms,
        "acted_at": 0,
    }
    inserted = notifications_collection.insert_one(notification)
    notification["_id"] = inserted.inserted_id
    dispatch_notification_push_async(notification)
    return "created"


def _resolve_notifications_for_source(
    *,
    site_name: str,
    entity_id: str,
    keep_recipient_emails: set[str] | None = None,
) -> int:
    query: dict[str, Any] = {
        "type": SAFETY_NOTIFICATION_TYPE,
        "site_name": site_name,
        "entity_id": entity_id,
        "status": "pending",
    }
    if keep_recipient_emails:
        query["recipient_email"] = {"$nin": sorted(keep_recipient_emails)}

    result = notifications_collection.update_many(
        query,
        {
            "$set": {
                "status": "resolved",
                "acted_at": _now_ms(),
                "updated_at": _now_ms(),
            }
        },
    )
    return int(result.modified_count or 0)


def _resolve_stale_notifications(
    *,
    site_name: str,
    active_entity_ids: set[str],
) -> int:
    pending = list(
        notifications_collection.find(
            {
                "type": SAFETY_NOTIFICATION_TYPE,
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


def _project_tour_query(project: dict[str, Any], site_name: str) -> dict[str, Any]:
    floorplan_id = str(project.get("id") or "").strip()
    clauses: list[dict[str, Any]] = [
        {"site_name": site_name},
        {"site": site_name},
        {"project_id": site_name},
    ]
    if floorplan_id:
        clauses.append({"floorplan_id": floorplan_id})
    return {"$or": clauses}


def _comment_payload(
    *,
    site_name: str,
    tour: dict[str, Any],
    node: dict[str, Any] | None,
    comment: dict[str, Any],
    matched_keyword: str,
) -> dict[str, Any]:
    comment_id = str(comment.get("id") or "").strip()
    title = _comment_title(comment)
    location = _comment_location(comment, node)
    pano_id = str(comment.get("pano_id") or "").strip()
    if not pano_id and node is not None:
        pano_id = str(node.get("id") or "").strip()
    risk_level = _first_text(
        comment,
        ("risk_level", "riskLevel", "severity", "priority"),
        "Critical",
    )
    location_suffix = f" at {location}" if location else ""
    return {
        "title": "Safety compliance issue",
        "message": f"{title}{location_suffix} in {site_name}.",
        "severity": "critical",
        "entity_id": comment_id,
        "entity_type": "comment",
        "route": _comment_route(site_name, str(tour.get("tour_id") or "")),
        "metadata": {
            "project_name": site_name,
            "issue_id": comment_id,
            "comment_id": comment_id,
            "comment_title": title,
            "issue_category": _first_text(
                comment,
                ("issue_type", "issueType", "type", "department"),
                matched_keyword or "Safety",
            ),
            "location": location,
            "risk_level": risk_level,
            "source": "comment",
            "matched_keyword": matched_keyword,
            "tour_id": str(tour.get("tour_id") or "").strip(),
            "pano_id": pano_id,
            "assigned_to": _comment_assignee(comment),
        },
    }


def _inspection_payload(
    *,
    site_name: str,
    inspection: dict[str, Any],
    matched_keyword: str,
) -> dict[str, Any]:
    inspection_id = str(inspection.get("inspection_id") or "").strip()
    title = _inspection_title(inspection)
    department = str(inspection.get("department") or "").strip()
    location = _first_text(inspection, ("location", "area", "zone"), department)
    location_suffix = f" at {location}" if location else ""
    return {
        "title": "Safety compliance issue",
        "message": f"{title}{location_suffix} in {site_name}.",
        "severity": "critical",
        "entity_id": inspection_id,
        "entity_type": "inspection",
        "route": _inspection_route(site_name),
        "metadata": {
            "project_name": site_name,
            "issue_id": inspection_id,
            "inspection_id": inspection_id,
            "inspection_title": title,
            "issue_category": department or matched_keyword or "Safety",
            "location": location,
            "risk_level": "Critical",
            "source": "inspection",
            "matched_keyword": matched_keyword,
            "assigned_to": str(inspection.get("assigned_to") or "").strip(),
            "due_date": str(inspection.get("due_date") or "").strip(),
        },
    }


def sync_safety_issue_notifications(
    *,
    project_id: str,
    current_user: AuthenticatedUser | None = None,
) -> dict[str, Any]:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        raise HTTPException(status_code=400, detail="project_id is required")

    project = _project_doc(normalized_project_id)
    site_name = _site_name(project, normalized_project_id)
    project_users = _project_users(project, fallback_user=current_user)

    created_count = 0
    updated_count = 0
    resolved_count = 0
    active_entity_ids: set[str] = set()

    for tour in raw_tours_collection.find(
        _project_tour_query(project, site_name),
        {"tour_id": 1, "site_name": 1, "site": 1, "project_id": 1, "nodes": 1},
    ):
        for node in tour.get("nodes", []) or []:
            if not isinstance(node, dict):
                continue
            for comment in node.get("comments", []) or []:
                if not isinstance(comment, dict):
                    continue
                comment_id = str(comment.get("id") or "").strip()
                if not comment_id:
                    continue

                matched, keyword = _safety_match(
                    comment,
                    (
                        "title",
                        "description",
                        "department",
                        "severity",
                        "priority",
                        "risk_level",
                        "riskLevel",
                        "issue_type",
                        "issueType",
                        "type",
                        "problem_description",
                        "action_required",
                        "root_cause",
                    ),
                )
                if _is_closed(_comment_status(comment)) or not matched:
                    resolved_count += _resolve_notifications_for_source(
                        site_name=site_name,
                        entity_id=comment_id,
                    )
                    continue

                active_entity_ids.add(comment_id)
                payload = _comment_payload(
                    site_name=site_name,
                    tour=tour,
                    node=node,
                    comment=comment,
                    matched_keyword=keyword,
                )
                recipients = [
                    *project_users,
                    *filter(
                        None,
                        [
                            _resolve_user_by_email(
                                project_users,
                                str(comment.get("created_by_email") or ""),
                            ),
                            _resolve_user_by_person(project_users, _comment_assignee(comment)),
                        ],
                    ),
                ]
                keep_emails: set[str] = set()
                for recipient in _dedup_recipients(recipients):
                    keep_emails.add(recipient["email"])
                    outcome = _upsert_notification(
                        recipient=recipient,
                        site_name=site_name,
                        payload=payload,
                    )
                    if outcome == "created":
                        created_count += 1
                    elif outcome == "updated":
                        updated_count += 1

                resolved_count += _resolve_notifications_for_source(
                    site_name=site_name,
                    entity_id=comment_id,
                    keep_recipient_emails=keep_emails,
                )

    for inspection in raw_inspections_collection.find({"site_name": site_name}):
        inspection_id = str(inspection.get("inspection_id") or "").strip()
        if not inspection_id:
            continue
        matched, keyword = _safety_match(
            inspection,
            (
                "title",
                "description",
                "department",
                "status",
                "completion_note",
                "link_note",
            ),
        )
        if _is_closed(inspection.get("status")) or not matched:
            resolved_count += _resolve_notifications_for_source(
                site_name=site_name,
                entity_id=inspection_id,
            )
            continue

        active_entity_ids.add(inspection_id)
        payload = _inspection_payload(
            site_name=site_name,
            inspection=inspection,
            matched_keyword=keyword,
        )
        recipients = [
            *project_users,
            *filter(
                None,
                [
                    _resolve_user_by_email(
                        project_users,
                        str(inspection.get("created_by_email") or ""),
                    ),
                    _resolve_user_by_person(
                        project_users,
                        str(inspection.get("assigned_to") or ""),
                    ),
                ],
            ),
        ]
        keep_emails = set()
        for recipient in _dedup_recipients(recipients):
            keep_emails.add(recipient["email"])
            outcome = _upsert_notification(
                recipient=recipient,
                site_name=site_name,
                payload=payload,
            )
            if outcome == "created":
                created_count += 1
            elif outcome == "updated":
                updated_count += 1

        resolved_count += _resolve_notifications_for_source(
            site_name=site_name,
            entity_id=inspection_id,
            keep_recipient_emails=keep_emails,
        )

    resolved_count += _resolve_stale_notifications(
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
