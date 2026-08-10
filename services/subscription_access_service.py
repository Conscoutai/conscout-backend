from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from core.config import APP_SURFACE
from core.database import raw_floorplans_collection, raw_users_collection
from core.subscription_plans import get_subscription_plan


_FREE_PROJECT_LIMIT = 1
_FREE_TRIAL_DAYS = 30
_PROJECT_CREATION_LEASE_MINUTES = 60


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    normalized = _clean(value)
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _owned_project_documents(user: dict[str, Any]) -> list[dict[str, Any]]:
    user_id = _clean(user.get("user_id"))
    email = _clean(user.get("email")).lower()
    owner_clauses = []
    if user_id:
        owner_clauses.append({"owner_user_id": user_id})
    if email:
        owner_clauses.extend(
            [
                {"owner_email": email},
                {"created_by_email": email},
            ]
        )
    if not owner_clauses:
        return []
    return list(
        raw_floorplans_collection.find(
            {"$or": owner_clauses},
            {
                "_id": 1,
                "id": 1,
                "project_id": 1,
                "dxf_project_id": 1,
                "site_name": 1,
            },
        )
    )


def _project_identity(project: dict[str, Any]) -> str:
    return (
        _clean(project.get("project_id"))
        or _clean(project.get("id"))
        or _clean(project.get("dxf_project_id"))
        or _clean(project.get("_id"))
    )


def _owned_project_count(projects: list[dict[str, Any]]) -> int:
    identities = {
        _project_identity(project) for project in projects if _project_identity(project)
    }
    return len(identities)


def _is_existing_owned_project(
    projects: list[dict[str, Any]],
    requested_project_id: str,
) -> bool:
    normalized_project_id = _clean(requested_project_id)
    if not normalized_project_id:
        return False
    return any(
        normalized_project_id
        in {
            _clean(project.get("project_id")),
            _clean(project.get("id")),
            _clean(project.get("dxf_project_id")),
        }
        for project in projects
    )


def _has_current_paid_entitlement(
    user: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    subscription = (
        user.get("subscription") if isinstance(user.get("subscription"), dict) else {}
    )
    period_end = _parse_datetime(subscription.get("current_period_end"))
    return (
        _clean(subscription.get("status")).lower() == "active"
        and _clean(subscription.get("payment_status")).lower() == "paid"
        and get_subscription_plan(subscription.get("plan_code")) is not None
        and period_end is not None
        and period_end > now
    )


def _paid_project_limit(
    user: dict[str, Any],
    *,
    now: datetime,
) -> Optional[int]:
    if not _has_current_paid_entitlement(user, now=now):
        return _FREE_PROJECT_LIMIT

    subscription = user["subscription"]
    plan = get_subscription_plan(subscription.get("plan_code"))
    assert plan is not None
    return plan["project_limit"]


def _ensure_free_trial_is_available(
    user: dict[str, Any],
    *,
    now: datetime,
) -> None:
    if _has_current_paid_entitlement(user, now=now):
        return

    trial_started_at = _parse_datetime(user.get("trial_started_at")) or _parse_datetime(
        user.get("created_at")
    )
    if trial_started_at and now > trial_started_at + timedelta(days=_FREE_TRIAL_DAYS):
        raise HTTPException(
            status_code=403,
            detail=(
                "Your 30-day Lite workspace access has ended. "
                "Complete a Moyasar subscription payment to create another project."
            ),
        )


def reserve_lite_project_creation(
    *,
    user_id: str,
    requested_project_id: str = "",
    now: Optional[datetime] = None,
) -> str:
    if APP_SURFACE != "lite":
        return ""

    user = raw_users_collection.find_one({"user_id": _clean(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User account was not found.")
    if user.get("is_subscription_admin") is True:
        return ""

    reference = (now or _utc_now()).astimezone(timezone.utc)
    projects = _owned_project_documents(user)
    if _is_existing_owned_project(projects, requested_project_id):
        return ""

    _ensure_free_trial_is_available(user, now=reference)
    project_limit = _paid_project_limit(user, now=reference)
    if project_limit is None:
        return ""

    lease_token = str(uuid4())
    lease_until = (
        reference + timedelta(minutes=_PROJECT_CREATION_LEASE_MINUTES)
    ).isoformat()
    claim = raw_users_collection.update_one(
        {
            "_id": user["_id"],
            "$or": [
                {"subscription.project_creation_lock_token": {"$in": ["", None]}},
                {
                    "subscription.project_creation_lease_until": {
                        "$lte": reference.isoformat()
                    }
                },
            ],
        },
        {
            "$set": {
                "subscription.project_creation_lock_token": lease_token,
                "subscription.project_creation_lease_until": lease_until,
            }
        },
    )
    if claim.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Another project is currently being created for this account.",
        )

    current_project_count = _owned_project_count(
        _owned_project_documents(
            raw_users_collection.find_one({"_id": user["_id"]}) or user
        )
    )
    if current_project_count >= project_limit:
        release_lite_project_creation(user_id=user_id, lease_token=lease_token)
        raise HTTPException(
            status_code=403,
            detail=(
                f"Your current Lite plan allows {project_limit} "
                f"{'project' if project_limit == 1 else 'projects'}. "
                "Complete a Moyasar payment for a larger plan before creating another project."
            ),
        )
    return lease_token


def release_lite_project_creation(*, user_id: str, lease_token: str) -> None:
    normalized_lease_token = _clean(lease_token)
    if not normalized_lease_token:
        return
    raw_users_collection.update_one(
        {
            "user_id": _clean(user_id),
            "subscription.project_creation_lock_token": normalized_lease_token,
        },
        {
            "$set": {
                "subscription.project_creation_lock_token": "",
                "subscription.project_creation_lease_until": "",
            }
        },
    )
