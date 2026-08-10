from __future__ import annotations

import calendar
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import uuid4

import requests

from core.config import (
    APP_SURFACE,
    MOYASAR_PUBLISHABLE_KEY,
    MOYASAR_SECRET_KEY,
    PUBLIC_API_BASE_URL,
    SUBSCRIPTION_RENEWAL_MAX_RETRIES,
    SUBSCRIPTION_RENEWAL_POLL_SECONDS,
    SUBSCRIPTION_RENEWAL_SCHEDULER_ENABLED,
)
from core.database import (
    raw_subscription_payments_collection,
    raw_users_collection,
)
from core.subscription_plans import get_subscription_plan


logger = logging.getLogger(__name__)

_MOYASAR_PAYMENT_API = "https://api.moyasar.com/v1/payments"
_RENEWAL_PROCESSING_LEASE_MINUTES = 10
_scheduler_started = False
_scheduler_lock = threading.Lock()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> Optional[datetime]:
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


def _next_month(value: datetime) -> datetime:
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _renewal_callback_url() -> str:
    base = _clean(PUBLIC_API_BASE_URL).rstrip("/")
    return f"{base}/subscriptions/moyasar/renewal-callback" if base else ""


def _renewal_configuration_error() -> str:
    if not MOYASAR_PUBLISHABLE_KEY or not MOYASAR_SECRET_KEY:
        return "Moyasar is not configured."

    callback_url = _renewal_callback_url()
    parsed_callback = urlparse(callback_url)
    if (
        not callback_url
        or parsed_callback.scheme not in {"http", "https"}
        or not parsed_callback.netloc
    ):
        return "A valid PUBLIC_API_BASE_URL is required for Moyasar renewals."
    if MOYASAR_SECRET_KEY.startswith("sk_live_") and parsed_callback.scheme != "https":
        return "Live Moyasar renewals require an HTTPS PUBLIC_API_BASE_URL."
    return ""


def _verify_renewal_payment(
    user: dict[str, Any],
    payment: dict[str, Any],
    *,
    due_at: str,
) -> dict[str, Any]:
    subscription = (
        user.get("subscription") if isinstance(user.get("subscription"), dict) else {}
    )
    plan = get_subscription_plan(subscription.get("plan_code"))
    if not plan:
        raise ValueError("Subscription plan is no longer available.")
    if _clean(payment.get("status")).lower() != "paid":
        raise ValueError(
            _clean((payment.get("source") or {}).get("message"))
            if isinstance(payment.get("source"), dict)
            else "Renewal payment was not paid."
        )
    if int(payment.get("amount") or 0) != int(plan["amount_minor"]):
        raise ValueError("Renewal payment amount does not match the plan.")
    if _clean(payment.get("currency")).upper() != plan["currency"]:
        raise ValueError("Renewal payment currency does not match the plan.")

    metadata = payment.get("metadata")
    expected = {
        "subscription_renewal_user_id": _clean(user.get("user_id")),
        "plan_code": plan["plan_code"],
        "billing_period_start": due_at,
    }
    if not isinstance(metadata, dict) or any(
        _clean(metadata.get(key)) != value for key, value in expected.items()
    ):
        raise ValueError("Renewal payment metadata does not match the subscription.")
    return plan


def _record_renewal_payment(
    user: dict[str, Any],
    payment: dict[str, Any],
    *,
    due_at: str,
) -> None:
    payment_id = _clean(payment.get("id"))
    given_id = _clean(payment.get("given_id")) or payment_id
    raw_subscription_payments_collection.update_one(
        {"payment_id": payment_id or given_id},
        {
            "$set": {
                "payment_id": payment_id or given_id,
                "given_id": given_id,
                "user_id": _clean(user.get("user_id")),
                "type": "subscription_renewal",
                "billing_period_start": due_at,
                "status": _clean(payment.get("status")).lower(),
                "amount": payment.get("amount"),
                "currency": _clean(payment.get("currency")).upper(),
                "payment": payment,
                "updated_at": _utc_now().isoformat(),
            },
            "$setOnInsert": {"created_at": _utc_now().isoformat()},
        },
        upsert=True,
    )


