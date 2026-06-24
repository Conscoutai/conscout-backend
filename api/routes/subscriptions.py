from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import require_authenticated_user
from core.auth_context import AuthenticatedUser
from core.database import raw_users_collection


router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])


class SubscriptionRequestPayload(BaseModel):
    plan_code: str = Field(..., min_length=1)
    plan_name: str = Field(..., min_length=1)
    monthly_price_usd: int = Field(..., ge=0)
    project_limit: int | None = Field(default=None, ge=1)
    company_name: str = Field(..., min_length=1)
    phone: str = ""
    requested_project_count: int | None = Field(default=None, ge=1)
    notes: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/request")
def create_or_update_subscription_request(
    payload: SubscriptionRequestPayload,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    user = raw_users_collection.find_one({"user_id": current_user.user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    normalized_plan_code = payload.plan_code.strip().lower()
    normalized_plan_name = payload.plan_name.strip()
    company_name = payload.company_name.strip()
    phone = payload.phone.strip()
    notes = payload.notes.strip()
    requested_project_count = payload.requested_project_count
    now = _utc_now()

    subscription = {
        "plan_code": normalized_plan_code,
        "plan_name": normalized_plan_name,
        "monthly_price_usd": payload.monthly_price_usd,
        "project_limit": payload.project_limit,
        "company_name": company_name,
        "phone": phone,
        "requested_project_count": requested_project_count,
        "notes": notes,
        "status": "pending_payment",
        "payment_status": "pending",
        "source": "landing_pricing",
        "updated_at": now,
    }

    raw_users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "workspace": company_name,
                "subscription": subscription,
                "updated_at": int(datetime.now(timezone.utc).timestamp() * 1000),
            }
        },
    )

    refreshed = raw_users_collection.find_one({"_id": user["_id"]})
    if not refreshed:
        raise HTTPException(status_code=500, detail="User subscription could not be saved.")

    return {
        "message": "Subscription saved successfully.",
        "subscription": {
            "plan_code": refreshed.get("subscription", {}).get(
                "plan_code", normalized_plan_code
            ),
            "plan_name": refreshed.get("subscription", {}).get(
                "plan_name", normalized_plan_name
            ),
            "status": refreshed.get("subscription", {}).get(
                "status", "pending_payment"
            ),
            "payment_status": refreshed.get("subscription", {}).get(
                "payment_status", "pending"
            ),
        },
    }
