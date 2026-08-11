from __future__ import annotations

import calendar
import html
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import uuid4

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from core.auth import ensure_subscription_admin_user, require_authenticated_user
from core.auth_context import AuthenticatedUser
from core.config import (
    ALLOWED_ORIGINS,
    APP_SURFACE,
    MOYASAR_APPLE_PAY_COUNTRY,
    MOYASAR_APPLE_PAY_LABEL,
    MOYASAR_FORM_CSS_URL,
    MOYASAR_FORM_SCRIPT_URL,
    MOYASAR_PUBLISHABLE_KEY,
    MOYASAR_SECRET_KEY,
    MOYASAR_WEBHOOK_SECRET,
    PUBLIC_API_BASE_URL,
    SUBSCRIPTION_PAYMENT_CURRENCY,
)
from core.database import (
    raw_subscription_checkout_sessions_collection,
    raw_subscription_requests_collection,
    raw_users_collection,
)
from core.subscription_plans import get_subscription_plan
from services.subscription_billing_service import reconcile_moyasar_renewal_payment


router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])

_PENDING_STATUSES = {"pending", "pending_approval"}
_REVIEWABLE_STATUSES = {"approved", "rejected", "completed_by_payment"}
_CHECKOUT_PENDING_STATUSES = {"pending_checkout", "ready", "pending_activation"}
_CHECKOUT_FINAL_STATUSES = {"paid", "failed", "cancelled", "expired", "replaced"}
_CHECKOUT_ACTIVATABLE_STATUSES = _CHECKOUT_PENDING_STATUSES | {
    "failed",
    "cancelled",
    "expired",
    "replaced",
}
_MOYASAR_PAYMENT_API_BASE = "https://api.moyasar.com/v1"
_CHECKOUT_SESSION_LIFETIME_MINUTES = 30
_bearer_scheme = HTTPBearer(auto_error=False)


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


class SubscriptionCheckoutSessionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_code: str = Field(..., min_length=1, max_length=64)
    company_name: str = Field(..., min_length=1, max_length=200)
    billing_contact_name: str = Field(..., min_length=1, max_length=160)
    billing_email: str = Field(..., min_length=3, max_length=320)
    phone: str = Field(default="", max_length=40)
    tax_id: str = Field(default="", max_length=80)
    team_size: Optional[int] = Field(default=None, ge=1, le=100000)
    notes: str = Field(default="", max_length=2000)
    payment_method: str = Field(..., min_length=1, max_length=32)
    return_url: str = Field(default="", max_length=2048)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_email(value: Any) -> str:
    return _clean_text(value).lower()


def _checkout_plan(plan_code: Any) -> dict[str, Any]:
    plan = get_subscription_plan(plan_code)
    if not plan:
        raise HTTPException(
            status_code=400, detail="Unknown or unavailable subscription plan."
        )
    return dict(plan)


def _next_month(value: datetime) -> datetime:
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


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


def _normalize_checkout_status(value: Any) -> str:
    normalized = _clean_text(value).lower()
    if normalized in _CHECKOUT_PENDING_STATUSES or normalized == "processing":
        return normalized
    if normalized in _CHECKOUT_FINAL_STATUSES:
        return normalized
    return "pending_checkout"


def _normalize_checkout_session(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    normalized = dict(raw)
    normalized["session_id"] = _clean_text(raw.get("session_id"))
    normalized["access_key"] = _clean_text(raw.get("access_key"))
    normalized["user_id"] = _clean_text(raw.get("user_id"))
    normalized["user_email"] = _normalize_email(raw.get("user_email"))
    normalized["plan_code"] = _clean_text(raw.get("plan_code")).lower()
    normalized["plan_name"] = _clean_text(raw.get("plan_name"))
    normalized["company_name"] = _clean_text(raw.get("company_name"))
    normalized["billing_contact_name"] = _clean_text(raw.get("billing_contact_name"))
    normalized["billing_email"] = _normalize_email(raw.get("billing_email"))
    normalized["phone"] = _clean_text(raw.get("phone"))
    normalized["tax_id"] = _clean_text(raw.get("tax_id"))
    normalized["notes"] = _clean_text(raw.get("notes"))
    normalized["payment_method"] = _clean_text(raw.get("payment_method")).lower()
    normalized["gateway_method"] = _clean_text(raw.get("gateway_method")).lower()
    normalized["currency"] = _clean_text(raw.get("currency")).upper()
    normalized["return_url"] = _clean_text(raw.get("return_url"))
    normalized["checkout_url"] = _clean_text(raw.get("checkout_url"))
    normalized["callback_url"] = _clean_text(raw.get("callback_url"))
    normalized["payment_given_id"] = _clean_text(raw.get("payment_given_id"))
    normalized["status"] = _normalize_checkout_status(raw.get("status"))
    normalized["payment_id"] = _clean_text(raw.get("payment_id"))
    normalized["payment_status"] = _clean_text(raw.get("payment_status")).lower()
    normalized["failure_reason"] = _clean_text(raw.get("failure_reason"))
    normalized["created_at"] = _clean_text(raw.get("created_at"))
    normalized["expires_at"] = _clean_text(raw.get("expires_at"))
    normalized["updated_at"] = _clean_text(raw.get("updated_at"))
    normalized["paid_at"] = _clean_text(raw.get("paid_at"))
    normalized["failed_at"] = _clean_text(raw.get("failed_at"))
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
        "current_period_start": _clean_text(normalized.get("current_period_start")),
        "current_period_end": _clean_text(normalized.get("current_period_end")),
        "next_charge_at": _clean_text(normalized.get("next_charge_at")),
        "auto_renew": normalized.get("auto_renew") is True,
        "cancellation_requested_at": _clean_text(
            normalized.get("cancellation_requested_at")
        ),
        "gateway_payment_method": _clean_text(normalized.get("gateway_payment_method")),
        "gateway_card_brand": _clean_text(normalized.get("gateway_card_brand")),
        "gateway_card_last_four": _clean_text(normalized.get("gateway_card_last_four")),
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


def _new_checkout_access_key() -> str:
    return secrets.token_urlsafe(32)


def _moyasar_gateway_method(payment_method: str) -> str:
    normalized = _clean_text(payment_method).lower()
    if normalized == "card":
        return "creditcard"
    if normalized == "apple_pay":
        return "applepay"
    raise HTTPException(status_code=400, detail="Unsupported payment method.")


def _resolve_public_api_base_url(request: Request) -> str:
    configured = _clean_text(PUBLIC_API_BASE_URL).rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _normalize_return_url(value: Any, request: Request) -> str:
    raw_value = _clean_text(value)
    if not raw_value:
        return ""

    parsed = urlparse(raw_value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    request_origin = _clean_text(request.headers.get("origin"))
    if request_origin:
        origin = urlparse(request_origin)
        if origin.scheme in {"http", "https"} and origin.netloc:
            if origin.scheme != parsed.scheme or origin.netloc != parsed.netloc:
                return ""
    elif "*" not in ALLOWED_ORIGINS:
        allowed_hosts = {
            urlparse(origin).netloc
            for origin in ALLOWED_ORIGINS
            if urlparse(origin).scheme in {"http", "https"}
        }
        if parsed.netloc not in allowed_hosts:
            return ""

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path or "/",
            "",
            urlencode(parse_qsl(parsed.query, keep_blank_values=True)),
            "",
        )
    )


