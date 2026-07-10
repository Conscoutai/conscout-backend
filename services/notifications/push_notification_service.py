from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from core.config import (
    FCM_ANDROID_CHANNEL_ID,
    FIREBASE_CREDENTIALS_FILE,
    FIREBASE_SERVICE_ACCOUNT_JSON,
)
from core.database import notification_devices_collection

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
except Exception:  # pragma: no cover - dependency/configuration guard
    firebase_admin = None
    credentials = None
    messaging = None


logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _firebase_ready() -> bool:
    return firebase_admin is not None and credentials is not None and messaging is not None


def _initialize_firebase() -> bool:
    if not _firebase_ready():
        return False
    if firebase_admin._apps:
        return True

    try:
        if FIREBASE_SERVICE_ACCOUNT_JSON:
            service_account = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
            cred = credentials.Certificate(service_account)
            firebase_admin.initialize_app(cred)
            return True
        if FIREBASE_CREDENTIALS_FILE:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS_FILE)
            firebase_admin.initialize_app(cred)
            return True
        firebase_admin.initialize_app()
        return True
    except Exception as exc:
        logger.warning("Firebase push initialization skipped: %s", exc)
        return False


def register_device_token(
    *,
    user_id: str,
    email: str,
    fcm_token: str,
    platform: str,
    app: str,
) -> dict[str, Any]:
    token = _normalize_text(fcm_token)
    if not token:
        return {"registered": False, "reason": "empty_token"}

    now_ms = _now_ms()
    normalized_email = _normalize_email(email)
    normalized_user_id = _normalize_text(user_id)
    normalized_platform = _normalize_text(platform).lower() or "unknown"
    normalized_app = _normalize_text(app).lower() or "main"

    notification_devices_collection.update_one(
        {"fcm_token": token},
        {
            "$set": {
                "fcm_token": token,
                "user_id": normalized_user_id,
                "email": normalized_email,
                "platform": normalized_platform,
                "app": normalized_app,
                "is_active": True,
                "updated_at": now_ms,
                "last_seen_at": now_ms,
            },
            "$setOnInsert": {"created_at": now_ms},
        },
        upsert=True,
    )
    return {"registered": True, "platform": normalized_platform, "app": normalized_app}


def dispatch_notification_push_async(notification: dict[str, Any]) -> None:
    if not isinstance(notification, dict) or not notification:
        return
    thread = threading.Thread(
        target=send_notification_push,
        kwargs={"notification": dict(notification)},
        daemon=True,
    )
    thread.start()


def send_notification_push(*, notification: dict[str, Any]) -> dict[str, Any]:
    if not _initialize_firebase():
        return {"sent": 0, "failed": 0, "skipped": True}

    tokens = _recipient_tokens(notification)
    if not tokens:
        return {"sent": 0, "failed": 0, "skipped": True}

    title = _normalize_text(notification.get("title")) or "Conscout"
    body = _normalize_text(notification.get("message")) or "You have a new notification."
    data = _message_data(notification)

    sent = 0
    failed = 0
    for token in tokens:
        try:
            message = messaging.Message(
                token=token,
                notification=messaging.Notification(title=title, body=body),
                data=data,
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(
                        channel_id=FCM_ANDROID_CHANNEL_ID or "conscout_alerts",
                        sound="default",
                    ),
                ),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(sound="default"),
                    ),
                ),
            )
            messaging.send(message)
            sent += 1
        except Exception as exc:
            failed += 1
            logger.warning("FCM send failed: %s", exc)
            if _is_invalid_token_error(exc):
                notification_devices_collection.update_one(
                    {"fcm_token": token},
                    {"$set": {"is_active": False, "updated_at": _now_ms()}},
                )

    return {"sent": sent, "failed": failed, "skipped": False}


def _recipient_tokens(notification: dict[str, Any]) -> list[str]:
    recipient_user_id = _normalize_text(notification.get("recipient_user_id"))
    recipient_email = _normalize_email(notification.get("recipient_email"))
    clauses: list[dict[str, Any]] = []
    if recipient_user_id:
        clauses.append({"user_id": recipient_user_id})
    if recipient_email:
        clauses.append({"email": recipient_email})
    if not clauses:
        return []

    query: dict[str, Any] = {"is_active": {"$ne": False}}
    query.update(clauses[0] if len(clauses) == 1 else {"$or": clauses})
    tokens = [
        _normalize_text(device.get("fcm_token"))
        for device in notification_devices_collection.find(query, {"fcm_token": 1})
    ]
    return sorted({token for token in tokens if token})


def _message_data(notification: dict[str, Any]) -> dict[str, str]:
    metadata = notification.get("metadata") if isinstance(notification.get("metadata"), dict) else {}
    return {
        "notification_id": _normalize_text(notification.get("_id")),
        "type": _normalize_text(notification.get("type")),
        "title": _normalize_text(notification.get("title")),
        "body": _normalize_text(notification.get("message")),
        "route": _normalize_text(notification.get("route")),
        "site_name": _normalize_text(notification.get("site_name")),
        "severity": _normalize_text(notification.get("severity")),
        "entity_id": _normalize_text(notification.get("entity_id")),
        "entity_type": _normalize_text(notification.get("entity_type")),
        "project_name": _normalize_text(metadata.get("project_name")),
    }


def _is_invalid_token_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "registration token is not a valid" in message
        or "requested entity was not found" in message
        or "not registered" in message
        or "invalid registration" in message
    )
