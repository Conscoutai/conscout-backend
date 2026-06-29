from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth import ensure_subscription_admin_user, require_authenticated_user
from core.auth_context import AuthenticatedUser
from core.database import raw_subscription_requests_collection, raw_users_collection


router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])

_PENDING_STATUSES = {"pending", "pending_approval"}
_REVIEWABLE_STATUSES = {"approved", "rejected"}


class SubscriptionRequestPayload(BaseModel):
    plan_code: str = Field(..., min_length=1)
    plan_name: str = Field(..., min_length=1)
    monthly_price_usd: int = Field(..., ge=0)
    project_limit: Optional[int] = Field(default=None, ge=1)
    company_name: str = Field(..., min_length=1)
    billing_contact_name: str = Field(..., min_length=1)
    billing_email: str = Field(..., min_length=1)
    phone: str = ""
    tax_id: str = ""
    team_size: Optional[int] = Field(default=None, ge=1)
    requested_project_count: Optional[int] = Field(default=None, ge=1)
    notes: str = ""


class SubscriptionReviewPayload(BaseModel):
    review_note: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_email(value: Any) -> str:
    return _clean_text(value).lower()


def _normalize_status(value: Any) -> str:
    normalized = _clean_text(value).lower()
    if normalized in _PENDING_STATUSES:
        return "pending_approval"
    if normalized in _REVIEWABLE_STATUSES:
        return normalized
    return "pending_approval"


