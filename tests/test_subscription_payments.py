from __future__ import annotations

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from api.routes import auth as auth_routes
from api.routes import subscriptions
from services import subscription_access_service
from services import subscription_billing_service


def _nested_value(document, path):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _matches(document, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(document, branch) for branch in expected):
                return False
            continue
        actual = _nested_value(document, key)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif isinstance(expected, dict) and "$nin" in expected:
            if actual in expected["$nin"]:
                return False
        elif isinstance(expected, dict) and "$lte" in expected:
            if actual is None or actual > expected["$lte"]:
                return False
        elif isinstance(expected, dict) and "$ne" in expected:
            if actual == expected["$ne"]:
                return False
        elif actual != expected:
            return False
    return True


def _set_nested(document, path, value):
    target = document
    parts = path.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = [deepcopy(document) for document in (documents or [])]

    def find_one(self, query):
        return next(
            (document for document in self.documents if _matches(document, query)),
            None,
        )

    def find(self, query, *_args, **_kwargs):
        documents = [
            document for document in self.documents if _matches(document, query)
        ]

        class Cursor(list):
            def limit(self, value):
                return Cursor(self[:value])

        return Cursor(documents)

    def update_one(self, query, update, upsert=False):
        document = self.find_one(query)
        if not document:
            if not upsert:
                return SimpleNamespace(modified_count=0)
            document = {
                key: value
                for key, value in query.items()
                if not isinstance(value, dict)
            }
            self.documents.append(document)
        for path, value in update.get("$set", {}).items():
            _set_nested(document, path, value)
        for path, value in update.get("$setOnInsert", {}).items():
            if _nested_value(document, path) is None:
                _set_nested(document, path, value)
        for path in update.get("$unset", {}):
            target = document
            parts = path.split(".")
            for part in parts[:-1]:
                target = target.get(part, {})
            target.pop(parts[-1], None)
        return SimpleNamespace(modified_count=1)

    def update_many(self, query, update):
        modified_count = 0
        for document in self.documents:
            if not _matches(document, query):
                continue
            for path, value in update.get("$set", {}).items():
                _set_nested(document, path, value)
            for path in update.get("$unset", {}):
                target = document
                parts = path.split(".")
                for part in parts[:-1]:
                    target = target.get(part, {})
                target.pop(parts[-1], None)
            modified_count += 1
        return SimpleNamespace(modified_count=modified_count)

    def insert_one(self, document):
        self.documents.append(deepcopy(document))
        return SimpleNamespace(inserted_id=document.get("_id"))

    def delete_many(self, query):
        before = len(self.documents)
        self.documents = [
            document for document in self.documents if not _matches(document, query)
        ]
        return SimpleNamespace(deleted_count=before - len(self.documents))


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/subscriptions/checkout-session",
            "raw_path": b"/subscriptions/checkout-session",
            "query_string": b"",
            "headers": [(b"origin", b"https://conscout.com")],
            "server": ("api.conscout.com", 443),
            "client": ("127.0.0.1", 12345),
            "root_path": "",
        }
    )


