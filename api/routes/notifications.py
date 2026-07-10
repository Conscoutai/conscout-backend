from __future__ import annotations

import time

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import ensure_admin_user, require_authenticated_user
from core.auth_context import AuthenticatedUser
from core.database import (
    notifications_collection,
    raw_floorplans_collection,
    raw_users_collection,
)
from services.features.comments.comment_notification_service import (
    sync_comment_delay_notifications,
)
from services.project_setup.team_member_notification_service import (
    notify_team_member_added,
)
from services.project_setup.inspection_notification_service import (
    sync_inspection_delay_notifications,
)
from services.project_setup.safety_notification_service import (
    sync_safety_issue_notifications,
)
from services.progress.weekly_progress_notification_service import (
    sync_weekly_progress_notifications,
)
from services.progress.prediction_notification_service import (
    sync_prediction_notifications,
)
from services.notifications.push_notification_service import (
    dispatch_notification_push_async,
    register_device_token,
)


router = APIRouter(prefix="/notifications", tags=["Notifications"])


class ProjectInviteRequest(BaseModel):
    site_name: str
    recipient_email: str


class RegisterDeviceRequest(BaseModel):
    fcm_token: str
    platform: str = "unknown"
    app: str = "main"


class WeeklyProgressSyncRequest(BaseModel):
    project_id: str = ""


class PredictionSyncRequest(BaseModel):
    project_id: str


class SafetySyncRequest(BaseModel):
    project_id: str


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _display_name(user: AuthenticatedUser) -> str:
    name = user.name.strip()
    if name:
        return name
    email = user.email.strip().lower()
    if "@" in email:
        return email.split("@", 1)[0]
    return "Team member"


def _serialize_notification(doc: dict) -> dict:
    return {
        "id": str(doc.get("_id", "")),
        "type": str(doc.get("type") or ""),
        "title": str(doc.get("title") or ""),
        "message": str(doc.get("message") or ""),
        "site_name": str(doc.get("site_name") or ""),
        "recipient_email": str(doc.get("recipient_email") or ""),
        "sender_email": str(doc.get("sender_email") or ""),
        "sender_name": str(doc.get("sender_name") or ""),
        "status": str(doc.get("status") or "pending"),
        "severity": str(doc.get("severity") or ""),
        "is_read": bool(doc.get("is_read") is True),
        "created_at": int(doc.get("created_at") or 0),
        "updated_at": int(doc.get("updated_at") or doc.get("created_at") or 0),
        "acted_at": int(doc.get("acted_at") or 0),
        "primary_action_label": str(doc.get("primary_action_label") or ""),
        "primary_action_type": str(doc.get("primary_action_type") or ""),
        "secondary_action_label": str(doc.get("secondary_action_label") or ""),
        "secondary_action_type": str(doc.get("secondary_action_type") or ""),
        "entity_id": str(doc.get("entity_id") or ""),
        "entity_type": str(doc.get("entity_type") or ""),
        "route": str(doc.get("route") or ""),
        "metadata": doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {},
    }


def _best_effort_inspection_notification_sync(
    current_user: AuthenticatedUser,
) -> dict:
    synced_projects = 0
    created_count = 0
    updated_count = 0
    resolved_count = 0
    for project_name in current_user.accessible_project_names:
        normalized_project_name = str(project_name or "").strip()
        if not normalized_project_name:
            continue
        try:
            result = sync_inspection_delay_notifications(
                project_id=normalized_project_name,
                current_user=current_user,
            )
            synced_projects += 1
            created_count += int(result.get("created_count") or 0)
            updated_count += int(result.get("updated_count") or 0)
            resolved_count += int(result.get("resolved_count") or 0)
        except Exception:
            continue
    return {
        "synced_projects": synced_projects,
        "created_count": created_count,
        "updated_count": updated_count,
        "resolved_count": resolved_count,
    }


def _best_effort_comment_notification_sync(
    current_user: AuthenticatedUser,
) -> dict:
    synced_projects = 0
    created_count = 0
    updated_count = 0
    resolved_count = 0
    for project_name in current_user.accessible_project_names:
        normalized_project_name = str(project_name or "").strip()
        if not normalized_project_name:
            continue
        try:
            result = sync_comment_delay_notifications(
                project_id=normalized_project_name,
                current_user=current_user,
            )
            synced_projects += 1
            created_count += int(result.get("created_count") or 0)
            updated_count += int(result.get("updated_count") or 0)
            resolved_count += int(result.get("resolved_count") or 0)
        except Exception:
            continue
    return {
        "synced_projects": synced_projects,
        "created_count": created_count,
        "updated_count": updated_count,
        "resolved_count": resolved_count,
    }