def _mark_renewal_paid(
    user: dict[str, Any],
    payment: dict[str, Any],
    *,
    due_at: str,
) -> dict[str, Any]:
    _verify_renewal_payment(user, payment, due_at=due_at)
    period_start = _parse_datetime(due_at)
    if not period_start:
        raise ValueError("Subscription renewal date is invalid.")
    period_end = _next_month(period_start)
    payment_id = _clean(payment.get("id"))

    raw_users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "subscription.status": "active",
                "subscription.payment_status": "paid",
                "subscription.current_period_start": period_start.isoformat(),
                "subscription.current_period_end": period_end.isoformat(),
                "subscription.next_charge_at": period_end.isoformat(),
                "subscription.payment_reference": payment_id,
                "subscription.payment_amount_minor": payment.get("amount"),
                "subscription.payment_currency": _clean(
                    payment.get("currency")
                ).upper(),
                "subscription.renewal_processing": False,
                "subscription.renewal_retry_count": 0,
                "subscription.renewal_last_error": "",
                "subscription.renewal_last_paid_at": _utc_now().isoformat(),
                "subscription.renewal_given_id": "",
                "subscription.renewal_attempt_due_at": "",
                "subscription.renewal_billing_period_start": "",
                "subscription.renewal_lease_until": "",
                "updated_at": int(_utc_now().timestamp() * 1000),
            }
        },
    )
    _record_renewal_payment(user, payment, due_at=due_at)
    return {
        "user_id": _clean(user.get("user_id")),
        "status": "paid",
        "payment_id": payment_id,
        "next_charge_at": period_end.isoformat(),
    }


