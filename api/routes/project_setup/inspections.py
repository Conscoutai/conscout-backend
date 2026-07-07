import time
import uuid
from typing import Dict, List, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import ensure_admin_user, require_authenticated_user
from core.auth_context import AuthenticatedUser
from core.database import raw_floorplans_collection, raw_inspections_collection
from services.project_setup.inspection_notification_service import (
    create_inspection_assignment_notification,
    create_inspection_completion_notification,
    sync_inspection_delay_notifications,
)


router = APIRouter(tags=["Inspections"])


class InspectionReplyItem(BaseModel):
    id: str
    type: str = "message"
    author: str
    badge: Optional[str] = None
    message: str = ""
    linked_tours: list[str] = Field(default_factory=list)
    created_at: int


class InspectionCreateRequest(BaseModel):
    title: str
    description: str
    department: str
    assigned_to: str
    due_date: str
    linked_tour: Optional[str] = None
    linked_tours: list[str] = Field(default_factory=list)
    replies: list[InspectionReplyItem] = Field(default_factory=list)
    status: Optional[
        Literal["Pending", "In Progress", "Completed", "Overdue"]
    ] = "Pending"
    link_note: Optional[str] = None
    link_by: Optional[str] = None
    link_at: Optional[int] = None
    completion_note: Optional[str] = None


class InspectionUpdateRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    department: Optional[str] = None
    assigned_to: Optional[str] = None
    due_date: Optional[str] = None
    linked_tour: Optional[str] = None
    linked_tours: Optional[list[str]] = None
    replies: Optional[list[InspectionReplyItem]] = None
    link_note: Optional[str] = None
    link_by: Optional[str] = None
    link_at: Optional[int] = None
    status: Optional[
        Literal["Pending", "In Progress", "Completed", "Overdue"]
    ] = None
    completion_note: Optional[str] = None
    completion_by: Optional[str] = None
    completion_at: Optional[int] = None


