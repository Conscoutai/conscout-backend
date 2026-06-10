from __future__ import annotations

import os
import shutil
from typing import Any

from core.config import DATA_DIR, user_data_dir
from core.database import (
    raw_floorplans_collection,
    raw_inspections_collection,
    raw_notifications_collection,
    raw_tours_collection,
    raw_users_collection,
    raw_work_schedules_collection,
)


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _owned_documents_filter(user: dict[str, Any]) -> dict[str, Any]:
    user_id = str(user.get("user_id") or "").strip()
    email = _normalize_email(str(user.get("email") or ""))

    clauses: list[dict[str, Any]] = []
    if user_id:
        clauses.append({"owner_user_id": user_id})
    if email:
        clauses.append({"owner_email": email})
        clauses.append({"created_by_email": email})

    if not clauses:
        return {"_id": None}
    if len(clauses) == 1:
        return clauses[0]
    return {"$or": clauses}


def _notification_filter(user: dict[str, Any]) -> dict[str, Any]:
    user_id = str(user.get("user_id") or "").strip()
    email = _normalize_email(str(user.get("email") or ""))

    clauses: list[dict[str, Any]] = []
    if user_id:
        clauses.extend(
            [
                {"recipient_user_id": user_id},
                {"sender_user_id": user_id},
            ]
        )
    if email:
        clauses.extend(
            [
                {"recipient_email": email},
                {"sender_email": email},
            ]
        )

    if not clauses:
        return {"_id": None}
    if len(clauses) == 1:
        return clauses[0]
    return {"$or": clauses}


def _safe_remove_user_data_dir(user: dict[str, Any]) -> bool:
    target_dir = user_data_dir(
        owner_email=str(user.get("email") or ""),
        owner_user_id=str(user.get("user_id") or ""),
    )
    if not target_dir:
        return False

    base_dir = os.path.abspath(DATA_DIR)
    resolved_target = os.path.abspath(target_dir)
    if resolved_target == base_dir:
        return False
    if not resolved_target.startswith(base_dir + os.sep):
        return False
    if not os.path.isdir(resolved_target):
        return False

    shutil.rmtree(resolved_target, ignore_errors=True)
    return True


def delete_user_account(user: dict[str, Any]) -> dict[str, Any]:
    owned_filter = _owned_documents_filter(user)
    normalized_email = _normalize_email(str(user.get("email") or ""))

    sites_deleted = raw_floorplans_collection.delete_many(owned_filter).deleted_count
    tours_deleted = raw_tours_collection.delete_many(owned_filter).deleted_count
    work_schedules_deleted = raw_work_schedules_collection.delete_many(
        owned_filter
    ).deleted_count
    inspections_deleted = raw_inspections_collection.delete_many(
        owned_filter
    ).deleted_count

    stakeholder_refs_removed = 0
    if normalized_email:
        stakeholder_refs_removed = raw_floorplans_collection.update_many(
            {"stakeholder_emails": normalized_email},
            {"$pull": {"stakeholder_emails": normalized_email}},
        ).modified_count

    notifications_deleted = raw_notifications_collection.delete_many(
        _notification_filter(user)
    ).deleted_count
    users_deleted = raw_users_collection.delete_many(
        {"user_id": str(user.get("user_id") or "").strip()}
    ).deleted_count
    files_deleted = _safe_remove_user_data_dir(user)

    return {
        "sites_deleted": sites_deleted,
        "tours_deleted": tours_deleted,
        "work_schedules_deleted": work_schedules_deleted,
        "inspections_deleted": inspections_deleted,
        "notifications_deleted": notifications_deleted,
        "stakeholder_refs_removed": stakeholder_refs_removed,
        "users_deleted": users_deleted,
        "files_deleted": files_deleted,
    }