def _best_effort_prediction_notification_sync(
    current_user: AuthenticatedUser,
) -> dict:
    synced_projects = 0
    created_count = 0
    updated_count = 0
    resolved_count = 0
    for project_name in current_user.accessible_project_names:
        normalized_project_name = str(project_name or "").strip()
        if not normalized_project_name:
            continue
        try:
            result = sync_prediction_notifications(
                project_id=normalized_project_name,
                current_user=current_user,
            )
            synced_projects += 1
            created_count += int(result.get("created_count") or 0)
            updated_count += int(result.get("updated_count") or 0)
            resolved_count += int(result.get("resolved_count") or 0)
        except Exception:
            continue
    return {
        "synced_projects": synced_projects,
        "created_count": created_count,
        "updated_count": updated_count,
        "resolved_count": resolved_count,
    }


def _best_effort_safety_notification_sync(
    current_user: AuthenticatedUser,
) -> dict:
    synced_projects = 0
    created_count = 0
    updated_count = 0
    resolved_count = 0
    for project_name in current_user.accessible_project_names:
        normalized_project_name = str(project_name or "").strip()
        if not normalized_project_name:
            continue
        try:
            result = sync_safety_issue_notifications(
                project_id=normalized_project_name,
                current_user=current_user,
            )
            synced_projects += 1
            created_count += int(result.get("created_count") or 0)
            updated_count += int(result.get("updated_count") or 0)
            resolved_count += int(result.get("resolved_count") or 0)
        except Exception:
            continue
    return {
        "synced_projects": synced_projects,
        "created_count": created_count,
        "updated_count": updated_count,
        "resolved_count": resolved_count,
    }


def _recipient_filter(current_user: AuthenticatedUser) -> dict:
    normalized_email = _normalize_email(current_user.email)
    return {
        "$or": [
            {"recipient_user_id": current_user.user_id},
            {"recipient_email": normalized_email},
        ]
    }