def _normalize_string_list(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(cleaned)
    return normalized


def _normalized_site_name(value: str) -> str:
    return str(value or "").strip().lower()


def _can_user_access_site(current_user: AuthenticatedUser, site_name: str) -> bool:
    normalized_site_name = _normalized_site_name(site_name)
    if not normalized_site_name:
        return False

    accessible_sites = {
        _normalized_site_name(value)
        for value in current_user.accessible_project_names
        if str(value or "").strip()
    }
    if normalized_site_name in accessible_sites:
        return True

    floorplan_ids = [
        floorplan_id.strip()
        for floorplan_id in current_user.accessible_floorplan_ids
        if floorplan_id.strip()
    ]
    query = {
        "$and": [
            {
                "$or": [
                    {"site_name": site_name.strip()},
                    {"dxf_project_id": site_name.strip()},
                ]
            },
            {
                "$or": [
                    {"owner_user_id": current_user.user_id},
                    {"owner_email": current_user.email.strip().lower()},
                    {"stakeholder_emails": current_user.email.strip().lower()},
                ]
            },
        ]
    }
    if floorplan_ids:
        query["$and"][1]["$or"].append({"id": {"$in": floorplan_ids}})

    return raw_floorplans_collection.find_one(query, {"_id": 1}) is not None


def _normalize_replies(
    replies: Union[List[InspectionReplyItem], List[Dict]]
) -> List[Dict]:
    normalized: List[Dict] = []
    for item in replies:
        if isinstance(item, InspectionReplyItem):
            payload = item.model_dump()
        else:
            payload = dict(item)
        payload["id"] = str(payload.get("id") or f"reply_{uuid.uuid4().hex}").strip()
        payload["type"] = str(payload.get("type") or "message").strip() or "message"
        payload["author"] = str(payload.get("author") or "").strip()
        payload["badge"] = str(payload.get("badge") or "").strip() or None
        payload["message"] = str(payload.get("message") or "").strip()
        payload["linked_tours"] = _normalize_string_list(
            [str(value) for value in payload.get("linked_tours") or []]
        )
        try:
            payload["created_at"] = int(payload.get("created_at") or int(time.time() * 1000))
        except (TypeError, ValueError):
            payload["created_at"] = int(time.time() * 1000)
        normalized.append(payload)
    normalized.sort(key=lambda item: int(item.get("created_at") or 0))
    return normalized


def _can_user_update_inspection(current_user: AuthenticatedUser, inspection: dict) -> bool:
    email = current_user.email.strip().lower()
    name = current_user.name.strip().lower()
    email_prefix = email.split("@", 1)[0] if email else ""
    assignee = str(inspection.get("assigned_to") or "").strip().lower()
    creator_email = str(inspection.get("created_by_email") or "").strip().lower()
    site_name = str(inspection.get("site_name") or "").strip().lower()
    accessible_sites = {
        value.strip().lower()
        for value in current_user.accessible_project_names
        if value.strip()
    }

    return (
        creator_email == email
        or assignee == name
        or assignee == email_prefix
        or site_name in accessible_sites
    )


def _serialize_inspection(doc: dict) -> dict:
    serialized = dict(doc)
    if "_id" in serialized:
        serialized["_id"] = str(serialized["_id"])
    linked_tours = serialized.get("linked_tours")
    if not isinstance(linked_tours, list):
        linked_tours = []
    linked_tours = _normalize_string_list([str(value) for value in linked_tours])
    if not linked_tours:
        fallback_link = str(serialized.get("linked_tour") or "").strip()
        if fallback_link:
            linked_tours = [fallback_link]
    serialized["linked_tours"] = linked_tours
    serialized["linked_tour"] = linked_tours[-1] if linked_tours else ""
    serialized["replies"] = _normalize_replies(serialized.get("replies") or [])
    return serialized


def _best_effort_inspection_delay_sync(
    site_name: str,
    current_user: Optional[AuthenticatedUser] = None,
) -> dict:
    try:
        result = sync_inspection_delay_notifications(
            project_id=site_name,
            current_user=current_user,
        )
        return {
            "status": "synced",
            "created_count": int(result.get("created_count") or 0),
            "updated_count": int(result.get("updated_count") or 0),
            "resolved_count": int(result.get("resolved_count") or 0),
        }
    except Exception as error:
        return {
            "status": "skipped",
            "detail": str(error),
        }


@router.get("/projects/{site_name}/inspections")
def list_project_inspections(
    site_name: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    if not _can_user_access_site(current_user, site_name):
        raise HTTPException(status_code=403, detail="Not allowed to access this project")
    _best_effort_inspection_delay_sync(site_name, current_user=current_user)
    docs = list(
        raw_inspections_collection.find({"site_name": site_name}).sort(
            [("updated_at", -1), ("created_at", -1)]
        )
    )
    return [_serialize_inspection(doc) for doc in docs]


@router.post("/projects/{site_name}/inspections")
def create_project_inspection(
    site_name: str,
    payload: InspectionCreateRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    if not _can_user_access_site(current_user, site_name):
        raise HTTPException(status_code=403, detail="Not allowed to access this project")
    now = int(time.time() * 1000)
    linked_tours = _normalize_string_list(
        [*payload.linked_tours, *(([payload.linked_tour]) if payload.linked_tour else [])]
    )
    replies = _normalize_replies(payload.replies)
    inspection = {
        "inspection_id": f"inspection_{uuid.uuid4().hex}",
        "site_name": site_name.strip(),
        "title": payload.title.strip(),
        "description": payload.description.strip(),
        "department": payload.department.strip(),
        "assigned_to": payload.assigned_to.strip(),
        "due_date": payload.due_date.strip(),
        "linked_tours": linked_tours,
        "linked_tour": linked_tours[-1] if linked_tours else "",
        "replies": replies,
        "link_note": (payload.link_note or "").strip(),
        "link_by": (payload.link_by or "").strip(),
        "link_at": payload.link_at,
        "status": (payload.status or "Pending").strip(),
        "completion_note": (payload.completion_note or "").strip(),
        "completion_by": "",
        "completion_at": None,
        "created_by": current_user.name.strip()
        or current_user.email.split("@")[0].strip(),
        "created_by_email": current_user.email.strip().lower(),
        "owner_user_id": current_user.user_id,
        "owner_email": current_user.email.strip().lower(),
        "created_at": now,
        "updated_at": now,
    }
    result = raw_inspections_collection.insert_one(inspection)
    inspection["_id"] = str(result.inserted_id)
    create_inspection_assignment_notification(
        project_id=site_name,
        inspection=inspection,
        sender_email=current_user.email,
        sender_name=current_user.name.strip() or current_user.email.split("@")[0].strip(),
        current_user=current_user,
    )
    _best_effort_inspection_delay_sync(site_name, current_user=current_user)
    return _serialize_inspection(inspection)


@router.put("/projects/{site_name}/inspections/{inspection_id}")
def update_project_inspection(
    site_name: str,
    inspection_id: str,
    payload: InspectionUpdateRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    if not _can_user_access_site(current_user, site_name):
        raise HTTPException(status_code=403, detail="Not allowed to access this project")

    existing = raw_inspections_collection.find_one(
        {
            "site_name": site_name,
            "inspection_id": inspection_id,
        }
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Inspection not found")
    if not _can_user_update_inspection(current_user, existing):
        raise HTTPException(status_code=403, detail="Not allowed to update this inspection")

    update_fields = {
        key: value.strip() if isinstance(value, str) else value
        for key, value in payload.dict(exclude_none=True).items()
    }
    if not update_fields:
        raise HTTPException(status_code=400, detail="No inspection fields provided")

    if "linked_tours" in update_fields:
        update_fields["linked_tours"] = _normalize_string_list(update_fields["linked_tours"])
        update_fields["linked_tour"] = (
            update_fields["linked_tours"][-1] if update_fields["linked_tours"] else ""
        )
    elif "linked_tour" in update_fields:
        linked_tours = _normalize_string_list(
            [*(existing.get("linked_tours") or []), update_fields.get("linked_tour") or ""]
        )
        update_fields["linked_tours"] = linked_tours
        update_fields["linked_tour"] = linked_tours[-1] if linked_tours else ""

    if "replies" in update_fields:
        update_fields["replies"] = _normalize_replies(update_fields["replies"])

    update_fields["updated_at"] = int(time.time() * 1000)

    result = raw_inspections_collection.update_one(
        {
            "site_name": site_name,
            "inspection_id": inspection_id,
        },
        {"$set": update_fields},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Inspection not found")

    updated = raw_inspections_collection.find_one(
        {"site_name": site_name, "inspection_id": inspection_id}
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Inspection not found")

    assignment_changed = (
        str(existing.get("assigned_to") or "").strip().lower()
        != str(updated.get("assigned_to") or "").strip().lower()
    )
    completed_before = str(existing.get("status") or "").strip().lower() == "completed"
    completed_after = str(updated.get("status") or "").strip().lower() == "completed"
    actor_name = current_user.name.strip() or current_user.email.split("@")[0].strip()
    if assignment_changed:
        create_inspection_assignment_notification(
            project_id=site_name,
            inspection=updated,
            sender_email=current_user.email,
            sender_name=actor_name,
            current_user=current_user,
        )
    if completed_after and not completed_before:
        create_inspection_completion_notification(
            project_id=site_name,
            inspection=updated,
            sender_email=current_user.email,
            sender_name=actor_name,
            current_user=current_user,
        )
    _best_effort_inspection_delay_sync(site_name, current_user=current_user)
    return _serialize_inspection(updated)


@router.delete("/projects/{site_name}/inspections/{inspection_id}")
def delete_project_inspection(
    site_name: str,
    inspection_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    if not _can_user_access_site(current_user, site_name):
        raise HTTPException(status_code=403, detail="Not allowed to access this project")

    result = raw_inspections_collection.delete_one(
        {"site_name": site_name, "inspection_id": inspection_id}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Inspection not found")
    return {"status": "deleted", "inspection_id": inspection_id}