def _mark_renewal_failed(
    user: dict[str, Any],
    *,
    due_at: str,
    message: str,
    payment: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    subscription = (
        user.get("subscription") if isinstance(user.get("subscription"), dict) else {}
    )
    retry_count = int(subscription.get("renewal_retry_count") or 0) + 1
    max_retries = max(1, int(SUBSCRIPTION_RENEWAL_MAX_RETRIES))
    will_retry = retry_count <= max_retries
    retry_delays = (1, 3, 5)
    retry_days = retry_delays[min(retry_count - 1, len(retry_delays) - 1)]
    next_charge_at = (
        (_utc_now() + timedelta(days=retry_days)).isoformat() if will_retry else ""
    )
    payment_id = _clean((payment or {}).get("id"))

    update = {
        "subscription.status": "past_due" if will_retry else "inactive",
        "subscription.payment_status": "failed",
        "subscription.next_charge_at": next_charge_at,
        "subscription.auto_renew": will_retry,
        "subscription.renewal_processing": False,
        "subscription.renewal_retry_count": retry_count,
        "subscription.renewal_last_error": _clean(message) or "Renewal payment failed.",
        "subscription.renewal_last_failed_at": _utc_now().isoformat(),
        "subscription.renewal_given_id": "",
        "subscription.renewal_attempt_due_at": "",
        "subscription.renewal_billing_period_start": due_at,
        "subscription.renewal_lease_until": "",
        "updated_at": int(_utc_now().timestamp() * 1000),
    }
    if payment_id:
        update["subscription.payment_reference"] = payment_id
    raw_users_collection.update_one({"_id": user["_id"]}, {"$set": update})
    if payment:
        _record_renewal_payment(user, payment, due_at=due_at)
    return {
        "user_id": _clean(user.get("user_id")),
        "status": "past_due" if will_retry else "inactive",
        "payment_id": payment_id,
        "retry_count": retry_count,
        "next_charge_at": next_charge_at,
    }


def _charge_due_subscription(
    user: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    reference = (now or _utc_now()).astimezone(timezone.utc)
    subscription = (
        user.get("subscription") if isinstance(user.get("subscription"), dict) else {}
    )
    plan = get_subscription_plan(subscription.get("plan_code"))
    charge_at = _clean(subscription.get("next_charge_at"))
    billing_period_start = (
        _clean(subscription.get("renewal_billing_period_start"))
        or _clean(subscription.get("current_period_end"))
        or charge_at
    )
    token = _clean(subscription.get("gateway_payment_token"))
    if not plan or not charge_at or not billing_period_start or not token:
        return {
            "user_id": _clean(user.get("user_id")),
            "status": "skipped",
            "reason": "Missing plan, renewal date, or saved payment token.",
        }

    retry_count = int(subscription.get("renewal_retry_count") or 0)
    previous_attempt_number = subscription.get("renewal_attempt_number")
    existing_attempt_matches = (
        _clean(subscription.get("renewal_attempt_due_at")) == charge_at
        and int(previous_attempt_number if previous_attempt_number is not None else -1)
        == retry_count
        and bool(_clean(subscription.get("renewal_given_id")))
    )
    given_id = (
        _clean(subscription.get("renewal_given_id"))
        if existing_attempt_matches
        else str(uuid4())
    )

    claim = raw_users_collection.update_one(
        {
            "_id": user["_id"],
            "subscription.status": {"$in": ["active", "past_due"]},
            "subscription.auto_renew": True,
            "subscription.next_charge_at": charge_at,
            "subscription.gateway_payment_token": {"$nin": ["", None]},
            "$or": [
                {"subscription.renewal_processing": {"$ne": True}},
                {"subscription.renewal_lease_until": {"$lte": reference.isoformat()}},
            ],
        },
        {
            "$set": {
                "subscription.renewal_processing": True,
                "subscription.renewal_given_id": given_id,
                "subscription.renewal_attempt_due_at": charge_at,
                "subscription.renewal_attempt_number": retry_count,
                "subscription.renewal_started_at": reference.isoformat(),
                "subscription.renewal_lease_until": (
                    reference + timedelta(minutes=_RENEWAL_PROCESSING_LEASE_MINUTES)
                ).isoformat(),
            }
        },
    )
    if claim.modified_count != 1:
        return {
            "user_id": _clean(user.get("user_id")),
            "status": "skipped",
            "reason": "Renewal is already being processed.",
        }

    request_body = {
        "given_id": given_id,
        "amount": int(plan["amount_minor"]),
        "currency": plan["currency"],
        "description": f"{plan['plan_name']} monthly subscription renewal",
        "callback_url": _renewal_callback_url(),
        "metadata": {
            "subscription_renewal_user_id": _clean(user.get("user_id")),
            "plan_code": plan["plan_code"],
            "billing_period_start": billing_period_start,
        },
        "source": {
            "type": "token",
            "token": token,
        },
    }

    try:
        response = requests.post(
            _MOYASAR_PAYMENT_API,
            auth=(MOYASAR_SECRET_KEY, ""),
            json=request_body,
            timeout=30,
        )
    except requests.RequestException as exc:
        raw_users_collection.update_one(
            {"_id": user["_id"], "subscription.renewal_given_id": given_id},
            {
                "$set": {
                    "subscription.renewal_processing": False,
                    "subscription.renewal_last_error": "Moyasar renewal request was interrupted.",
                    "subscription.renewal_lease_until": "",
                }
            },
        )
        raise RuntimeError("Moyasar renewal request was interrupted.") from exc

    try:
        payment = response.json()
    except ValueError:
        payment = {}
    if not isinstance(payment, dict):
        payment = {}

    if response.status_code >= 500:
        raw_users_collection.update_one(
            {"_id": user["_id"], "subscription.renewal_given_id": given_id},
            {
                "$set": {
                    "subscription.renewal_processing": False,
                    "subscription.renewal_last_error": (
                        "Moyasar returned a temporary server error. "
                        "The same idempotent renewal will be retried."
                    ),
                    "subscription.renewal_lease_until": "",
                }
            },
        )
        raise RuntimeError("Moyasar renewal request returned a temporary server error.")

    if response.status_code not in {200, 201}:
        message = (
            _clean(payment.get("message")) or "Moyasar rejected the renewal request."
        )
        return _mark_renewal_failed(
            user,
            due_at=billing_period_start,
            message=message,
            payment=payment or None,
        )

    returned_payment_id = _clean(payment.get("id"))
    if returned_payment_id != given_id:
        raw_users_collection.update_one(
            {"_id": user["_id"], "subscription.renewal_given_id": given_id},
            {
                "$set": {
                    "subscription.renewal_processing": False,
                    "subscription.renewal_last_error": (
                        "Moyasar returned a payment id that does not match "
                        "the idempotent renewal id."
                    ),
                    "subscription.renewal_lease_until": "",
                }
            },
        )
        raise RuntimeError(
            "Moyasar renewal payment id did not match the renewal attempt."
        )

    if _clean(payment.get("status")).lower() == "paid":
        return _mark_renewal_paid(user, payment, due_at=billing_period_start)

    payment_status = _clean(payment.get("status")).lower()
    if payment_status not in {"failed"}:
        raw_users_collection.update_one(
            {"_id": user["_id"], "subscription.renewal_given_id": given_id},
            {
                "$set": {
                    "subscription.renewal_processing": False,
                    "subscription.renewal_last_error": (
                        f"Renewal payment status is {payment_status or 'unknown'}; "
                        "the same idempotent payment will be reconciled."
                    ),
                    "subscription.renewal_lease_until": "",
                }
            },
        )
        return {
            "user_id": _clean(user.get("user_id")),
            "status": "pending",
            "payment_id": _clean(payment.get("id")) or given_id,
        }

    message = (
        _clean((payment.get("source") or {}).get("message"))
        if isinstance(payment.get("source"), dict)
        else ""
    ) or f"Renewal payment status is {_clean(payment.get('status')) or 'unknown'}."
    return _mark_renewal_failed(
        user,
        due_at=billing_period_start,
        message=message,
        payment=payment,
    )


def process_due_subscription_renewals(
    *,
    now: Optional[datetime] = None,
    limit: int = 100,
) -> dict[str, Any]:
    reference = (now or _utc_now()).astimezone(timezone.utc)
    configuration_error = _renewal_configuration_error()
    if configuration_error:
        return {
            "processed_count": 0,
            "paid_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "error": configuration_error,
        }

    users = list(
        raw_users_collection.find(
            {
                "subscription.status": {"$in": ["active", "past_due"]},
                "subscription.auto_renew": True,
                "subscription.next_charge_at": {"$lte": reference.isoformat()},
                "subscription.gateway_payment_token": {"$nin": ["", None]},
            }
        ).limit(max(1, min(int(limit), 500)))
    )
    results = []
    for user in users:
        try:
            results.append(_charge_due_subscription(user, now=reference))
        except Exception as exc:
            logger.exception(
                "Subscription renewal failed for user %s",
                _clean(user.get("user_id")),
            )
            results.append(
                {
                    "user_id": _clean(user.get("user_id")),
                    "status": "error",
                    "reason": _clean(exc),
                }
            )

    return {
        "processed_count": len(results),
        "paid_count": sum(result.get("status") == "paid" for result in results),
        "failed_count": sum(
            result.get("status") in {"past_due", "inactive", "error"}
            for result in results
        ),
        "skipped_count": sum(result.get("status") == "skipped" for result in results),
        "results": results,
    }


def reconcile_moyasar_renewal_payment(payment: dict[str, Any]) -> dict[str, Any]:
    metadata = payment.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("Renewal metadata is missing.")
    user_id = _clean(metadata.get("subscription_renewal_user_id"))
    due_at = _clean(metadata.get("billing_period_start"))
    if not user_id or not due_at:
        raise ValueError("Renewal metadata is incomplete.")

    user = raw_users_collection.find_one({"user_id": user_id})
    if not user:
        raise ValueError("Renewal account was not found.")
    subscription = (
        user.get("subscription") if isinstance(user.get("subscription"), dict) else {}
    )
    due_datetime = _parse_datetime(due_at)
    current_period_start = _parse_datetime(subscription.get("current_period_start"))
    if due_datetime and current_period_start and current_period_start >= due_datetime:
        return {
            "user_id": user_id,
            "status": "already_processed",
            "payment_id": _clean(payment.get("id")),
        }

    if _clean(subscription.get("payment_reference")) == _clean(payment.get("id")):
        return {
            "user_id": user_id,
            "status": "already_processed",
            "payment_id": _clean(payment.get("id")),
        }

    expected_period_starts = {
        _clean(subscription.get("current_period_end")),
        _clean(subscription.get("renewal_billing_period_start")),
        _clean(subscription.get("next_charge_at")),
    }
    expected_period_starts.discard("")
    if due_at not in expected_period_starts:
        raise ValueError(
            "Renewal billing period does not match the current subscription."
        )

    if _clean(payment.get("status")).lower() == "paid":
        return _mark_renewal_paid(user, payment, due_at=due_at)
    return _mark_renewal_failed(
        user,
        due_at=due_at,
        message="Renewal payment failed.",
        payment=payment,
    )


def run_subscription_billing_scheduler() -> None:
    while True:
        try:
            result = process_due_subscription_renewals()
            if result.get("processed_count"):
                logger.info(
                    "Subscription renewals processed=%s paid=%s failed=%s skipped=%s",
                    result.get("processed_count"),
                    result.get("paid_count"),
                    result.get("failed_count"),
                    result.get("skipped_count"),
                )
        except Exception:
            logger.exception("Subscription billing scheduler failed")
        time.sleep(max(60, int(SUBSCRIPTION_RENEWAL_POLL_SECONDS)))


def ensure_subscription_billing_scheduler_started() -> None:
    global _scheduler_started

    if APP_SURFACE != "lite" or not SUBSCRIPTION_RENEWAL_SCHEDULER_ENABLED:
        logger.info("Subscription billing scheduler is disabled")
        return
    configuration_error = _renewal_configuration_error()
    if configuration_error:
        logger.warning(
            "Subscription billing scheduler cannot start: %s",
            configuration_error,
        )
        return

    with _scheduler_lock:
        if _scheduler_started:
            return
        thread = threading.Thread(
            target=run_subscription_billing_scheduler,
            name="subscription-billing-scheduler",
            daemon=True,
        )
        thread.start()
        _scheduler_started = True
        logger.info("Subscription billing scheduler started")