def _find_notification_for_current_user(
    notification_id: str,
    current_user: AuthenticatedUser,
) -> dict:
    try:
        object_id = ObjectId(notification_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid notification id") from exc

    notification = notifications_collection.find_one(
        {
            "_id": object_id,
            **_recipient_filter(current_user),
        }
    )
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    return notification


@router.get("")
def list_notifications(
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _best_effort_inspection_notification_sync(current_user)
    _best_effort_comment_notification_sync(current_user)
    _best_effort_prediction_notification_sync(current_user)
    _best_effort_safety_notification_sync(current_user)
    records = list(
        notifications_collection.find(_recipient_filter(current_user)).sort(
            "created_at",
            -1,
        )
    )
    return {"notifications": [_serialize_notification(record) for record in records]}


@router.get("/unread-count")
def unread_notification_count(
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    count = notifications_collection.count_documents(
        {
            **_recipient_filter(current_user),
            "status": "pending",
            "is_read": {"$ne": True},
        }
    )
    return {"count": count}


@router.post("/register-device")
def register_notification_device(
    payload: RegisterDeviceRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    fcm_token = payload.fcm_token.strip()
    if not fcm_token:
        raise HTTPException(status_code=400, detail="fcm_token is required")
    result = register_device_token(
        user_id=current_user.user_id,
        email=current_user.email,
        fcm_token=fcm_token,
        platform=payload.platform,
        app=payload.app,
    )
    return {"message": "Notification device registered", **result}


@router.post("/{notification_id}/read")
def mark_notification_as_read(
    notification_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    notification = _find_notification_for_current_user(notification_id, current_user)
    notifications_collection.update_one(
        {"_id": notification["_id"]},
        {
            "$set": {
                "is_read": True,
                "read_at": _now_ms(),
                "updated_at": _now_ms(),
            }
        },
    )
    updated = notifications_collection.find_one({"_id": notification["_id"]}) or notification
    return {
        "message": "Notification marked as read",
        "notification": _serialize_notification(updated),
    }


@router.post("/project-invites")
def create_project_invite_notification(
    payload: ProjectInviteRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    site_name = payload.site_name.strip()
    recipient_email = _normalize_email(payload.recipient_email)

    if not site_name:
        raise HTTPException(status_code=400, detail="Project name is required")
    if not recipient_email:
        raise HTTPException(status_code=400, detail="Recipient email is required")
    if recipient_email == _normalize_email(current_user.email):
        raise HTTPException(status_code=400, detail="You cannot invite yourself")

    project = raw_floorplans_collection.find_one(
        {"$or": [{"site_name": site_name}, {"dxf_project_id": site_name}]},
        {"site_name": 1, "dxf_project_id": 1, "stakeholder_emails": 1},
        sort=[("_id", -1)],
    )
    if not project:
        raise HTTPException(status_code=404, detail="No floorplan found for this project")

    stakeholder_emails = {
        _normalize_email(str(email))
        for email in project.get("stakeholder_emails", [])
        if str(email).strip()
    }
    if recipient_email in stakeholder_emails:
        raise HTTPException(status_code=400, detail="User already has project access")

    recipient_user = raw_users_collection.find_one(
        {"email": recipient_email},
        {"user_id": 1, "email": 1},
    )
    if not recipient_user:
        raise HTTPException(status_code=404, detail="No registered user found for this email")

    existing = notifications_collection.find_one(
        {
            "type": "project_invite",
            "status": "pending",
            "site_name": site_name,
            "recipient_email": recipient_email,
        }
    )
    if existing:
        return {
            "message": "Invite already pending",
            "notification": _serialize_notification(existing),
        }

    notification = {
        "type": "project_invite",
        "title": "Project access invite",
        "message": f"{_display_name(current_user)} invited you to join {site_name}.",
        "site_name": site_name,
        "recipient_email": recipient_email,
        "recipient_user_id": str(recipient_user.get("user_id") or "").strip(),
        "sender_email": _normalize_email(current_user.email),
        "sender_name": _display_name(current_user),
        "status": "pending",
        "severity": "info",
        "is_read": False,
        "primary_action_label": "Accept access",
        "primary_action_type": "accept_invite",
        "secondary_action_label": "Decline",
        "secondary_action_type": "reject_invite",
        "entity_id": site_name,
        "entity_type": "project",
        "route": f"/projects/{site_name}",
        "metadata": {"project_name": site_name},
        "created_at": _now_ms(),
        "updated_at": _now_ms(),
        "acted_at": 0,
    }
    inserted = notifications_collection.insert_one(notification)
    created = notifications_collection.find_one({"_id": inserted.inserted_id}) or notification
    dispatch_notification_push_async(created)
    return {
        "message": "Invite notification sent",
        "notification": _serialize_notification(created),
    }


@router.post("/weekly-progress/sync")
def manual_weekly_progress_sync(
    payload: WeeklyProgressSyncRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return sync_weekly_progress_notifications(
        project_id=payload.project_id.strip() or None,
    )


@router.post("/predictions/sync")
def manual_prediction_sync(
    payload: PredictionSyncRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return sync_prediction_notifications(
        project_id=payload.project_id.strip(),
        current_user=current_user,
    )


@router.post("/safety/sync")
def manual_safety_sync(
    payload: SafetySyncRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return sync_safety_issue_notifications(
        project_id=payload.project_id.strip(),
        current_user=current_user,
    )


@router.post("/{notification_id}/accept")
def accept_project_invite(
    notification_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    notification = _find_notification_for_current_user(notification_id, current_user)
    if str(notification.get("type") or "") != "project_invite":
        raise HTTPException(status_code=400, detail="Unsupported notification type")
    if str(notification.get("status") or "") != "pending":
        raise HTTPException(status_code=400, detail="Invite is no longer pending")

    site_name = str(notification.get("site_name") or "").strip()
    if not site_name:
        raise HTTPException(status_code=400, detail="Notification is missing a project")

    update = raw_floorplans_collection.update_many(
        {"$or": [{"site_name": site_name}, {"dxf_project_id": site_name}]},
        {"$addToSet": {"stakeholder_emails": _normalize_email(current_user.email)}},
    )
    if update.matched_count == 0:
        raise HTTPException(status_code=404, detail="No floorplan found for this project")

    team_member_notification = {
        "created_count": 0,
        "duplicate_count": 0,
        "skipped_count": 0,
    }
    if int(update.modified_count or 0) > 0:
        team_member_notification = notify_team_member_added(
            site_name=site_name,
            added_member_email=current_user.email,
            current_user=current_user,
        )

    notifications_collection.update_one(
        {"_id": notification["_id"]},
        {
            "$set": {
                "status": "accepted",
                "is_read": True,
                "acted_at": _now_ms(),
                "updated_at": _now_ms(),
            }
        },
    )
    updated = notifications_collection.find_one({"_id": notification["_id"]}) or notification
    return {
        "message": "Project access accepted",
        "site_name": site_name,
        "notification": _serialize_notification(updated),
        "team_member_notification": team_member_notification,
    }


@router.post("/{notification_id}/reject")
def reject_project_invite(
    notification_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    notification = _find_notification_for_current_user(notification_id, current_user)
    if str(notification.get("type") or "") != "project_invite":
        raise HTTPException(status_code=400, detail="Unsupported notification type")
    if str(notification.get("status") or "") != "pending":
        raise HTTPException(status_code=400, detail="Invite is no longer pending")

    notifications_collection.update_one(
        {"_id": notification["_id"]},
        {
            "$set": {
                "status": "rejected",
                "is_read": True,
                "acted_at": _now_ms(),
                "updated_at": _now_ms(),
            }
        },
    )
    updated = notifications_collection.find_one({"_id": notification["_id"]}) or notification
    return {
        "message": "Invite declined",
        "notification": _serialize_notification(updated),
    }