def _append_query(url: str, values: dict[str, str]) -> str:
    parsed = urlparse(url)
    current_query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    current_query.update({key: value for key, value in values.items() if value})
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(current_query),
            parsed.fragment,
        )
    )


def _is_moyasar_configured() -> bool:
    return bool(MOYASAR_PUBLISHABLE_KEY and MOYASAR_SECRET_KEY)


def _moyasar_environment() -> str:
    publishable_environment = (
        "live"
        if MOYASAR_PUBLISHABLE_KEY.startswith("pk_live_")
        else "test" if MOYASAR_PUBLISHABLE_KEY.startswith("pk_test_") else ""
    )
    secret_environment = (
        "live"
        if MOYASAR_SECRET_KEY.startswith("sk_live_")
        else "test" if MOYASAR_SECRET_KEY.startswith("sk_test_") else ""
    )
    if not publishable_environment or publishable_environment != secret_environment:
        return ""
    return publishable_environment


def _ensure_moyasar_configured() -> None:
    if not _is_moyasar_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Moyasar is not configured yet. Add MOYASAR_PUBLISHABLE_KEY and "
                "MOYASAR_SECRET_KEY on the backend first."
            ),
        )
    environment = _moyasar_environment()
    if not environment:
        raise HTTPException(
            status_code=503,
            detail="Moyasar publishable and secret keys must belong to the same test or live environment.",
        )
    if environment == "live":
        if not MOYASAR_WEBHOOK_SECRET:
            raise HTTPException(
                status_code=503,
                detail="A Moyasar webhook secret is required before live checkout can be enabled.",
            )
        if urlparse(PUBLIC_API_BASE_URL).scheme != "https":
            raise HTTPException(
                status_code=503,
                detail="PUBLIC_API_BASE_URL must be an HTTPS URL before live checkout can be enabled.",
            )


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    normalized = _clean_text(value)
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _checkout_has_expired(
    session_doc: dict[str, Any], *, now: Optional[datetime] = None
) -> bool:
    expires_at = _parse_iso_datetime(session_doc.get("expires_at"))
    if not expires_at:
        return False
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return expires_at <= reference


def _validate_checkout_details(payload: SubscriptionCheckoutSessionPayload) -> None:
    if not _clean_text(payload.company_name):
        raise HTTPException(status_code=400, detail="Company name is required.")
    if not _clean_text(payload.billing_contact_name):
        raise HTTPException(status_code=400, detail="Billing contact is required.")
    billing_email = _normalize_email(payload.billing_email)
    if (
        "@" not in billing_email
        or billing_email.startswith("@")
        or billing_email.endswith("@")
    ):
        raise HTTPException(
            status_code=400, detail="A valid billing email is required."
        )


def _json_for_inline_script(value: Any) -> str:
    return (
        json.dumps(value, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _fetch_moyasar_payment(payment_id: str) -> dict[str, Any]:
    normalized_payment_id = _clean_text(payment_id)
    if not normalized_payment_id:
        raise HTTPException(status_code=400, detail="Payment id is required.")

    try:
        response = requests.get(
            f"{_MOYASAR_PAYMENT_API_BASE}/payments/{normalized_payment_id}",
            auth=(MOYASAR_SECRET_KEY, ""),
            timeout=20,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to verify the payment with Moyasar right now.",
        ) from exc

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Payment record was not found.")
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="Payment verification failed with Moyasar.",
        )

    payload = response.json()
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=502,
            detail="Unexpected payment response was returned by Moyasar.",
        )
    return payload


def _verify_checkout_payment(
    session_doc: dict[str, Any], payment_doc: dict[str, Any]
) -> None:
    expected_payment_id = _clean_text(session_doc.get("payment_given_id"))
    actual_payment_id = _clean_text(payment_doc.get("id"))
    if not expected_payment_id or actual_payment_id != expected_payment_id:
        raise HTTPException(
            status_code=400,
            detail="Payment id does not match this checkout session.",
        )

    payment_status = _clean_text(payment_doc.get("status")).lower()
    if payment_status != "paid":
        raise HTTPException(
            status_code=400,
            detail=f"Payment is not completed. Current status: {payment_status or 'unknown'}.",
        )

    expected_amount = int(session_doc.get("amount_minor") or 0)
    actual_amount = int(payment_doc.get("amount") or 0)
    if expected_amount <= 0 or actual_amount != expected_amount:
        raise HTTPException(
            status_code=400,
            detail="Payment amount does not match the selected plan.",
        )

    expected_currency = _clean_text(session_doc.get("currency")).upper()
    actual_currency = _clean_text(payment_doc.get("currency")).upper()
    if expected_currency != actual_currency:
        raise HTTPException(
            status_code=400,
            detail="Payment currency does not match the selected plan.",
        )

    source = payment_doc.get("source")
    expected_method = _clean_text(session_doc.get("gateway_method")).lower()
    source_type = (
        _clean_text(source.get("type")).lower() if isinstance(source, dict) else ""
    )
    if not source_type or (expected_method and source_type != expected_method):
        raise HTTPException(
            status_code=400,
            detail="Payment method does not match the checkout session.",
        )

    metadata = payment_doc.get("metadata")
    expected_metadata = {
        "subscription_session_id": _clean_text(session_doc.get("session_id")),
        "plan_code": _clean_text(session_doc.get("plan_code")).lower(),
        "user_id": _clean_text(session_doc.get("user_id")),
    }
    if not isinstance(metadata, dict) or any(
        _clean_text(metadata.get(key)) != expected
        for key, expected in expected_metadata.items()
    ):
        raise HTTPException(
            status_code=400,
            detail="Payment metadata does not match this checkout session.",
        )


def _ensure_payment_not_reused(
    session_doc: dict[str, Any], payment_doc: dict[str, Any]
) -> None:
    payment_id = _clean_text(payment_doc.get("id"))
    if not payment_id:
        raise HTTPException(status_code=400, detail="Moyasar payment id is missing.")

    existing = raw_subscription_checkout_sessions_collection.find_one(
        {
            "payment_id": payment_id,
            "status": "paid",
        }
    )
    if existing and _clean_text(existing.get("session_id")) != _clean_text(
        session_doc.get("session_id")
    ):
        raise HTTPException(
            status_code=409,
            detail="This Moyasar payment has already been used for another checkout.",
        )


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