def _normalize_subscription(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    normalized = dict(raw)
    normalized["plan_code"] = _clean_text(raw.get("plan_code")).lower()
    normalized["plan_name"] = _clean_text(raw.get("plan_name"))
    return normalized


def _normalize_request_document(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    normalized = dict(raw)
    normalized["request_id"] = _clean_text(raw.get("request_id"))
    normalized["user_id"] = _clean_text(raw.get("user_id"))
    normalized["user_email"] = _normalize_email(raw.get("user_email"))
    normalized["user_name"] = _clean_text(raw.get("user_name"))
    normalized["workspace"] = _clean_text(raw.get("workspace"))
    normalized["plan_code"] = _clean_text(raw.get("plan_code")).lower()
    normalized["plan_name"] = _clean_text(raw.get("plan_name"))
    normalized["current_plan_code"] = _clean_text(raw.get("current_plan_code")).lower()
    normalized["current_plan_name"] = _clean_text(raw.get("current_plan_name"))
    normalized["company_name"] = _clean_text(raw.get("company_name"))
    normalized["billing_contact_name"] = _clean_text(raw.get("billing_contact_name"))
    normalized["billing_email"] = _normalize_email(raw.get("billing_email"))
    normalized["phone"] = _clean_text(raw.get("phone"))
    normalized["tax_id"] = _clean_text(raw.get("tax_id"))
    normalized["notes"] = _clean_text(raw.get("notes"))
    normalized["status"] = _normalize_status(raw.get("status"))
    normalized["source"] = _clean_text(raw.get("source")) or "landing_pricing"
    normalized["requested_at"] = _clean_text(raw.get("requested_at"))
    normalized["updated_at"] = _clean_text(raw.get("updated_at"))
    normalized["reviewed_at"] = _clean_text(raw.get("reviewed_at"))
    normalized["review_note"] = _clean_text(raw.get("review_note"))
    normalized["reviewed_by_user_id"] = _clean_text(raw.get("reviewed_by_user_id"))
    normalized["reviewed_by_email"] = _normalize_email(raw.get("reviewed_by_email"))
    return normalized


def _public_subscription(raw: Any) -> dict[str, Any]:
    normalized = _normalize_subscription(raw)
    if not normalized:
        return {}
    return {
        "plan_code": normalized.get("plan_code", ""),
        "plan_name": normalized.get("plan_name", ""),
        "monthly_price_usd": normalized.get("monthly_price_usd"),
        "project_limit": normalized.get("project_limit"),
        "status": _clean_text(normalized.get("status")),
        "payment_status": _clean_text(normalized.get("payment_status")),
        "activated_at": _clean_text(normalized.get("activated_at")),
        "approved_at": _clean_text(normalized.get("approved_at")),
        "approved_by_email": _normalize_email(normalized.get("approved_by_email")),
        "request_id": _clean_text(normalized.get("request_id")),
    }


def _public_request(raw: Any) -> dict[str, Any]:
    normalized = _normalize_request_document(raw)
    if not normalized:
        return {}
    return {
        "request_id": normalized.get("request_id", ""),
        "user_id": normalized.get("user_id", ""),
        "user_email": normalized.get("user_email", ""),
        "user_name": normalized.get("user_name", ""),
        "workspace": normalized.get("workspace", ""),
        "current_plan_code": normalized.get("current_plan_code", ""),
        "current_plan_name": normalized.get("current_plan_name", ""),
        "plan_code": normalized.get("plan_code", ""),
        "plan_name": normalized.get("plan_name", ""),
        "monthly_price_usd": normalized.get("monthly_price_usd"),
        "project_limit": normalized.get("project_limit"),
        "company_name": normalized.get("company_name", ""),
        "billing_contact_name": normalized.get("billing_contact_name", ""),
        "billing_email": normalized.get("billing_email", ""),
        "phone": normalized.get("phone", ""),
        "tax_id": normalized.get("tax_id", ""),
        "team_size": normalized.get("team_size"),
        "requested_project_count": normalized.get("requested_project_count"),
        "notes": normalized.get("notes", ""),
        "status": normalized.get("status", "pending_approval"),
        "source": normalized.get("source", "landing_pricing"),
        "requested_at": normalized.get("requested_at", ""),
        "updated_at": normalized.get("updated_at", ""),
        "reviewed_at": normalized.get("reviewed_at", ""),
        "review_note": normalized.get("review_note", ""),
        "reviewed_by_user_id": normalized.get("reviewed_by_user_id", ""),
        "reviewed_by_email": normalized.get("reviewed_by_email", ""),
    }


def _build_pending_request(
    *,
    request_id: str,
    user: dict[str, Any],
    payload: SubscriptionRequestPayload,
    requested_at: str,
    updated_at: str,
) -> dict[str, Any]:
    current_subscription = _normalize_subscription(user.get("subscription"))
    current_plan_code = _clean_text(current_subscription.get("plan_code")).lower()
    current_plan_name = _clean_text(current_subscription.get("plan_name"))
    if not current_plan_code:
        current_plan_code = "starter_access"
    if not current_plan_name:
        current_plan_name = "Starter Access"

    return {
        "request_id": request_id,
        "user_id": _clean_text(user.get("user_id")),
        "user_email": _normalize_email(user.get("email")),
        "user_name": _clean_text(user.get("name")),
        "workspace": _clean_text(user.get("workspace")),
        "current_plan_code": current_plan_code,
        "current_plan_name": current_plan_name,
        "plan_code": _clean_text(payload.plan_code).lower(),
        "plan_name": _clean_text(payload.plan_name),
        "monthly_price_usd": payload.monthly_price_usd,
        "project_limit": payload.project_limit,
        "company_name": _clean_text(payload.company_name),
        "billing_contact_name": _clean_text(payload.billing_contact_name),
        "billing_email": _normalize_email(payload.billing_email),
        "phone": _clean_text(payload.phone),
        "tax_id": _clean_text(payload.tax_id),
        "team_size": payload.team_size,
        "requested_project_count": payload.requested_project_count,
        "notes": _clean_text(payload.notes),
        "status": "pending_approval",
        "source": "landing_pricing",
        "requested_at": requested_at,
        "updated_at": updated_at,
        "reviewed_at": "",
        "review_note": "",
        "reviewed_by_user_id": "",
        "reviewed_by_email": "",
    }


def _build_active_subscription(
    request_doc: dict[str, Any],
    *,
    approved_by: AuthenticatedUser,
    approved_at: str,
) -> dict[str, Any]:
    return {
        "plan_code": _clean_text(request_doc.get("plan_code")).lower(),
        "plan_name": _clean_text(request_doc.get("plan_name")),
        "monthly_price_usd": request_doc.get("monthly_price_usd"),
        "project_limit": request_doc.get("project_limit"),
        "company_name": _clean_text(request_doc.get("company_name")),
        "billing_contact_name": _clean_text(request_doc.get("billing_contact_name")),
        "billing_email": _normalize_email(request_doc.get("billing_email")),
        "phone": _clean_text(request_doc.get("phone")),
        "tax_id": _clean_text(request_doc.get("tax_id")),
        "team_size": request_doc.get("team_size"),
        "requested_project_count": request_doc.get("requested_project_count"),
        "notes": _clean_text(request_doc.get("notes")),
        "status": "active",
        "payment_status": "approved",
        "source": "admin_approval",
        "activated_at": approved_at,
        "approved_at": approved_at,
        "approved_by_user_id": approved_by.user_id,
        "approved_by_email": approved_by.email,
        "request_id": _clean_text(request_doc.get("request_id")),
    }


def _find_request_or_404(request_id: str) -> dict[str, Any]:
    normalized_request_id = _clean_text(request_id)
    if not normalized_request_id:
        raise HTTPException(status_code=400, detail="Request id is required.")

    request_doc = raw_subscription_requests_collection.find_one(
        {"request_id": normalized_request_id}
    )
    if not request_doc:
        raise HTTPException(status_code=404, detail="Subscription request not found.")
    return request_doc


@router.get("/me")
def get_my_subscription_state(
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    user = raw_users_collection.find_one({"user_id": current_user.user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    current_subscription = _public_subscription(user.get("subscription"))
    pending_request = _public_request(user.get("pending_subscription_request"))

    effective_plan_code = current_subscription.get("plan_code") or "starter_access"
    return {
        "current_subscription": current_subscription,
        "pending_request": pending_request,
        "effective_plan_code": effective_plan_code,
    }


@router.post("/request")
def create_or_update_subscription_request(
    payload: SubscriptionRequestPayload,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    user = raw_users_collection.find_one({"user_id": current_user.user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    existing_pending = _normalize_request_document(user.get("pending_subscription_request"))
    if existing_pending and existing_pending.get("status") != "pending_approval":
        existing_pending = {}

    now_iso = _utc_now()
    request_id = existing_pending.get("request_id") or uuid4().hex
    requested_at = existing_pending.get("requested_at") or now_iso
    request_doc = _build_pending_request(
        request_id=request_id,
        user=user,
        payload=payload,
        requested_at=requested_at,
        updated_at=now_iso,
    )

    raw_users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "pending_subscription_request": request_doc,
                "updated_at": _now_ms(),
            }
        },
    )
    raw_subscription_requests_collection.update_one(
        {"request_id": request_id},
        {
            "$set": request_doc,
            "$setOnInsert": {"created_at": requested_at},
        },
        upsert=True,
    )

    refreshed = raw_users_collection.find_one({"_id": user["_id"]})
    if not refreshed:
        raise HTTPException(status_code=500, detail="User subscription request could not be saved.")

    return {
        "message": "Subscription request saved for admin approval.",
        "current_subscription": _public_subscription(refreshed.get("subscription")),
        "pending_request": _public_request(refreshed.get("pending_subscription_request")),
    }


@router.get("/requests")
def list_subscription_requests(
    status: str = Query(default="pending"),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_subscription_admin_user(current_user)

    normalized_status = _clean_text(status).lower()
    mongo_filter: dict[str, Any] = {}
    if normalized_status in _PENDING_STATUSES:
        mongo_filter["status"] = "pending_approval"
    elif normalized_status in _REVIEWABLE_STATUSES:
        mongo_filter["status"] = normalized_status
    elif normalized_status not in {"", "all"}:
        raise HTTPException(status_code=400, detail="Invalid request status filter.")

    docs = list(
        raw_subscription_requests_collection.find(mongo_filter, {"_id": 0})
        .sort([("requested_at", -1), ("updated_at", -1)])
        .limit(limit)
    )
    return {
        "requests": [_public_request(doc) for doc in docs],
        "count": len(docs),
    }


@router.post("/requests/{request_id}/approve")
def approve_subscription_request(
    request_id: str,
    payload: Optional[SubscriptionReviewPayload] = None,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_subscription_admin_user(current_user)

    request_doc = _normalize_request_document(_find_request_or_404(request_id))
    if request_doc.get("status") != "pending_approval":
        raise HTTPException(status_code=400, detail="Only pending requests can be approved.")

    user = raw_users_collection.find_one({"user_id": request_doc.get("user_id")})
    if not user:
        raise HTTPException(status_code=404, detail="Request owner not found.")

    now_iso = _utc_now()
    now_ms = _now_ms()
    review_note = _clean_text(payload.review_note) if payload else ""
    active_subscription = _build_active_subscription(
        request_doc,
        approved_by=current_user,
        approved_at=now_iso,
    )

    raw_users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "subscription": active_subscription,
                "workspace": _clean_text(request_doc.get("company_name"))
                or _clean_text(user.get("workspace")),
                "updated_at": now_ms,
            },
            "$unset": {"pending_subscription_request": ""},
        },
    )
    raw_subscription_requests_collection.update_one(
        {"request_id": request_doc["request_id"]},
        {
            "$set": {
                "status": "approved",
                "reviewed_at": now_iso,
                "review_note": review_note,
                "reviewed_by_user_id": current_user.user_id,
                "reviewed_by_email": current_user.email,
                "approved_subscription": active_subscription,
                "updated_at": now_iso,
            }
        },
    )

    return {
        "message": "Subscription request approved.",
        "request": {
            **_public_request(
                {
                    **request_doc,
                    "status": "approved",
                    "reviewed_at": now_iso,
                    "review_note": review_note,
                    "reviewed_by_user_id": current_user.user_id,
                    "reviewed_by_email": current_user.email,
                    "updated_at": now_iso,
                }
            ),
            "approved_subscription": _public_subscription(active_subscription),
        },
    }


@router.post("/requests/{request_id}/reject")
def reject_subscription_request(
    request_id: str,
    payload: Optional[SubscriptionReviewPayload] = None,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_subscription_admin_user(current_user)

    request_doc = _normalize_request_document(_find_request_or_404(request_id))
    if request_doc.get("status") != "pending_approval":
        raise HTTPException(status_code=400, detail="Only pending requests can be rejected.")

    user = raw_users_collection.find_one({"user_id": request_doc.get("user_id")})
    if not user:
        raise HTTPException(status_code=404, detail="Request owner not found.")

    now_iso = _utc_now()
    review_note = _clean_text(payload.review_note) if payload else ""
    raw_users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$unset": {"pending_subscription_request": ""},
            "$set": {"updated_at": _now_ms()},
        },
    )
    raw_subscription_requests_collection.update_one(
        {"request_id": request_doc["request_id"]},
        {
            "$set": {
                "status": "rejected",
                "reviewed_at": now_iso,
                "review_note": review_note,
                "reviewed_by_user_id": current_user.user_id,
                "reviewed_by_email": current_user.email,
                "updated_at": now_iso,
            }
        },
    )

    return {
        "message": "Subscription request rejected.",
        "request": _public_request(
            {
                **request_doc,
                "status": "rejected",
                "reviewed_at": now_iso,
                "review_note": review_note,
                "reviewed_by_user_id": current_user.user_id,
                "reviewed_by_email": current_user.email,
                "updated_at": now_iso,
            }
        ),
    }