class SubscriptionPaymentTests(unittest.TestCase):
    def test_http_checkout_webhook_callback_and_entitlement_flow(self):
        users = FakeCollection(
            [
                {
                    "_id": "mongo-user-1",
                    "user_id": "user-1",
                    "email": "user@example.com",
                    "name": "HTTP Test User",
                    "workspace": "Old workspace",
                    "subscription": {},
                }
            ]
        )
        sessions = FakeCollection()
        requests = FakeCollection()
        app = FastAPI()
        app.include_router(subscriptions.router)
        app.dependency_overrides[subscriptions.require_authenticated_user] = (
            lambda: SimpleNamespace(
                user_id="user-1",
                email="user@example.com",
            )
        )

        with patch.multiple(
            subscriptions,
            APP_SURFACE="lite",
            PUBLIC_API_BASE_URL="https://api.example.test",
            MOYASAR_PUBLISHABLE_KEY="pk_test_http_flow",
            MOYASAR_SECRET_KEY="sk_test_http_flow",
            MOYASAR_WEBHOOK_SECRET="http-flow-webhook-secret",
            raw_users_collection=users,
            raw_subscription_checkout_sessions_collection=sessions,
            raw_subscription_requests_collection=requests,
        ):
            with TestClient(app) as client:
                checkout_response = client.post(
                    "/subscriptions/checkout-session",
                    headers={
                        "Authorization": "Bearer http-flow-token",
                        "Origin": "https://conscout.com",
                    },
                    json={
                        "plan_code": "tier_1",
                        "company_name": "HTTP Flow Co",
                        "billing_contact_name": "HTTP Test User",
                        "billing_email": "billing@example.com",
                        "payment_method": "card",
                        "return_url": "https://conscout.com/plans?tier=tier_1",
                    },
                )
                self.assertEqual(checkout_response.status_code, 200)
                checkout = checkout_response.json()
                session = sessions.find_one({"session_id": checkout["session_id"]})
                self.assertIsNotNone(session)
                self.assertNotIn("authorization_token_hint", session)
                access_key = session["access_key"]
                payment_id = session["payment_given_id"]

                page_response = client.get(
                    f"/subscriptions/checkout/{session['session_id']}",
                    params={"access_key": access_key},
                )
                self.assertEqual(page_response.status_code, 200)
                self.assertIn("pk_test_http_flow", page_response.text)
                self.assertNotIn("sk_test_http_flow", page_response.text)

                authorize_response = client.post(
                    f"/subscriptions/checkout/{session['session_id']}/authorize",
                    params={"access_key": access_key},
                )
                self.assertEqual(authorize_response.status_code, 200)

                payment = {
                    "id": payment_id,
                    "status": "paid",
                    "amount": 1000,
                    "currency": "USD",
                    "source": {
                        "type": "creditcard",
                        "company": "visa",
                        "number": "XXXX-XXXX-XXXX-1111",
                        "token": "saved-token-1",
                    },
                    "metadata": {
                        "subscription_session_id": session["session_id"],
                        "plan_code": "tier_1",
                        "user_id": "user-1",
                    },
                }
                moyasar_response = Mock(status_code=200)
                moyasar_response.json.return_value = payment
                with patch.object(
                    subscriptions.requests,
                    "get",
                    return_value=moyasar_response,
                ) as fetch_payment:
                    webhook_response = client.post(
                        "/subscriptions/moyasar/webhook",
                        json={
                            "secret_token": "http-flow-webhook-secret",
                            "live": False,
                            "type": "payment_paid",
                            "data": payment,
                        },
                    )
                    self.assertEqual(webhook_response.status_code, 200)
                    self.assertEqual(webhook_response.json()["status"], "paid")

                    callback_response = client.get(
                        f"/subscriptions/checkout/{session['session_id']}/callback",
                        params={"access_key": access_key, "id": payment_id},
                        follow_redirects=False,
                    )
                    self.assertEqual(callback_response.status_code, 303)
                    self.assertIn(
                        "checkout_status=success",
                        callback_response.headers["location"],
                    )
                    self.assertEqual(fetch_payment.call_count, 1)

        activated = users.find_one({"user_id": "user-1"})["subscription"]
        self.assertEqual(activated["plan_code"], "tier_1")
        self.assertEqual(activated["payment_status"], "paid")
        self.assertEqual(
            sessions.find_one({"session_id": session["session_id"]})["status"],
            "paid",
        )

        projects = FakeCollection(
            [
                {
                    "_id": f"project-{index}",
                    "project_id": f"project-{index}",
                    "owner_user_id": "user-1",
                }
                for index in range(4)
            ]
        )
        with patch.multiple(
            subscription_access_service,
            APP_SURFACE="lite",
            raw_users_collection=users,
            raw_floorplans_collection=projects,
        ):
            with self.assertRaises(HTTPException) as limit_context:
                subscription_access_service.reserve_lite_project_creation(
                    user_id="user-1"
                )
        self.assertIn("allows 4 projects", str(limit_context.exception.detail))

    def test_login_payload_never_exposes_saved_moyasar_token(self):
        payload = auth_routes.sanitize_user_payload(
            {
                "user_id": "user-1",
                "email": "user@example.com",
                "subscription": {
                    "plan_code": "tier_1",
                    "status": "active",
                    "payment_status": "paid",
                    "current_period_end": "2099-01-01T00:00:00+00:00",
                    "auto_renew": True,
                    "gateway_payment_token": "token-must-stay-on-server",
                    "payment_reference": "payment-private-reference",
                },
            }
        )

        subscription = payload["subscription"]
        self.assertTrue(subscription["auto_renew"])
        self.assertNotIn("gateway_payment_token", subscription)
        self.assertNotIn("payment_reference", subscription)

    def test_server_catalog_owns_price_and_allowance(self):
        plan = subscriptions._checkout_plan("tier_2")
        self.assertEqual(plan["monthly_price_usd"], 39)
        self.assertEqual(plan["project_limit"], 10)

        with self.assertRaises(ValidationError):
            subscriptions.SubscriptionCheckoutSessionPayload(
                plan_code="tier_2",
                company_name="ConScout Test",
                billing_contact_name="Test User",
                billing_email="billing@example.com",
                payment_method="card",
                return_url="https://conscout.com/plans?tier=tier_2",
                monthly_price_usd=1,
            )
        payload = subscriptions.SubscriptionCheckoutSessionPayload(
            plan_code="tier_2",
            company_name="ConScout Test",
            billing_contact_name="Test User",
            billing_email="billing@example.com",
            payment_method="card",
            return_url="https://conscout.com/plans?tier=tier_2",
        )
        session = subscriptions._build_checkout_session(
            session_id="session-1",
            access_key="access-key",
            user={
                "user_id": "user-1",
                "email": "user@example.com",
                "name": "Test User",
            },
            plan=plan,
            payload=payload,
            request=_request(),
            created_at="2026-08-07T10:00:00+00:00",
            updated_at="2026-08-07T10:00:00+00:00",
        )

        self.assertEqual(session["plan_name"], "Tier 2")
        self.assertEqual(session["monthly_price_usd"], 39)
        self.assertEqual(session["amount_minor"], 3900)
        self.assertRegex(
            session["payment_given_id"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )

    def test_checkout_return_url_cannot_leave_request_origin(self):
        normalized = subscriptions._normalize_return_url(
            "https://attacker.example/payment-complete",
            _request(),
        )
        self.assertEqual(normalized, "")
        self.assertEqual(
            subscriptions._normalize_return_url(
                "http://conscout.com/payment-complete",
                _request(),
            ),
            "",
        )

    def test_unknown_plan_is_rejected(self):
        with self.assertRaises(HTTPException) as context:
            subscriptions._checkout_plan("made_up_plan")
        self.assertEqual(context.exception.status_code, 400)

    def test_checkout_config_is_safe_inside_an_inline_script(self):
        encoded = subscriptions._json_for_inline_script(
            {"description": "</script><script>alert('xss')</script>"}
        )
        self.assertNotIn("</script>", encoded.lower())
        self.assertIn("\\u003c", encoded)

    def test_payment_requires_exact_status_amount_currency_method_and_metadata(self):
        session = {
            "session_id": "session-1",
            "payment_given_id": "payment-1",
            "user_id": "user-1",
            "plan_code": "tier_1",
            "amount_minor": 1000,
            "currency": "USD",
            "gateway_method": "creditcard",
        }
        payment = {
            "id": "payment-1",
            "status": "paid",
            "amount": 1000,
            "currency": "USD",
            "source": {"type": "creditcard"},
            "metadata": {
                "subscription_session_id": "session-1",
                "plan_code": "tier_1",
                "user_id": "user-1",
            },
        }

        subscriptions._verify_checkout_payment(session, payment)

        tampered = {
            **payment,
            "metadata": {**payment["metadata"], "subscription_session_id": "other"},
        }
        with self.assertRaises(HTTPException):
            subscriptions._verify_checkout_payment(session, tampered)

        with self.assertRaises(HTTPException):
            subscriptions._verify_checkout_payment(
                session,
                {**payment, "id": "payment-from-another-checkout"},
            )
        with self.assertRaises(HTTPException):
            subscriptions._verify_checkout_payment(
                session,
                {key: value for key, value in payment.items() if key != "source"},
            )

    def test_failed_webhook_closes_checkout_so_a_new_payment_can_start(self):
        sessions = FakeCollection(
            [
                {
                    "session_id": "session-1",
                    "status": "ready",
                    "payment_status": "",
                }
            ]
        )
        webhook = {
            "secret_token": "webhook-secret",
            "live": False,
            "type": "payment_failed",
            "data": {
                "id": "payment-1",
                "status": "failed",
                "source": {"message": "Card declined"},
                "metadata": {"subscription_session_id": "session-1"},
            },
        }

        with patch.multiple(
            subscriptions,
            MOYASAR_WEBHOOK_SECRET="webhook-secret",
            MOYASAR_SECRET_KEY="sk_test_example",
            raw_subscription_checkout_sessions_collection=sessions,
        ):
            result = subscriptions.moyasar_webhook(webhook)

        failed = sessions.find_one({"session_id": "session-1"})
        self.assertTrue(result["handled"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["payment_status"], "failed")
        self.assertTrue(failed["failed_at"])

    def test_paid_subscription_has_monthly_period_and_saved_token(self):
        subscription = subscriptions._build_paid_subscription(
            {
                "session_id": "session-1",
                "plan_code": "tier_1",
                "plan_name": "Tier 1",
                "monthly_price_usd": 10,
                "project_limit": 4,
                "company_name": "ConScout Test",
                "billing_contact_name": "Test User",
                "billing_email": "billing@example.com",
                "payment_method": "card",
            },
            {
                "id": "payment-1",
                "amount": 1000,
                "currency": "USD",
                "source": {
                    "type": "creditcard",
                    "company": "visa",
                    "number": "XXXX-XXXX-XXXX-1111",
                    "token": "token-1",
                },
            },
            activated_at="2026-01-31T10:00:00+00:00",
        )

        self.assertEqual(
            subscription["current_period_end"], "2026-02-28T10:00:00+00:00"
        )
        self.assertEqual(
            subscription["next_charge_at"], subscription["current_period_end"]
        )
        self.assertTrue(subscription["auto_renew"])
        self.assertEqual(subscription["gateway_card_last_four"], "1111")

    def test_plan_changes_only_after_verified_payment(self):
        session = {
            "session_id": "session-1",
            "access_key": "access-key",
            "payment_given_id": "payment-1",
            "user_id": "user-1",
            "plan_code": "tier_2",
            "plan_name": "Tier 2",
            "monthly_price_usd": 39,
            "project_limit": 10,
            "company_name": "ConScout Test",
            "billing_contact_name": "Test User",
            "billing_email": "billing@example.com",
            "payment_method": "card",
            "gateway_method": "creditcard",
            "amount_minor": 3900,
            "currency": "USD",
            "status": "pending_checkout",
        }
        payment = {
            "id": "payment-1",
            "status": "paid",
            "amount": 3900,
            "currency": "USD",
            "source": {
                "type": "creditcard",
                "company": "visa",
                "number": "XXXX-XXXX-XXXX-1111",
                "token": "token-1",
            },
            "metadata": {
                "subscription_session_id": "session-1",
                "plan_code": "tier_2",
                "user_id": "user-1",
            },
        }
        sessions = FakeCollection([session])
        users = FakeCollection(
            [
                {
                    "_id": "mongo-user-1",
                    "user_id": "user-1",
                    "workspace": "Old workspace",
                    "subscription": {
                        "plan_code": "starter_access",
                        "status": "active",
                    },
                }
            ]
        )
        requests = FakeCollection(
            [
                {
                    "request_id": "legacy-request",
                    "user_id": "user-1",
                    "status": "pending_approval",
                }
            ]
        )

        with patch.multiple(
            subscriptions,
            raw_subscription_checkout_sessions_collection=sessions,
            raw_users_collection=users,
            raw_subscription_requests_collection=requests,
        ):
            invalid_payment = {**payment, "amount": 1}
            with self.assertRaises(HTTPException):
                subscriptions._activate_verified_checkout(session, invalid_payment)
            self.assertEqual(
                users.find_one({"user_id": "user-1"})["subscription"]["plan_code"],
                "starter_access",
            )

            subscriptions._activate_verified_checkout(session, payment)

        self.assertEqual(
            users.find_one({"user_id": "user-1"})["subscription"]["plan_code"],
            "tier_2",
        )
        self.assertEqual(
            sessions.find_one({"session_id": "session-1"})["status"],
            "paid",
        )
        self.assertEqual(
            requests.find_one({"request_id": "legacy-request"})["status"],
            "completed_by_payment",
        )

    def test_verified_payment_still_activates_if_checkout_was_replaced(self):
        session = {
            "session_id": "session-replaced",
            "payment_given_id": "payment-replaced",
            "user_id": "user-1",
            "plan_code": "tier_1",
            "plan_name": "Tier 1",
            "monthly_price_usd": 10,
            "project_limit": 4,
            "company_name": "ConScout Test",
            "billing_contact_name": "Test User",
            "billing_email": "billing@example.com",
            "payment_method": "card",
            "gateway_method": "creditcard",
            "amount_minor": 1000,
            "currency": "USD",
            "status": "replaced",
        }
        payment = {
            "id": "payment-replaced",
            "status": "paid",
            "amount": 1000,
            "currency": "USD",
            "source": {"type": "creditcard"},
            "metadata": {
                "subscription_session_id": "session-replaced",
                "plan_code": "tier_1",
                "user_id": "user-1",
            },
        }
        sessions = FakeCollection([session])
        users = FakeCollection(
            [{"_id": "mongo-user-1", "user_id": "user-1", "subscription": {}}]
        )

        with patch.multiple(
            subscriptions,
            raw_subscription_checkout_sessions_collection=sessions,
            raw_users_collection=users,
            raw_subscription_requests_collection=FakeCollection(),
        ):
            subscriptions._activate_verified_checkout(session, payment)

        self.assertEqual(
            users.find_one({"user_id": "user-1"})["subscription"]["plan_code"],
            "tier_1",
        )

    def test_lite_admin_approval_paths_are_disabled(self):
        request_payload = subscriptions.SubscriptionRequestPayload(
            plan_code="tier_1",
            plan_name="Tier 1",
            monthly_price_usd=10,
            project_limit=4,
            company_name="ConScout Test",
            billing_contact_name="Test User",
            billing_email="billing@example.com",
        )
        current_user = SimpleNamespace(user_id="admin-1", email="admin@example.com")

        with patch.object(subscriptions, "APP_SURFACE", "lite"):
            with self.assertRaises(HTTPException) as request_context:
                subscriptions.create_or_update_subscription_request(
                    request_payload,
                    current_user,
                )
            self.assertEqual(request_context.exception.status_code, 410)

            with patch.object(
                subscriptions,
                "ensure_subscription_admin_user",
                return_value=None,
            ):
                with self.assertRaises(HTTPException) as approval_context:
                    subscriptions.approve_subscription_request(
                        "legacy-request",
                        None,
                        current_user,
                    )
                self.assertEqual(approval_context.exception.status_code, 410)

        with patch.object(
            auth_routes,
            "_admin_product_collections",
            return_value=(FakeCollection(), FakeCollection(), "lite"),
        ):
            with self.assertRaises(HTTPException) as central_context:
                auth_routes._review_admin_subscription_request(
                    app="lite",
                    request_id="legacy-request",
                    approve=True,
                    current_user=current_user,
                )
            self.assertEqual(central_context.exception.status_code, 410)

    def test_customer_can_stop_renewal_without_losing_current_paid_period(self):
        users = FakeCollection(
            [
                {
                    "_id": "mongo-user-1",
                    "user_id": "user-1",
                    "email": "user@example.com",
                    "subscription": {
                        "plan_code": "tier_1",
                        "plan_name": "Tier 1",
                        "source": "moyasar_checkout",
                        "status": "active",
                        "payment_status": "paid",
                        "payment_reference": "payment-1",
                        "current_period_end": "2099-01-01T00:00:00+00:00",
                        "next_charge_at": "2099-01-01T00:00:00+00:00",
                        "auto_renew": True,
                    },
                }
            ]
        )

        with patch.multiple(
            subscriptions,
            APP_SURFACE="lite",
            raw_users_collection=users,
        ):
            result = subscriptions.cancel_subscription_renewal(
                SimpleNamespace(user_id="user-1")
            )

        cancelled = users.find_one({"user_id": "user-1"})["subscription"]
        self.assertFalse(cancelled["auto_renew"])
        self.assertEqual(cancelled["next_charge_at"], "")
        self.assertEqual(cancelled["status"], "active")
        self.assertIn("remains available", result["message"])

    def test_lite_project_limit_uses_only_a_verified_paid_plan(self):
        users = FakeCollection(
            [
                {
                    "_id": "mongo-user-1",
                    "user_id": "user-1",
                    "email": "user@example.com",
                    "subscription": {
                        "plan_code": "tier_1",
                        "project_limit": 999,
                        "status": "active",
                        "payment_status": "paid",
                        "current_period_end": "2099-01-01T00:00:00+00:00",
                    },
                }
            ]
        )
        projects = FakeCollection(
            [
                {
                    "_id": f"project-{index}",
                    "project_id": f"project-{index}",
                    "owner_user_id": "user-1",
                }
                for index in range(4)
            ]
        )

        with patch.multiple(
            subscription_access_service,
            APP_SURFACE="lite",
            raw_users_collection=users,
            raw_floorplans_collection=projects,
        ):
            with self.assertRaises(HTTPException) as limit_context:
                subscription_access_service.reserve_lite_project_creation(
                    user_id="user-1",
                )

        self.assertEqual(limit_context.exception.status_code, 403)
        self.assertIn("allows 4 projects", str(limit_context.exception.detail))

    def test_unpaid_lite_plan_cannot_unlock_paid_project_allowance(self):
        users = FakeCollection(
            [
                {
                    "_id": "mongo-user-1",
                    "user_id": "user-1",
                    "email": "user@example.com",
                    "subscription": {
                        "plan_code": "tier_3",
                        "project_limit": None,
                        "status": "active",
                        "payment_status": "approved",
                    },
                }
            ]
        )
        projects = FakeCollection(
            [
                {
                    "_id": "project-1",
                    "project_id": "project-1",
                    "owner_user_id": "user-1",
                }
            ]
        )

        with patch.multiple(
            subscription_access_service,
            APP_SURFACE="lite",
            raw_users_collection=users,
            raw_floorplans_collection=projects,
        ):
            with self.assertRaises(HTTPException) as limit_context:
                subscription_access_service.reserve_lite_project_creation(
                    user_id="user-1",
                )

        self.assertEqual(limit_context.exception.status_code, 403)
        self.assertIn("allows 1 project", str(limit_context.exception.detail))

    def test_tier_three_paid_subscription_has_unlimited_project_creation(self):
        users = FakeCollection(
            [
                {
                    "_id": "mongo-user-1",
                    "user_id": "user-1",
                    "email": "user@example.com",
                    "subscription": {
                        "plan_code": "tier_3",
                        "status": "active",
                        "payment_status": "paid",
                        "current_period_end": "2099-01-01T00:00:00+00:00",
                    },
                }
            ]
        )
        projects = FakeCollection(
            [
                {
                    "_id": f"project-{index}",
                    "project_id": f"project-{index}",
                    "owner_user_id": "user-1",
                }
                for index in range(25)
            ]
        )

        with patch.multiple(
            subscription_access_service,
            APP_SURFACE="lite",
            raw_users_collection=users,
            raw_floorplans_collection=projects,
        ):
            lease_token = subscription_access_service.reserve_lite_project_creation(
                user_id="user-1",
            )

        self.assertEqual(lease_token, "")

    def test_expired_paid_period_cannot_unlock_a_paid_project_allowance(self):
        users = FakeCollection(
            [
                {
                    "_id": "mongo-user-1",
                    "user_id": "user-1",
                    "email": "user@example.com",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "subscription": {
                        "plan_code": "tier_3",
                        "status": "active",
                        "payment_status": "paid",
                        "current_period_end": "2026-02-01T00:00:00+00:00",
                    },
                }
            ]
        )

        with patch.multiple(
            subscription_access_service,
            APP_SURFACE="lite",
            raw_users_collection=users,
            raw_floorplans_collection=FakeCollection(),
        ):
            with self.assertRaises(HTTPException) as expiry_context:
                subscription_access_service.reserve_lite_project_creation(
                    user_id="user-1",
                    now=subscription_access_service._parse_datetime(
                        "2026-03-01T00:00:00+00:00"
                    ),
                )

        self.assertEqual(expiry_context.exception.status_code, 403)
        self.assertIn(
            "30-day Lite workspace access has ended",
            str(expiry_context.exception.detail),
        )

    def test_due_saved_token_is_charged_and_period_advances(self):
        due_at = "2026-02-28T10:00:00+00:00"
        users = FakeCollection(
            [
                {
                    "_id": "mongo-user-1",
                    "user_id": "user-1",
                    "subscription": {
                        "plan_code": "tier_1",
                        "status": "active",
                        "payment_status": "paid",
                        "auto_renew": True,
                        "gateway_payment_token": "token-1",
                        "next_charge_at": due_at,
                        "renewal_retry_count": 0,
                        "renewal_given_id": "renewal-payment-1",
                        "renewal_attempt_due_at": due_at,
                        "renewal_attempt_number": 0,
                    },
                }
            ]
        )
        payments = FakeCollection()
        payment = {
            "id": "renewal-payment-1",
            "status": "paid",
            "amount": 1000,
            "currency": "USD",
            "source": {"type": "token"},
            "metadata": {
                "subscription_renewal_user_id": "user-1",
                "plan_code": "tier_1",
                "billing_period_start": due_at,
            },
        }
        response = Mock(status_code=201)
        response.json.return_value = payment

        with patch.multiple(
            subscription_billing_service,
            raw_users_collection=users,
            raw_subscription_payments_collection=payments,
            MOYASAR_SECRET_KEY="sk_test_example",
        ), patch.object(
            subscription_billing_service.requests,
            "post",
            return_value=response,
        ):
            result = subscription_billing_service._charge_due_subscription(
                users.find_one({"user_id": "user-1"})
            )

        self.assertEqual(result["status"], "paid")
        renewed = users.find_one({"user_id": "user-1"})["subscription"]
        self.assertEqual(renewed["current_period_start"], due_at)
        self.assertEqual(
            renewed["current_period_end"],
            "2026-03-28T10:00:00+00:00",
        )
        self.assertEqual(len(payments.documents), 1)

    def test_renewal_scheduler_requires_a_valid_public_callback_url(self):
        with patch.multiple(
            subscription_billing_service,
            MOYASAR_PUBLISHABLE_KEY="pk_test_example",
            MOYASAR_SECRET_KEY="sk_test_example",
            PUBLIC_API_BASE_URL="",
        ):
            result = subscription_billing_service.process_due_subscription_renewals()

        self.assertEqual(result["processed_count"], 0)
        self.assertIn("PUBLIC_API_BASE_URL", result["error"])

    def test_live_renewal_callback_must_use_https(self):
        with patch.multiple(
            subscription_billing_service,
            MOYASAR_PUBLISHABLE_KEY="pk_live_example",
            MOYASAR_SECRET_KEY="sk_live_example",
            PUBLIC_API_BASE_URL="http://payments.example.test",
        ):
            result = subscription_billing_service.process_due_subscription_renewals()

        self.assertEqual(result["processed_count"], 0)
        self.assertIn("HTTPS", result["error"])

    def test_renewal_5xx_retries_with_the_same_given_id(self):
        due_at = "2026-02-28T10:00:00+00:00"
        users = FakeCollection(
            [
                {
                    "_id": "mongo-user-1",
                    "user_id": "user-1",
                    "subscription": {
                        "plan_code": "tier_1",
                        "status": "active",
                        "payment_status": "paid",
                        "auto_renew": True,
                        "gateway_payment_token": "token-1",
                        "current_period_end": due_at,
                        "next_charge_at": due_at,
                        "renewal_retry_count": 0,
                    },
                }
            ]
        )
        payments = FakeCollection()
        attempted_given_ids = []

        def post_payment(*_args, **kwargs):
            given_id = kwargs["json"]["given_id"]
            attempted_given_ids.append(given_id)
            if len(attempted_given_ids) == 1:
                response = Mock(status_code=503)
                response.json.return_value = {"message": "temporary failure"}
                return response
            response = Mock(status_code=201)
            response.json.return_value = {
                "id": given_id,
                "status": "paid",
                "amount": 1000,
                "currency": "USD",
                "source": {"type": "token"},
                "metadata": {
                    "subscription_renewal_user_id": "user-1",
                    "plan_code": "tier_1",
                    "billing_period_start": due_at,
                },
            }
            return response

        with patch.multiple(
            subscription_billing_service,
            raw_users_collection=users,
            raw_subscription_payments_collection=payments,
            MOYASAR_SECRET_KEY="sk_test_example",
        ), patch.object(
            subscription_billing_service.requests,
            "post",
            side_effect=post_payment,
        ):
            with self.assertRaises(RuntimeError):
                subscription_billing_service._charge_due_subscription(
                    users.find_one({"user_id": "user-1"})
                )

            after_5xx = users.find_one({"user_id": "user-1"})["subscription"]
            self.assertFalse(after_5xx["renewal_processing"])
            self.assertTrue(after_5xx["renewal_given_id"])

            result = subscription_billing_service._charge_due_subscription(
                users.find_one({"user_id": "user-1"})
            )

        self.assertEqual(result["status"], "paid")
        self.assertEqual(len(attempted_given_ids), 2)
        self.assertEqual(attempted_given_ids[0], attempted_given_ids[1])

    def test_stale_renewal_webhook_cannot_roll_back_a_newer_period(self):
        users = FakeCollection(
            [
                {
                    "_id": "mongo-user-1",
                    "user_id": "user-1",
                    "subscription": {
                        "plan_code": "tier_1",
                        "status": "active",
                        "current_period_start": "2026-03-28T10:00:00+00:00",
                        "current_period_end": "2026-04-28T10:00:00+00:00",
                        "next_charge_at": "2026-04-28T10:00:00+00:00",
                        "payment_reference": "newer-payment",
                    },
                }
            ]
        )
        stale_payment = {
            "id": "older-payment",
            "status": "paid",
            "amount": 1000,
            "currency": "USD",
            "metadata": {
                "subscription_renewal_user_id": "user-1",
                "plan_code": "tier_1",
                "billing_period_start": "2026-02-28T10:00:00+00:00",
            },
        }

        with patch.multiple(
            subscription_billing_service,
            raw_users_collection=users,
            raw_subscription_payments_collection=FakeCollection(),
        ):
            result = subscription_billing_service.reconcile_moyasar_renewal_payment(
                stale_payment
            )

        self.assertEqual(result["status"], "already_processed")
        self.assertEqual(
            users.find_one({"user_id": "user-1"})["subscription"][
                "current_period_start"
            ],
            "2026-03-28T10:00:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()