def _build_paid_subscription(
    session_doc: dict[str, Any],
    payment_doc: dict[str, Any],
    *,
    activated_at: str,
) -> dict[str, Any]:
    source = (
        payment_doc.get("source") if isinstance(payment_doc.get("source"), dict) else {}
    )
    source_type = _clean_text(source.get("type")).lower()
    activated_datetime = datetime.fromisoformat(activated_at.replace("Z", "+00:00"))
    period_end = _next_month(activated_datetime).isoformat()
    payment_token = _clean_text(source.get("token"))
    masked_number = "".join(
        character
        for character in _clean_text(source.get("number"))
        if character.isdigit()
    )
    return {
        "plan_code": _clean_text(session_doc.get("plan_code")).lower(),
        "plan_name": _clean_text(session_doc.get("plan_name")),
        "monthly_price_usd": session_doc.get("monthly_price_usd"),
        "project_limit": session_doc.get("project_limit"),
        "company_name": _clean_text(session_doc.get("company_name")),
        "billing_contact_name": _clean_text(session_doc.get("billing_contact_name")),
        "billing_email": _normalize_email(session_doc.get("billing_email")),
        "phone": _clean_text(session_doc.get("phone")),
        "tax_id": _clean_text(session_doc.get("tax_id")),
        "team_size": session_doc.get("team_size"),
        "notes": _clean_text(session_doc.get("notes")),
        "status": "active",
        "payment_status": "paid",
        "source": "moyasar_checkout",
        "activated_at": activated_at,
        "approved_at": activated_at,
        "current_period_start": activated_at,
        "current_period_end": period_end,
        "next_charge_at": period_end if payment_token else "",
        "auto_renew": bool(payment_token),
        "gateway_provider": "moyasar",
        "gateway_payment_method": _clean_text(
            session_doc.get("payment_method")
        ).lower(),
        "gateway_source_type": source_type,
        "gateway_payment_token": payment_token,
        "gateway_card_brand": _clean_text(source.get("company")).lower(),
        "gateway_card_last_four": masked_number[-4:],
        "payment_reference": _clean_text(payment_doc.get("id")),
        "payment_currency": _clean_text(payment_doc.get("currency")).upper(),
        "payment_amount_minor": payment_doc.get("amount"),
        "request_id": _clean_text(session_doc.get("session_id")),
    }


def _build_checkout_session(
    *,
    session_id: str,
    access_key: str,
    user: dict[str, Any],
    plan: dict[str, Any],
    payload: SubscriptionCheckoutSessionPayload,
    request: Request,
    created_at: str,
    updated_at: str,
) -> dict[str, Any]:
    _validate_checkout_details(payload)
    gateway_method = _moyasar_gateway_method(payload.payment_method)
    currency = _clean_text(plan.get("currency")).upper()
    if SUBSCRIPTION_PAYMENT_CURRENCY != currency:
        raise HTTPException(
            status_code=503,
            detail="Subscription payment currency does not match the server plan catalogue.",
        )
    amount_minor = int(plan["amount_minor"])
    api_base_url = _resolve_public_api_base_url(request)
    checkout_url = (
        f"{api_base_url}/subscriptions/checkout/{session_id}"
        f"?access_key={access_key}"
    )
    callback_url = (
        f"{api_base_url}/subscriptions/checkout/{session_id}/callback"
        f"?access_key={access_key}"
    )
    created_datetime = _parse_iso_datetime(created_at) or datetime.now(timezone.utc)

    return {
        "session_id": session_id,
        "access_key": access_key,
        "user_id": _clean_text(user.get("user_id")),
        "user_email": _normalize_email(user.get("email")),
        "user_name": _clean_text(user.get("name")),
        "workspace": _clean_text(user.get("workspace")),
        "plan_code": plan["plan_code"],
        "plan_name": plan["plan_name"],
        "monthly_price_usd": plan["monthly_price_usd"],
        "project_limit": plan["project_limit"],
        "company_name": _clean_text(payload.company_name),
        "billing_contact_name": _clean_text(payload.billing_contact_name),
        "billing_email": _normalize_email(payload.billing_email),
        "phone": _clean_text(payload.phone),
        "tax_id": _clean_text(payload.tax_id),
        "team_size": payload.team_size,
        "notes": _clean_text(payload.notes),
        "payment_provider": "moyasar",
        "payment_method": _clean_text(payload.payment_method).lower(),
        "gateway_method": gateway_method,
        "currency": currency,
        "amount_minor": amount_minor,
        "return_url": _normalize_return_url(payload.return_url, request),
        "checkout_url": checkout_url,
        "callback_url": callback_url,
        "payment_given_id": str(uuid4()),
        "status": "pending_checkout",
        "payment_id": "",
        "payment_status": "",
        "failure_reason": "",
        "created_at": created_at,
        "expires_at": (
            created_datetime + timedelta(minutes=_CHECKOUT_SESSION_LIFETIME_MINUTES)
        ).isoformat(),
        "updated_at": updated_at,
        "paid_at": "",
        "failed_at": "",
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


def _find_checkout_session_or_404(session_id: str, access_key: str) -> dict[str, Any]:
    normalized_session_id = _clean_text(session_id)
    normalized_access_key = _clean_text(access_key)
    if not normalized_session_id or not normalized_access_key:
        raise HTTPException(
            status_code=400, detail="Checkout session credentials are required."
        )

    checkout_session = raw_subscription_checkout_sessions_collection.find_one(
        {
            "session_id": normalized_session_id,
            "access_key": normalized_access_key,
        }
    )
    if not checkout_session:
        raise HTTPException(status_code=404, detail="Checkout session was not found.")
    return checkout_session


def _activate_verified_checkout(
    session_doc: dict[str, Any],
    payment_doc: dict[str, Any],
) -> dict[str, Any]:
    _verify_checkout_payment(session_doc, payment_doc)
    _ensure_payment_not_reused(session_doc, payment_doc)

    session_id = _clean_text(session_doc.get("session_id"))
    payment_id = _clean_text(payment_doc.get("id"))
    claim = raw_subscription_checkout_sessions_collection.update_one(
        {
            "session_id": session_id,
            "status": {"$in": list(_CHECKOUT_ACTIVATABLE_STATUSES)},
        },
        {
            "$set": {
                "status": "processing",
                "payment_id": payment_id,
                "payment_status": _clean_text(payment_doc.get("status")).lower(),
                "updated_at": _utc_now(),
            }
        },
    )
    if claim.modified_count != 1:
        refreshed_session = raw_subscription_checkout_sessions_collection.find_one(
            {"session_id": session_id}
        )
        refreshed_status = _clean_text((refreshed_session or {}).get("status"))
        if refreshed_status == "paid":
            user = raw_users_collection.find_one(
                {"user_id": _clean_text(session_doc.get("user_id"))}
            )
            if user:
                return _public_subscription(user.get("subscription"))
        if (
            refreshed_status == "processing"
            and _clean_text((refreshed_session or {}).get("payment_id")) == payment_id
        ):
            user = raw_users_collection.find_one(
                {"user_id": _clean_text(session_doc.get("user_id"))}
            )
            subscription = (
                _normalize_subscription(user.get("subscription")) if user else {}
            )
            if _clean_text(subscription.get("payment_reference")) == payment_id:
                paid_at = _clean_text(subscription.get("activated_at")) or _utc_now()
                raw_subscription_checkout_sessions_collection.update_one(
                    {"session_id": session_id, "status": "processing"},
                    {
                        "$set": {
                            "status": "paid",
                            "payment": payment_doc,
                            "payment_status": "paid",
                            "failure_reason": "",
                            "updated_at": paid_at,
                            "paid_at": paid_at,
                        }
                    },
                )
                return _public_subscription(subscription)
            raw_subscription_checkout_sessions_collection.update_one(
                {"session_id": session_id, "status": "processing"},
                {
                    "$set": {
                        "status": "pending_activation",
                        "failure_reason": "Plan activation must be retried.",
                        "updated_at": _utc_now(),
                    }
                },
            )
        raise HTTPException(
            status_code=409,
            detail="This checkout is already being processed or is no longer available.",
        )

    user = raw_users_collection.find_one({"user_id": session_doc.get("user_id")})
    if not user:
        raw_subscription_checkout_sessions_collection.update_one(
            {"session_id": session_id, "status": "processing"},
            {
                "$set": {
                    "status": "pending_activation",
                    "failure_reason": "The account for this checkout session was not found.",
                    "updated_at": _utc_now(),
                }
            },
        )
        raise HTTPException(
            status_code=404,
            detail="The account for this checkout session was not found.",
        )

    paid_at = _utc_now()
    active_subscription = _build_paid_subscription(
        session_doc,
        payment_doc,
        activated_at=paid_at,
    )
    raw_users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "subscription": active_subscription,
                "workspace": _clean_text(session_doc.get("company_name"))
                or _clean_text(user.get("workspace")),
                "updated_at": _now_ms(),
            },
            "$unset": {"pending_subscription_request": ""},
        },
    )
    raw_subscription_requests_collection.update_many(
        {
            "user_id": _clean_text(session_doc.get("user_id")),
            "status": "pending_approval",
        },
        {
            "$set": {
                "status": "completed_by_payment",
                "reviewed_at": paid_at,
                "review_note": "Superseded by verified Moyasar payment.",
                "payment_reference": payment_id,
                "updated_at": paid_at,
            }
        },
    )
    raw_subscription_checkout_sessions_collection.update_one(
        {"session_id": session_id, "status": "processing"},
        {
            "$set": {
                "status": "paid",
                "payment_id": payment_id,
                "payment_status": _clean_text(payment_doc.get("status")).lower(),
                "payment": payment_doc,
                "failure_reason": "",
                "updated_at": paid_at,
                "paid_at": paid_at,
            }
        },
    )
    raw_subscription_checkout_sessions_collection.update_many(
        {
            "user_id": _clean_text(session_doc.get("user_id")),
            "session_id": {"$ne": session_id},
            "status": {"$in": ["pending_checkout", "ready"]},
        },
        {
            "$set": {
                "status": "replaced",
                "failure_reason": "Superseded by a completed subscription payment.",
                "updated_at": paid_at,
            }
        },
    )
    return active_subscription


