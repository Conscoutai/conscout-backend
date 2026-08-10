from __future__ import annotations

from typing import Any, Optional


SUBSCRIPTION_PLANS: dict[str, dict[str, Any]] = {
    "tier_1": {
        "plan_code": "tier_1",
        "plan_name": "Tier 1",
        "monthly_price_usd": 10,
        "currency": "USD",
        "amount_minor": 1000,
        "project_limit": 4,
    },
    "tier_2": {
        "plan_code": "tier_2",
        "plan_name": "Tier 2",
        "monthly_price_usd": 39,
        "currency": "USD",
        "amount_minor": 3900,
        "project_limit": 10,
    },
    "tier_3": {
        "plan_code": "tier_3",
        "plan_name": "Tier 3",
        "monthly_price_usd": 100,
        "currency": "USD",
        "amount_minor": 10000,
        "project_limit": None,
    },
}


def get_subscription_plan(plan_code: Any) -> Optional[dict[str, Any]]:
    normalized = str(plan_code or "").strip().lower()
    plan = SUBSCRIPTION_PLANS.get(normalized)
    return dict(plan) if plan else None