def _build_checkout_redirect_url(
    session_doc: dict[str, Any],
    *,
    status: str,
    message: str,
) -> str:
    return_url = _clean_text(session_doc.get("return_url"))
    if not return_url:
        return ""
    return _append_query(
        return_url,
        {
            "checkout_status": status,
            "checkout_plan": _clean_text(session_doc.get("plan_code")).lower(),
            "checkout_message": message,
            "checkout_session_id": _clean_text(session_doc.get("session_id")),
        },
    )


def _checkout_session_response(
    session_doc: dict[str, Any],
    *,
    reused: bool = False,
) -> dict[str, Any]:
    normalized = _normalize_checkout_session(session_doc)
    return {
        "message": (
            "Existing secure checkout session resumed."
            if reused
            else "Secure checkout session created."
        ),
        "provider": "moyasar",
        "session_id": normalized.get("session_id"),
        "checkout_url": normalized.get("checkout_url"),
        "payment_method": normalized.get("payment_method"),
        "currency": normalized.get("currency"),
        "amount_minor": normalized.get("amount_minor"),
        "reused": reused,
    }


def _result_page(
    *,
    title: str,
    message: str,
    accent: str,
    button_label: str = "Return to app",
    button_href: str = "",
) -> str:
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    safe_href = html.escape(button_href, quote=True)
    button_html = ""
    if safe_href:
        button_html = (
            f'<a href="{safe_href}" '
            'style="display:inline-flex;align-items:center;justify-content:center;'
            "min-width:220px;padding:14px 20px;border-radius:14px;"
            'background:#ffffff;color:#08111f;text-decoration:none;font-weight:800;">'
            f"{html.escape(button_label)}</a>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
</head>
<body style="margin:0;font-family:Arial,sans-serif;background:#08111f;color:#ffffff;">
  <div style="min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;">
    <div style="width:100%;max-width:560px;background:#101a2d;border:1px solid rgba(255,255,255,0.08);border-radius:28px;padding:32px;box-shadow:0 24px 60px rgba(0,0,0,0.32);">
      <div style="display:inline-flex;padding:8px 14px;border-radius:999px;background:{accent};color:#fff;font-weight:700;font-size:13px;">
        Subscription Checkout
      </div>
      <h1 style="margin:18px 0 12px;font-size:34px;line-height:1.1;">{safe_title}</h1>
      <p style="margin:0 0 24px;color:rgba(255,255,255,0.76);line-height:1.65;font-size:15px;">{safe_message}</p>
      {button_html}
    </div>
  </div>
</body>
</html>"""


def _checkout_page(session_doc: dict[str, Any]) -> str:
    payment_method = _clean_text(session_doc.get("payment_method")).lower()
    method_title = "Apple Pay" if payment_method == "apple_pay" else "Card"
    method_caption = (
        "Apple Pay checkout with direct plan activation after verified payment."
        if payment_method == "apple_pay"
        else "Secure card checkout powered by Moyasar. Card details stay on the payment form."
    )
    amount_minor = int(session_doc.get("amount_minor") or 0)
    amount_major = int(session_doc.get("monthly_price_usd") or 0)
    currency = _clean_text(session_doc.get("currency")).upper() or "USD"
    checkout_config: dict[str, Any] = {
        "element": ".mysr-form",
        "given_id": _clean_text(session_doc.get("payment_given_id")),
        "amount": amount_minor,
        "currency": currency,
        "description": f"{_clean_text(session_doc.get('plan_name'))} subscription for {_clean_text(session_doc.get('company_name'))}",
        "publishable_api_key": MOYASAR_PUBLISHABLE_KEY,
        "callback_url": _clean_text(session_doc.get("callback_url")),
        "supported_networks": ["visa", "mastercard", "mada"],
        "methods": [_clean_text(session_doc.get("gateway_method")).lower()],
        "metadata": {
            "subscription_session_id": _clean_text(session_doc.get("session_id")),
            "plan_code": _clean_text(session_doc.get("plan_code")).lower(),
            "user_id": _clean_text(session_doc.get("user_id")),
        },
    }
    if payment_method == "apple_pay":
        checkout_config["apple_pay"] = {
            "country": MOYASAR_APPLE_PAY_COUNTRY,
            "label": MOYASAR_APPLE_PAY_LABEL,
            "validate_merchant_url": "https://api.moyasar.com/v1/applepay/initiate",
            "save_card": True,
        }
    else:
        checkout_config["credit_card"] = {"save_card": True}

    config_json = _json_for_inline_script(checkout_config)
    authorize_url_json = _json_for_inline_script(
        (
            f"/subscriptions/checkout/{_clean_text(session_doc.get('session_id'))}/authorize"
            f"?access_key={_clean_text(session_doc.get('access_key'))}"
        )
    )
    company_name = html.escape(_clean_text(session_doc.get("company_name")))
    billing_name = html.escape(_clean_text(session_doc.get("billing_contact_name")))
    billing_email = html.escape(_clean_text(session_doc.get("billing_email")))
    plan_name = html.escape(_clean_text(session_doc.get("plan_name")))
    project_limit = session_doc.get("project_limit")
    project_text = "Unlimited" if project_limit is None else str(project_limit)
    method_title_safe = html.escape(method_title)
    method_caption_safe = html.escape(method_caption)
    payment_label = f"{currency} {amount_major}/mo"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Complete Subscription Payment</title>
  <link rel="stylesheet" href="{html.escape(MOYASAR_FORM_CSS_URL, quote=True)}" />
  <style>
    :root {{
      color-scheme: dark;
      --bg-1: #08111f;
      --bg-2: #101a2d;
      --panel: rgba(18, 27, 45, 0.94);
      --line: rgba(255, 255, 255, 0.10);
      --text-soft: rgba(255, 255, 255, 0.72);
      --accent: #58a6ff;
      --success: #35c46e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, sans-serif;
      color: #ffffff;
      background:
        radial-gradient(circle at top left, rgba(88, 166, 255, 0.18), transparent 34%),
        radial-gradient(circle at bottom right, rgba(53, 196, 110, 0.16), transparent 28%),
        linear-gradient(135deg, var(--bg-1), #0e1c30 45%, #12263f);
      min-height: 100vh;
    }}
    .shell {{
      max-width: 1160px;
      margin: 0 auto;
      padding: 28px 20px 40px;
    }}
    .header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 24px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
      font-weight: 800;
      font-size: 22px;
      letter-spacing: -0.03em;
    }}
    .brand-mark {{
      width: 42px;
      height: 42px;
      border-radius: 14px;
      background: linear-gradient(180deg, #3027c7, #1f169d);
      display: grid;
      place-items: center;
      box-shadow: 0 14px 28px rgba(47, 39, 199, 0.28);
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      padding: 8px 14px;
      border-radius: 999px;
      background: rgba(88, 166, 255, 0.14);
      border: 1px solid rgba(88, 166, 255, 0.24);
      color: #b8d8ff;
      font-size: 13px;
      font-weight: 700;
    }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(300px, 400px) minmax(0, 1fr);
      gap: 22px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 28px;
      box-shadow: 0 26px 60px rgba(0, 0, 0, 0.28);
      backdrop-filter: blur(10px);
    }}
    .summary {{
      padding: 28px;
    }}
    .summary h1 {{
      margin: 16px 0 12px;
      font-size: 34px;
      line-height: 1.05;
      letter-spacing: -0.04em;
    }}
    .summary p {{
      margin: 0 0 22px;
      color: var(--text-soft);
      line-height: 1.65;
      font-size: 15px;
    }}
    .facts {{
      display: grid;
      gap: 14px;
      margin-top: 18px;
    }}
    .fact {{
      padding: 16px 18px;
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.07);
    }}
    .fact .label {{
      color: rgba(255, 255, 255, 0.58);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 8px;
    }}
    .fact .value {{
      font-size: 18px;
      font-weight: 700;
      line-height: 1.35;
    }}
    .note {{
      margin-top: 20px;
      padding: 16px 18px;
      border-radius: 18px;
      background: rgba(53, 196, 110, 0.10);
      border: 1px solid rgba(53, 196, 110, 0.22);
      color: #d7ffea;
      line-height: 1.55;
      font-size: 13px;
      font-weight: 600;
    }}
    .checkout {{
      padding: 28px;
    }}
    .checkout h2 {{
      margin: 0 0 8px;
      font-size: 26px;
      letter-spacing: -0.03em;
    }}
    .checkout p {{
      margin: 0 0 18px;
      color: var(--text-soft);
      line-height: 1.65;
      font-size: 14px;
    }}
    .checkout-status {{
      margin-bottom: 16px;
      padding: 14px 16px;
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.08);
      color: #d9e7ff;
      font-size: 13px;
      font-weight: 600;
    }}
    .mysr-form {{
      min-height: 360px;
    }}
    @media (max-width: 940px) {{
      .grid {{
        grid-template-columns: 1fr;
      }}
      .summary,
      .checkout {{
        padding: 22px;
      }}
      .summary h1 {{
        font-size: 30px;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="header">
      <div class="brand">
        <div class="brand-mark">C</div>
        <div>ConScout</div>
      </div>
      <div class="badge">Secure Checkout</div>
    </div>
    <div class="grid">
      <section class="panel summary">
        <div class="badge">{method_title_safe}</div>
        <h1>Activate {plan_name} now</h1>
        <p>{method_caption_safe}</p>
        <div class="facts">
          <div class="fact">
            <div class="label">Amount</div>
            <div class="value">{html.escape(payment_label)}</div>
          </div>
          <div class="fact">
            <div class="label">Projects</div>
            <div class="value">{html.escape(project_text)} active projects</div>
          </div>
          <div class="fact">
            <div class="label">Workspace</div>
            <div class="value">{company_name}</div>
          </div>
          <div class="fact">
            <div class="label">Billing Contact</div>
            <div class="value">{billing_name}<br /><span style="font-size:14px;color:rgba(255,255,255,0.68);font-weight:600;">{billing_email}</span></div>
          </div>
        </div>
        <div class="note">
          Payments are processed by Moyasar. By paying, you authorize the shown
          monthly charge until you stop renewal. Moyasar returns a reusable
          payment token for renewal; ConScout does not receive or store your full
          card number. Your plan activates only after backend payment verification.
        </div>
      </section>
      <section class="panel checkout">
        <h2>Finish your payment</h2>
        <p>Use the secure form below. If 3D Secure or wallet confirmation is required, you will be redirected automatically and then returned to finish activation.</p>
        <div id="checkout-status" class="checkout-status">
          Initializing secure payment form...
        </div>
        <div class="mysr-form"></div>
      </section>
    </div>
  </div>

  <script src="{html.escape(MOYASAR_FORM_SCRIPT_URL, quote=True)}"></script>
  <script>
    (function () {{
      var statusNode = document.getElementById('checkout-status');
      var checkoutConfig = {config_json};

      function setStatus(message) {{
        if (statusNode) {{
          statusNode.textContent = message;
        }}
      }}

      checkoutConfig.on_initiating = async function () {{
        setStatus('Securing this checkout session...');
        try {{
          var response = await fetch({authorize_url_json}, {{
            method: 'POST',
            headers: {{ 'Accept': 'application/json' }},
          }});
          if (!response.ok) {{
            var body = await response.json().catch(function () {{ return {{}}; }});
            throw new Error(body.detail || 'This checkout session is no longer available.');
          }}
        }} catch (error) {{
          setStatus((error && error.message) || 'This checkout session could not be secured.');
          return false;
        }}
        setStatus('Submitting payment to Moyasar...');
        return {{}};
      }};

      checkoutConfig.on_completed = async function () {{
        setStatus('Payment received. Verifying and activating your subscription...');
      }};

      checkoutConfig.on_failure = function (error) {{
        var message = (error && (error.message || error.toString())) || 'Payment failed. Please review the entered details and try again.';
        setStatus(message);
      }};

      checkoutConfig.on_redirect = function (url) {{
        setStatus('Redirecting to secure bank or wallet confirmation...');
        if (url) {{
          window.location.assign(url);
        }}
      }};

      if (!window.Moyasar || typeof window.Moyasar.init !== 'function') {{
        setStatus('Unable to load Moyasar checkout right now. Please try again in a moment.');
        return;
      }}

      window.Moyasar.init(checkoutConfig);
      setStatus('Secure payment form ready.');
    }})();
  </script>
</body>
</html>"""


@router.get("/me")
def get_my_subscription_state(
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    user = raw_users_collection.find_one({"user_id": current_user.user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    current_subscription = _public_subscription(user.get("subscription"))
    pending_request = _public_request(user.get("pending_subscription_request"))

    current_period_end = _parse_iso_datetime(
        current_subscription.get("current_period_end")
    )
    has_current_paid_plan = (
        _clean_text(current_subscription.get("status")).lower() == "active"
        and _clean_text(current_subscription.get("payment_status")).lower() == "paid"
        and get_subscription_plan(current_subscription.get("plan_code")) is not None
        and current_period_end is not None
        and current_period_end > datetime.now(timezone.utc)
    )
    effective_plan_code = (
        current_subscription.get("plan_code")
        if has_current_paid_plan
        else "starter_access"
    )
    return {
        "current_subscription": current_subscription,
        "pending_request": pending_request,
        "effective_plan_code": effective_plan_code,
    }


@router.post("/cancel-renewal")
def cancel_subscription_renewal(
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    if APP_SURFACE != "lite":
        raise HTTPException(
            status_code=404,
            detail="Subscription renewal management is available on ConScout Lite.",
        )

    user = raw_users_collection.find_one({"user_id": current_user.user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    subscription = (
        user.get("subscription") if isinstance(user.get("subscription"), dict) else {}
    )
    if _clean_text(subscription.get("source")) != "moyasar_checkout":
        raise HTTPException(
            status_code=400,
            detail="This account does not have a Moyasar subscription to cancel.",
        )
    if subscription.get("renewal_processing") is True:
        raise HTTPException(
            status_code=409,
            detail=(
                "A renewal is already being processed. Please check the payment "
                "result before changing renewal settings."
            ),
        )

    if subscription.get("auto_renew") is not True:
        return {
            "message": "Monthly renewal is already stopped.",
            "current_subscription": _public_subscription(subscription),
        }

    now_datetime = datetime.now(timezone.utc)
    now_iso = now_datetime.isoformat()
    period_end = _parse_iso_datetime(subscription.get("current_period_end"))
    remains_active = (
        _clean_text(subscription.get("payment_status")).lower() == "paid"
        and period_end is not None
        and period_end > now_datetime
    )
    payment_reference = _clean_text(subscription.get("payment_reference"))
    cancelled = raw_users_collection.update_one(
        {
            "_id": user["_id"],
            "subscription.payment_reference": payment_reference,
            "subscription.auto_renew": True,
            "subscription.renewal_processing": {"$ne": True},
        },
        {
            "$set": {
                "subscription.auto_renew": False,
                "subscription.next_charge_at": "",
                "subscription.cancellation_requested_at": now_iso,
                "subscription.status": "active" if remains_active else "inactive",
                "updated_at": _now_ms(),
            }
        },
    )
    if cancelled.modified_count != 1:
        refreshed = raw_users_collection.find_one({"_id": user["_id"]}) or {}
        refreshed_subscription = (
            refreshed.get("subscription")
            if isinstance(refreshed.get("subscription"), dict)
            else {}
        )
        if refreshed_subscription.get("auto_renew") is not True:
            return {
                "message": "Monthly renewal is already stopped.",
                "current_subscription": _public_subscription(refreshed_subscription),
            }
        raise HTTPException(
            status_code=409,
            detail="Renewal settings changed at the same time. Please try again.",
        )

    refreshed = raw_users_collection.find_one({"_id": user["_id"]}) or user
    return {
        "message": (
            "Monthly renewal stopped. Your paid access remains available until "
            f"{period_end.date().isoformat()}."
            if remains_active and period_end is not None
            else "Monthly renewal stopped."
        ),
        "current_subscription": _public_subscription(refreshed.get("subscription")),
    }


@router.post("/request")
def create_or_update_subscription_request(
    payload: SubscriptionRequestPayload,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    if APP_SURFACE == "lite":
        raise HTTPException(
            status_code=410,
            detail=(
                "Lite plan requests no longer use admin approval. "
                "Start a Moyasar checkout from the Lite pricing page."
            ),
        )

    user = raw_users_collection.find_one({"user_id": current_user.user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    existing_pending = _normalize_request_document(
        user.get("pending_subscription_request")
    )
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
        raise HTTPException(
            status_code=500, detail="User subscription request could not be saved."
        )

    return {
        "message": "Subscription request saved for admin approval.",
        "current_subscription": _public_subscription(refreshed.get("subscription")),
        "pending_request": _public_request(
            refreshed.get("pending_subscription_request")
        ),
    }


@router.post("/checkout-session")
def create_checkout_session(
    payload: SubscriptionCheckoutSessionPayload,
    request: Request,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
):
    _ensure_moyasar_configured()

    user = raw_users_collection.find_one({"user_id": current_user.user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    plan = _checkout_plan(payload.plan_code)
    _validate_checkout_details(payload)

    authorization_token = credentials.credentials.strip() if credentials else ""
    if not authorization_token:
        raise HTTPException(status_code=401, detail="Authentication required.")

    now_iso = _utc_now()
    open_checkout = raw_subscription_checkout_sessions_collection.find_one(
        {
            "user_id": _clean_text(user.get("user_id")),
            "status": {
                "$in": [
                    "pending_checkout",
                    "ready",
                    "processing",
                    "pending_activation",
                ]
            },
        }
    )
    if open_checkout:
        normalized_open_checkout = _normalize_checkout_session(open_checkout)
        if normalized_open_checkout.get("status") in {
            "processing",
            "pending_activation",
        }:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Another subscription payment is already being verified. "
                    "Please wait for that checkout to finish."
                ),
            )
        if _checkout_has_expired(normalized_open_checkout):
            raw_subscription_checkout_sessions_collection.update_one(
                {
                    "session_id": normalized_open_checkout.get("session_id"),
                    "status": {"$in": ["pending_checkout", "ready"]},
                },
                {
                    "$set": {
                        "status": "expired",
                        "failure_reason": "Checkout session expired before payment.",
                        "updated_at": now_iso,
                    }
                },
            )
        elif (
            normalized_open_checkout.get("plan_code") == plan["plan_code"]
            and normalized_open_checkout.get("payment_method")
            == _clean_text(payload.payment_method).lower()
            and normalized_open_checkout.get("status") in {"pending_checkout", "ready"}
        ):
            return _checkout_session_response(open_checkout, reused=True)
        elif normalized_open_checkout.get("status") == "ready":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Another subscription payment is already being submitted or verified. "
                    "Please finish that checkout before starting a different one."
                ),
            )

    session_id = uuid4().hex
    access_key = _new_checkout_access_key()
    session_doc = _build_checkout_session(
        session_id=session_id,
        access_key=access_key,
        user=user,
        plan=plan,
        payload=payload,
        request=request,
        created_at=now_iso,
        updated_at=now_iso,
    )

    raw_subscription_checkout_sessions_collection.update_many(
        {
            "user_id": _clean_text(user.get("user_id")),
            "status": "pending_checkout",
        },
        {
            "$set": {
                "status": "replaced",
                "updated_at": now_iso,
                "failure_reason": "Superseded by a newer checkout session.",
            }
        },
    )

    payment_in_progress = raw_subscription_checkout_sessions_collection.find_one(
        {
            "user_id": _clean_text(user.get("user_id")),
            "status": {"$in": ["ready", "processing", "pending_activation"]},
        }
    )
    if payment_in_progress:
        raise HTTPException(
            status_code=409,
            detail=(
                "Another subscription payment started while this checkout was being created. "
                "Please finish that payment before trying again."
            ),
        )

    raw_subscription_checkout_sessions_collection.insert_one(session_doc)

    return _checkout_session_response(session_doc)


@router.post("/checkout/{session_id}/authorize")
def authorize_checkout_submission(
    session_id: str,
    access_key: str = Query(..., min_length=1),
):
    _ensure_moyasar_configured()
    checkout_session = _normalize_checkout_session(
        _find_checkout_session_or_404(session_id, access_key)
    )
    if _checkout_has_expired(checkout_session):
        raw_subscription_checkout_sessions_collection.update_one(
            {
                "session_id": checkout_session["session_id"],
                "status": {"$in": ["pending_checkout", "ready"]},
            },
            {
                "$set": {
                    "status": "expired",
                    "failure_reason": "Checkout session expired before payment.",
                    "updated_at": _utc_now(),
                }
            },
        )
        raise HTTPException(
            status_code=410,
            detail="This checkout session expired. Please start a new checkout.",
        )

    authorization = raw_subscription_checkout_sessions_collection.update_one(
        {
            "session_id": checkout_session["session_id"],
            "access_key": checkout_session["access_key"],
            "status": {"$in": ["pending_checkout", "ready"]},
        },
        {
            "$set": {
                "status": "ready",
                "payment_submission_authorized_at": _utc_now(),
                "updated_at": _utc_now(),
            }
        },
    )
    if authorization.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="This checkout session is already completed or no longer available.",
        )
    return {"authorized": True, "session_id": checkout_session["session_id"]}


@router.post("/moyasar/webhook")
def moyasar_webhook(payload: dict[str, Any]):
    if not MOYASAR_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=503, detail="Moyasar webhook is not configured."
        )

    provided_secret = _clean_text(payload.get("secret_token"))
    if not provided_secret or not secrets.compare_digest(
        provided_secret,
        MOYASAR_WEBHOOK_SECRET,
    ):
        raise HTTPException(status_code=401, detail="Invalid Moyasar webhook secret.")

    expected_live = MOYASAR_SECRET_KEY.startswith("sk_live_")
    if not isinstance(payload.get("live"), bool) or payload["live"] != expected_live:
        raise HTTPException(
            status_code=400, detail="Moyasar webhook environment mismatch."
        )

    event_type = _clean_text(payload.get("type")).lower()
    payment_data = payload.get("data")
    if not event_type.startswith("payment_") or not isinstance(payment_data, dict):
        return {"received": True, "handled": False}

    metadata = payment_data.get("metadata")
    renewal_user_id = (
        _clean_text(metadata.get("subscription_renewal_user_id"))
        if isinstance(metadata, dict)
        else ""
    )
    if renewal_user_id:
        payment_id = _clean_text(payment_data.get("id"))
        if event_type == "payment_paid":
            verified_payment = _fetch_moyasar_payment(payment_id)
            result = reconcile_moyasar_renewal_payment(verified_payment)
            return {"received": True, "handled": True, **result}
        if event_type in {"payment_failed", "payment_faild"}:
            result = reconcile_moyasar_renewal_payment(payment_data)
            return {"received": True, "handled": True, **result}
        if event_type in {"payment_refunded", "payment_voided"}:
            terminal_status = (
                "refunded" if event_type == "payment_refunded" else "voided"
            )
            raw_users_collection.update_one(
                {
                    "user_id": renewal_user_id,
                    "subscription.payment_reference": payment_id,
                },
                {
                    "$set": {
                        "subscription.status": "inactive",
                        "subscription.payment_status": terminal_status,
                        "subscription.auto_renew": False,
                        "subscription.next_charge_at": "",
                        "updated_at": _now_ms(),
                    }
                },
            )
            return {
                "received": True,
                "handled": True,
                "status": terminal_status,
                "user_id": renewal_user_id,
            }
        return {"received": True, "handled": False}

    session_id = (
        _clean_text(metadata.get("subscription_session_id"))
        if isinstance(metadata, dict)
        else ""
    )
    if not session_id:
        return {"received": True, "handled": False}

    raw_session = raw_subscription_checkout_sessions_collection.find_one(
        {"session_id": session_id}
    )
    if not raw_session:
        return {"received": True, "handled": False}

    checkout_session = _normalize_checkout_session(raw_session)
    payment_id = _clean_text(payment_data.get("id"))

    if event_type == "payment_paid":
        if checkout_session.get("status") == "paid":
            return {"received": True, "handled": True, "status": "already_paid"}
        verified_payment = _fetch_moyasar_payment(payment_id)
        _activate_verified_checkout(checkout_session, verified_payment)
        return {"received": True, "handled": True, "status": "paid"}

    if event_type in {"payment_failed", "payment_faild"}:
        failed_at = _utc_now()
        raw_subscription_checkout_sessions_collection.update_one(
            {
                "session_id": session_id,
                "status": {"$in": list(_CHECKOUT_PENDING_STATUSES)},
            },
            {
                "$set": {
                    "status": "failed",
                    "payment_id": payment_id,
                    "payment_status": "failed",
                    "failure_reason": _clean_text(
                        (payment_data.get("source") or {}).get("message")
                        if isinstance(payment_data.get("source"), dict)
                        else ""
                    ),
                    "updated_at": failed_at,
                    "failed_at": failed_at,
                }
            },
        )
        return {"received": True, "handled": True, "status": "failed"}

    if event_type in {"payment_refunded", "payment_voided"}:
        terminal_status = "refunded" if event_type == "payment_refunded" else "voided"
        raw_subscription_checkout_sessions_collection.update_one(
            {"session_id": session_id},
            {
                "$set": {
                    "payment_status": terminal_status,
                    "updated_at": _utc_now(),
                }
            },
        )
        raw_users_collection.update_one(
            {
                "user_id": _clean_text(checkout_session.get("user_id")),
                "subscription.payment_reference": payment_id,
            },
            {
                "$set": {
                    "subscription.status": "inactive",
                    "subscription.payment_status": terminal_status,
                    "subscription.auto_renew": False,
                    "subscription.next_charge_at": "",
                    "updated_at": _now_ms(),
                }
            },
        )
        return {"received": True, "handled": True, "status": terminal_status}

    return {"received": True, "handled": False}


@router.get("/moyasar/renewal-callback", response_class=HTMLResponse)
def moyasar_renewal_callback(id: str = Query(default="")):
    _ensure_moyasar_configured()
    payment_id = _clean_text(id)
    if not payment_id:
        return HTMLResponse(
            _result_page(
                title="Renewal not confirmed",
                message="Moyasar did not return a renewal payment id.",
                accent="rgba(255, 112, 112, 0.26)",
            ),
            status_code=400,
        )

    try:
        payment = _fetch_moyasar_payment(payment_id)
        result = reconcile_moyasar_renewal_payment(payment)
    except (HTTPException, ValueError) as exc:
        message = (
            _clean_text(exc.detail if isinstance(exc, HTTPException) else exc)
            or "The renewal payment could not be confirmed."
        )
        return HTMLResponse(
            _result_page(
                title="Renewal not confirmed",
                message=message,
                accent="rgba(255, 112, 112, 0.26)",
            ),
            status_code=400,
        )

    status = _clean_text(result.get("status"))
    is_paid = status in {"paid", "already_processed"}
    return HTMLResponse(
        _result_page(
            title="Subscription renewed" if is_paid else "Renewal needs attention",
            message=(
                "Your monthly ConScout Lite subscription is active."
                if is_paid
                else "Please update your payment method before access can continue."
            ),
            accent=(
                "rgba(53, 196, 110, 0.28)" if is_paid else "rgba(255, 112, 112, 0.26)"
            ),
        ),
        status_code=200 if is_paid else 400,
    )


@router.get("/checkout/{session_id}", response_class=HTMLResponse)
def checkout_page(
    session_id: str,
    access_key: str = Query(..., min_length=1),
):
    _ensure_moyasar_configured()
    checkout_session = _normalize_checkout_session(
        _find_checkout_session_or_404(session_id, access_key)
    )

    if checkout_session.get("status") in {
        "pending_checkout",
        "ready",
    } and _checkout_has_expired(checkout_session):
        raw_subscription_checkout_sessions_collection.update_one(
            {
                "session_id": checkout_session["session_id"],
                "status": {"$in": ["pending_checkout", "ready"]},
            },
            {
                "$set": {
                    "status": "expired",
                    "failure_reason": "Checkout session expired before payment.",
                    "updated_at": _utc_now(),
                }
            },
        )
        return HTMLResponse(
            _result_page(
                title="Checkout expired",
                message="Please return to the pricing page and start a new secure checkout.",
                accent="rgba(255, 112, 112, 0.26)",
            ),
            status_code=410,
        )

    if checkout_session.get("status") == "paid":
        redirect_url = _build_checkout_redirect_url(
            checkout_session,
            status="success",
            message=f"{checkout_session.get('plan_name') or 'Subscription'} is already active.",
        )
        if redirect_url:
            return RedirectResponse(redirect_url, status_code=303)
        return HTMLResponse(
            _result_page(
                title="Subscription already active",
                message="This checkout session was already completed successfully.",
                accent="rgba(53, 196, 110, 0.28)",
            )
        )

    if checkout_session.get("status") in {"processing", "pending_activation"}:
        return HTMLResponse(
            _result_page(
                title="Payment received",
                message=(
                    "Moyasar confirmed the payment and ConScout is completing plan activation. "
                    "Please do not submit another payment."
                ),
                accent="rgba(88, 166, 255, 0.26)",
                button_href=_clean_text(checkout_session.get("return_url")),
            ),
            status_code=202,
        )

    if checkout_session.get("status") not in _CHECKOUT_PENDING_STATUSES:
        return HTMLResponse(
            _result_page(
                title="Checkout unavailable",
                message=checkout_session.get("failure_reason")
                or "This checkout session can no longer be used. Please start a new payment from the pricing page.",
                accent="rgba(255, 112, 112, 0.26)",
            ),
            status_code=410,
        )

    return HTMLResponse(_checkout_page(checkout_session))


@router.get("/checkout/{session_id}/callback", response_class=HTMLResponse)
def checkout_callback(
    session_id: str,
    access_key: str = Query(..., min_length=1),
    id: str = Query(default=""),
):
    _ensure_moyasar_configured()

    checkout_session = _normalize_checkout_session(
        _find_checkout_session_or_404(session_id, access_key)
    )
    payment_id = _clean_text(id)
    redirect_url = ""

    if checkout_session.get("status") == "paid":
        redirect_url = _build_checkout_redirect_url(
            checkout_session,
            status="success",
            message=f"{checkout_session.get('plan_name') or 'Subscription'} is already active.",
        )
        if redirect_url:
            return RedirectResponse(redirect_url, status_code=303)
        return HTMLResponse(
            _result_page(
                title="Subscription active",
                message="Your subscription was already activated successfully.",
                accent="rgba(53, 196, 110, 0.28)",
            )
        )

    if not payment_id:
        failure_message = (
            "Moyasar did not return a payment id. Please retry the checkout."
        )
        raw_subscription_checkout_sessions_collection.update_one(
            {"session_id": checkout_session["session_id"]},
            {
                "$set": {
                    "status": "failed",
                    "failure_reason": failure_message,
                    "payment_status": "missing_payment_id",
                    "updated_at": _utc_now(),
                    "failed_at": _utc_now(),
                }
            },
        )
        redirect_url = _build_checkout_redirect_url(
            checkout_session,
            status="failed",
            message=failure_message,
        )
        if redirect_url:
            return RedirectResponse(redirect_url, status_code=303)
        return HTMLResponse(
            _result_page(
                title="Payment incomplete",
                message=failure_message,
                accent="rgba(255, 112, 112, 0.26)",
            ),
            status_code=400,
        )

    try:
        payment_doc = _fetch_moyasar_payment(payment_id)
        _activate_verified_checkout(checkout_session, payment_doc)
    except HTTPException as exc:
        failure_message = _clean_text(exc.detail) or "Payment verification failed."
        payment_status = (
            _clean_text(payment_doc.get("status")).lower()
            if "payment_doc" in locals()
            else ""
        )
        latest_session = raw_subscription_checkout_sessions_collection.find_one(
            {"session_id": checkout_session["session_id"]}
        )
        retry_activation = _clean_text(
            (latest_session or {}).get("status")
        ) == "pending_activation" or (
            payment_status == "paid" and exc.status_code >= 500
        )
        raw_subscription_checkout_sessions_collection.update_one(
            {
                "session_id": checkout_session["session_id"],
                "status": {"$ne": "paid"},
            },
            {
                "$set": {
                    "status": "pending_activation" if retry_activation else "failed",
                    "failure_reason": failure_message,
                    "payment_id": payment_id,
                    "payment_status": payment_status,
                    "updated_at": _utc_now(),
                    "failed_at": "" if retry_activation else _utc_now(),
                }
            },
        )
        redirect_url = _build_checkout_redirect_url(
            checkout_session,
            status="failed",
            message=failure_message,
        )
        if redirect_url:
            return RedirectResponse(redirect_url, status_code=303)
        return HTMLResponse(
            _result_page(
                title="Payment not confirmed",
                message=failure_message,
                accent="rgba(255, 112, 112, 0.26)",
            ),
            status_code=400,
        )

    success_message = f"Payment verified. {checkout_session.get('plan_name') or 'Your subscription'} is now active."
    redirect_url = _build_checkout_redirect_url(
        checkout_session,
        status="success",
        message=success_message,
    )
    if redirect_url:
        return RedirectResponse(redirect_url, status_code=303)
    return HTMLResponse(
        _result_page(
            title="Subscription activated",
            message=success_message,
            accent="rgba(53, 196, 110, 0.28)",
        )
    )


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
    if APP_SURFACE == "lite":
        raise HTTPException(
            status_code=410,
            detail=(
                "Lite plans cannot be activated by admin approval. "
                "A verified Moyasar payment is required."
            ),
        )

    request_doc = _normalize_request_document(_find_request_or_404(request_id))
    if request_doc.get("status") != "pending_approval":
        raise HTTPException(
            status_code=400, detail="Only pending requests can be approved."
        )

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
                "updated_at": now_iso,
                "approved_subscription": active_subscription,
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
        raise HTTPException(
            status_code=400, detail="Only pending requests can be rejected."
        )

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
