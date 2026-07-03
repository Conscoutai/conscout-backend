from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import time
import uuid
from typing import Generator, Optional, Union

from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.auth_context import (
    AuthenticatedUser,
    reset_current_user,
    set_current_user,
)
from core.config import site_dir, site_storage_roots, tour_storage_roots, user_data_dir
from core.database import (
    raw_floorplans_collection,
    raw_tours_collection,
    raw_users_collection,
    raw_work_schedules_collection,
)


DEFAULT_BOOTSTRAP_EMAIL = "safwanc189@gmail.com"
DEFAULT_BOOTSTRAP_PASSWORD = "1234567890"
SUBSCRIPTION_ADMIN_EMAIL = "safwanc189@gmail.com"
SUPPORTED_APPS = {"main", "lite"}
DEFAULT_ALLOWED_APPS = sorted(SUPPORTED_APPS)
_bearer_scheme = HTTPBearer(auto_error=False)
ACCESS_TOKEN_TTL_MS = 12 * 60 * 60 * 1000
REFRESH_TOKEN_TTL_MS = 30 * 24 * 60 * 60 * 1000
MAX_ACTIVE_SESSIONS = 12


def _now_ms() -> int:
    return int(time.time() * 1000)


def _resolve_accessible_floorplans_for_user(user: dict) -> list[dict]:
    user_id = str(user.get("user_id") or "").strip()
    email = str(user.get("email") or "").strip().lower()
    if not user_id and not email:
        return []

    owner_query = {"$or": []}
    if user_id:
        owner_query["$or"].append({"owner_user_id": user_id})
    if email:
        owner_query["$or"].append({"owner_email": email})
        owner_query["$or"].append({"created_by_email": email})
        owner_query["$or"].append({"stakeholder_emails": email})
    if not owner_query["$or"]:
        return []

    floorplans: list[dict] = []
    for floorplan in raw_floorplans_collection.find(
        owner_query,
        {"id": 1, "site_name": 1, "dxf_project_id": 1},
    ):
        floorplan_id = str(floorplan.get("id") or "").strip()
        site_name = str(
            floorplan.get("site_name") or floorplan.get("dxf_project_id") or ""
        ).strip()
        if not floorplan_id and not site_name:
            continue
        floorplans.append(
            {
                "id": floorplan_id,
                "site_name": site_name,
            }
        )
    return floorplans


def _build_user_access_payload(user: dict) -> dict:
    accessible_floorplans = _resolve_accessible_floorplans_for_user(user)
    accessible_projects = sorted(
        {
            floorplan["site_name"]
            for floorplan in accessible_floorplans
            if str(floorplan.get("site_name") or "").strip()
        }
    )
    accessible_floorplan_ids = sorted(
        {
            floorplan["id"]
            for floorplan in accessible_floorplans
            if str(floorplan.get("id") or "").strip()
        }
    )
    stored_role = str(user.get("role") or "admin").strip().lower()
    role = stored_role if stored_role in {"admin", "stakeholder"} else "admin"
    return {
        "role": role,
        "accessible_project_names": accessible_projects,
        "accessible_floorplan_ids": accessible_floorplan_ids,
    }


def normalize_user_role(value: Optional[str]) -> str:
    normalized = str(value or "admin").strip().lower()
    return normalized if normalized in {"admin", "stakeholder"} else "admin"


def normalize_allowed_apps(
    values: Optional[Union[list[str], tuple[str, ...], set[str], str]],
) -> list[str]:
    candidates = values if isinstance(values, (list, tuple, set)) else [values]
    normalized = {
        str(item).strip().lower()
        for item in candidates
        if str(item or "").strip().lower() in SUPPORTED_APPS
    }
    if normalized:
        # Main and lite now share the same account pool, so any valid app
        # assignment should unlock both surfaces for the same user.
        return DEFAULT_ALLOWED_APPS.copy()
    return DEFAULT_ALLOWED_APPS.copy()


def user_can_access_app(user: dict, app_name: str) -> bool:
    return app_name in normalize_allowed_apps(user.get("allowed_apps"))


def ensure_user_allowed_for_app(user: dict, app_name: str) -> None:
    if user_can_access_app(user, app_name):
        return
    raise HTTPException(
        status_code=403,
        detail=f"This account is not allowed to access the {app_name} app.",
    )


def ensure_admin_user(user: AuthenticatedUser) -> None:
    if user.role == "stakeholder":
        raise HTTPException(
            status_code=403,
            detail="Stakeholders have read and comment access only.",
        )


def ensure_subscription_admin_user(user: AuthenticatedUser) -> None:
    ensure_admin_user(user)
    normalized_email = str(user.email or "").strip().lower()
    if normalized_email != SUBSCRIPTION_ADMIN_EMAIL:
        raise HTTPException(
            status_code=403,
            detail="This page is restricted to the designated subscription admin account.",
        )


def _hash_password(password: str, *, salt: Optional[str] = None) -> str:
    resolved_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        resolved_salt.encode("utf-8"),
        120000,
    ).hex()
    return f"{resolved_salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash or "$" not in stored_hash:
        return False
    salt, expected = stored_hash.split("$", 1)
    return secrets.compare_digest(_hash_password(password, salt=salt), stored_hash)


def issue_session_token() -> str:
    return secrets.token_urlsafe(32)


def issue_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def _normalize_auth_sessions(user: dict) -> list[dict]:
    raw_sessions = user.get("auth_sessions")
    if not isinstance(raw_sessions, list):
        return []

    normalized: list[dict] = []
    for item in raw_sessions:
        if not isinstance(item, dict):
            continue
        access_token = str(item.get("access_token") or "").strip()
        refresh_token = str(item.get("refresh_token") or "").strip()
        session_id = str(item.get("session_id") or "").strip()
        if not access_token or not refresh_token or not session_id:
            continue
        normalized.append(
            {
                "session_id": session_id,
                "app_name": str(item.get("app_name") or "").strip().lower(),
                "access_token": access_token,
                "refresh_token": refresh_token,
                "access_expires_at": int(item.get("access_expires_at") or 0),
                "refresh_expires_at": int(item.get("refresh_expires_at") or 0),
                "created_at": int(item.get("created_at") or 0),
                "updated_at": int(item.get("updated_at") or 0),
                "last_used_at": int(item.get("last_used_at") or 0),
            }
        )
    return normalized


def _prune_auth_sessions(sessions: list[dict], *, now_ms: Optional[int] = None) -> list[dict]:
    current_ms = now_ms if now_ms is not None else _now_ms()
    active = [
        session
        for session in sessions
        if int(session.get("refresh_expires_at") or 0) > current_ms
    ]
    active.sort(key=lambda session: int(session.get("updated_at") or 0), reverse=True)
    return active[:MAX_ACTIVE_SESSIONS]


def _build_auth_session(*, app_name: Optional[str] = None) -> dict:
    current_ms = _now_ms()
    return {
        "session_id": uuid.uuid4().hex,
        "app_name": (app_name or "").strip().lower(),
        "access_token": issue_session_token(),
        "refresh_token": issue_refresh_token(),
        "access_expires_at": current_ms + ACCESS_TOKEN_TTL_MS,
        "refresh_expires_at": current_ms + REFRESH_TOKEN_TTL_MS,
        "created_at": current_ms,
        "updated_at": current_ms,
        "last_used_at": current_ms,
    }


def _save_auth_sessions(
    user: dict,
    sessions: list[dict],
    *,
    app_name: Optional[str] = None,
    record_login: bool = False,
    login_at: Optional[int] = None,
) -> dict:
    pruned_sessions = _prune_auth_sessions(sessions)
    update_fields = {
        "auth_sessions": pruned_sessions,
        "session_token": pruned_sessions[0]["access_token"] if pruned_sessions else "",
        "updated_at": _now_ms(),
        "allowed_apps": normalize_allowed_apps(user.get("allowed_apps")),
    }
    if record_login:
        resolved_login_at = login_at if login_at is not None else _now_ms()
        update_fields["last_login_at"] = resolved_login_at
    if app_name and record_login:
        update_fields["last_login_app"] = app_name
    raw_users_collection.update_one({"_id": user["_id"]}, {"$set": update_fields})
    return raw_users_collection.find_one({"_id": user["_id"]}) or user


def _find_session_by_access_token(user: dict, token: str) -> Optional[dict]:
    for session in _normalize_auth_sessions(user):
        if session.get("access_token") == token:
            return session
    return None


def _find_session_by_refresh_token(user: dict, token: str) -> Optional[dict]:
    for session in _normalize_auth_sessions(user):
        if session.get("refresh_token") == token:
            return session
    return None


def bootstrap_default_user() -> dict:
    existing = raw_users_collection.find_one({"email": DEFAULT_BOOTSTRAP_EMAIL})
    if existing:
        raw_users_collection.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {
                    "name": "Conscout Admin",
                    "password_hash": _hash_password(DEFAULT_BOOTSTRAP_PASSWORD),
                    "role": "admin",
                    "allowed_apps": DEFAULT_ALLOWED_APPS.copy(),
                    "updated_at": _now_ms(),
                }
            },
        )
        return raw_users_collection.find_one({"email": DEFAULT_BOOTSTRAP_EMAIL}) or existing

    now = _now_ms()
    doc = {
        "user_id": uuid.uuid4().hex,
        "email": DEFAULT_BOOTSTRAP_EMAIL,
        "name": "Conscout Admin",
        "password_hash": _hash_password(DEFAULT_BOOTSTRAP_PASSWORD),
        "role": "admin",
        "allowed_apps": DEFAULT_ALLOWED_APPS.copy(),
        "session_token": "",
        "auth_sessions": [],
        "last_login_at": None,
        "created_at": now,
        "updated_at": now,
    }
    raw_users_collection.insert_one(doc)
    return raw_users_collection.find_one({"email": DEFAULT_BOOTSTRAP_EMAIL}) or doc


def migrate_legacy_data_to_default_user(default_user: dict) -> None:
    owner_patch = {
        "owner_user_id": default_user["user_id"],
        "owner_email": default_user["email"],
    }
    for collection in (
        raw_floorplans_collection,
        raw_tours_collection,
        raw_work_schedules_collection,
    ):
        collection.update_many(
            {
                "$or": [
                    {"owner_user_id": {"$exists": False}},
                    {"owner_user_id": None},
                    {"owner_user_id": ""},
                ]
            },
            {"$set": owner_patch},
        )


def _merge_directory_contents(source_dir: str, target_dir: str) -> None:
    if not os.path.isdir(source_dir):
        return

    os.makedirs(target_dir, exist_ok=True)
    for name in os.listdir(source_dir):
        src = os.path.join(source_dir, name)
        dst = os.path.join(target_dir, name)
        if os.path.isdir(src):
            _merge_directory_contents(src, dst)
            try:
                if not os.listdir(src):
                    os.rmdir(src)
            except OSError:
                pass
            continue

        if os.path.exists(dst):
            continue
        shutil.move(src, dst)

    try:
        if not os.listdir(source_dir):
            os.rmdir(source_dir)
    except OSError:
        pass


def migrate_legacy_files_to_user_folders() -> None:
    for user in raw_users_collection.find({}, {"user_id": 1, "email": 1}):
        os.makedirs(
            user_data_dir(
                owner_email=user.get("email"),
                owner_user_id=user.get("user_id"),
            ),
            exist_ok=True,
        )

    for floorplan in raw_floorplans_collection.find({}):
        site_name = floorplan.get("site_name") or floorplan.get("dxf_project_id")
        if not site_name:
            continue

        roots = site_storage_roots(
            owner_email=floorplan.get("owner_email"),
            owner_user_id=floorplan.get("owner_user_id"),
        )
        if len(roots) < 2:
            continue

        scoped_root = roots[0]
        legacy_root = roots[1]
        source_dir = os.path.join(legacy_root, site_name)
        target_dir = site_dir(
            site_name,
            owner_email=floorplan.get("owner_email"),
            owner_user_id=floorplan.get("owner_user_id"),
        )
        if os.path.abspath(source_dir) == os.path.abspath(target_dir):
            continue
        _merge_directory_contents(source_dir, target_dir)

    for tour in raw_tours_collection.find({}):
        storage_key = (tour.get("storage_key") or tour.get("tour_id") or "").strip()
        if not storage_key:
            continue

        roots = tour_storage_roots(
            owner_email=tour.get("owner_email"),
            owner_user_id=tour.get("owner_user_id"),
        )
        if len(roots) < 2:
            continue

        scoped_root = roots[0]
        legacy_root = roots[1]
        source_dir = os.path.join(legacy_root, storage_key)
        target_dir = os.path.join(scoped_root, storage_key)
        if os.path.abspath(source_dir) == os.path.abspath(target_dir):
            continue
        _merge_directory_contents(source_dir, target_dir)


def ensure_default_user_and_migrate_legacy_data() -> None:
    default_user = bootstrap_default_user()
    migrate_legacy_data_to_default_user(default_user)
    migrate_legacy_files_to_user_folders()


def authenticate_user(email: str, password: str) -> Optional[dict]:
    user = raw_users_collection.find_one({"email": email.strip().lower()})
    if not user:
        return None
    if not verify_password(password, user.get("password_hash", "")):
        return None
    return user


def create_user(
    *,
    name: str,
    email: str,
    password: str,
    workspace: str = "",
    role: str = "admin",
    allowed_apps: Optional[list[str]] = None,
) -> dict:
    normalized_email = email.strip().lower()
    existing = raw_users_collection.find_one({"email": normalized_email})
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    now = _now_ms()
    doc = {
        "user_id": uuid.uuid4().hex,
        "email": normalized_email,
        "name": name.strip(),
        "workspace": workspace.strip(),
        "password_hash": _hash_password(password),
        "role": normalize_user_role(role),
        "allowed_apps": normalize_allowed_apps(allowed_apps),
        "session_token": "",
        "auth_sessions": [],
        "last_login_at": None,
        "created_at": now,
        "updated_at": now,
    }
    raw_users_collection.insert_one(doc)
    return raw_users_collection.find_one({"email": normalized_email}) or doc


def change_user_password(*, user_id: str, current_password: str, new_password: str) -> dict:
    user = raw_users_collection.find_one({"user_id": user_id.strip()})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if not verify_password(current_password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")

    now = _now_ms()
    new_hash = _hash_password(new_password)
    raw_users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password_hash": new_hash,
                "updated_at": now,
            }
        },
    )
    return raw_users_collection.find_one({"_id": user["_id"]}) or user


def update_user_profile(
    *,
    user_id: str,
    name: str,
    workspace: Optional[str] = None,
) -> dict:
    normalized_user_id = user_id.strip()
    if not normalized_user_id:
        raise HTTPException(status_code=400, detail="User ID is required.")

    resolved_name = name.strip()
    if not resolved_name:
        raise HTTPException(status_code=400, detail="Name is required.")

    user = raw_users_collection.find_one({"user_id": normalized_user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    update_fields = {
        "name": resolved_name,
        "updated_at": _now_ms(),
    }
    if workspace is not None:
        update_fields["workspace"] = workspace.strip()

    raw_users_collection.update_one(
        {"_id": user["_id"]},
        {"$set": update_fields},
    )
    return raw_users_collection.find_one({"_id": user["_id"]}) or user


def reset_user_password_by_email(*, email: str, new_password: str) -> dict:
    normalized_email = email.strip().lower()
    if not normalized_email:
        raise HTTPException(status_code=400, detail="Email is required.")
    user = raw_users_collection.find_one({"email": normalized_email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if str(user.get("auth_provider") or "").strip().lower() == "google":
        raise HTTPException(
            status_code=400,
            detail="This account uses Google Sign-In. Continue with Google instead.",
        )
    if len(new_password.strip()) < 8:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters.",
        )

    now = _now_ms()
    new_hash = _hash_password(new_password.strip())
    raw_users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password_hash": new_hash,
                "session_token": "",
                "auth_sessions": [],
                "updated_at": now,
            }
        },
    )
    return raw_users_collection.find_one({"_id": user["_id"]}) or user


def start_user_session(user: dict, *, app_name: Optional[str] = None) -> dict:
    sessions = _normalize_auth_sessions(user)
    new_session = _build_auth_session(app_name=app_name)
    sessions.insert(0, new_session)
    refreshed = _save_auth_sessions(
        user,
        sessions,
        app_name=app_name,
        record_login=True,
        login_at=new_session["created_at"],
    )
    latest_session = _normalize_auth_sessions(refreshed)[0]
    refreshed["session_token"] = latest_session["access_token"]
    refreshed["refresh_token"] = latest_session["refresh_token"]
    refreshed["session_expires_at"] = latest_session["access_expires_at"]
    refreshed["refresh_expires_at"] = latest_session["refresh_expires_at"]
    return refreshed


def refresh_user_session(refresh_token: str, *, app_name: Optional[str] = None) -> dict:
    normalized_refresh_token = refresh_token.strip()
    if not normalized_refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token is required.")

    user = raw_users_collection.find_one({"auth_sessions.refresh_token": normalized_refresh_token})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    sessions = _normalize_auth_sessions(user)
    matching_session = _find_session_by_refresh_token(user, normalized_refresh_token)
    if not matching_session:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    current_ms = _now_ms()
    if int(matching_session.get("refresh_expires_at") or 0) <= current_ms:
        remaining_sessions = [
            session
            for session in sessions
            if session.get("session_id") != matching_session.get("session_id")
        ]
        _save_auth_sessions(user, remaining_sessions)
        raise HTTPException(status_code=401, detail="Refresh token expired.")

    rotated_session = {
        **matching_session,
        "app_name": (app_name or matching_session.get("app_name") or "").strip().lower(),
        "access_token": issue_session_token(),
        "refresh_token": issue_refresh_token(),
        "access_expires_at": current_ms + ACCESS_TOKEN_TTL_MS,
        "refresh_expires_at": current_ms + REFRESH_TOKEN_TTL_MS,
        "updated_at": current_ms,
        "last_used_at": current_ms,
    }
    updated_sessions = [
        rotated_session if session.get("session_id") == rotated_session["session_id"] else session
        for session in sessions
    ]
    refreshed = _save_auth_sessions(user, updated_sessions, app_name=app_name)
    refreshed["session_token"] = rotated_session["access_token"]
    refreshed["refresh_token"] = rotated_session["refresh_token"]
    refreshed["session_expires_at"] = rotated_session["access_expires_at"]
    refreshed["refresh_expires_at"] = rotated_session["refresh_expires_at"]
    return refreshed


def revoke_user_session(
    *,
    access_token: Optional[str] = None,
    refresh_token: Optional[str] = None,
) -> None:
    normalized_access = (access_token or "").strip()
    normalized_refresh = (refresh_token or "").strip()
    if not normalized_access and not normalized_refresh:
        return

    user = None
    if normalized_access:
        user = raw_users_collection.find_one({"auth_sessions.access_token": normalized_access})
        if not user:
            user = raw_users_collection.find_one({"session_token": normalized_access})
    if user is None and normalized_refresh:
        user = raw_users_collection.find_one({"auth_sessions.refresh_token": normalized_refresh})
    if not user:
        return

    sessions = [
        session
        for session in _normalize_auth_sessions(user)
        if session.get("access_token") != normalized_access
        and session.get("refresh_token") != normalized_refresh
    ]
    _save_auth_sessions(user, sessions)


def sanitize_user_payload(user: dict) -> dict:
    access_payload = _build_user_access_payload(user)
    subscription = user.get("subscription")
    pending_subscription_request = user.get("pending_subscription_request")
    active_plan_code = ""
    if isinstance(subscription, dict):
        active_plan_code = str(subscription.get("plan_code") or "").strip().lower()
    return {
        "user_id": user.get("user_id", ""),
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        "workspace": user.get("workspace", ""),
        "role": normalize_user_role(user.get("role")),
        "allowed_apps": normalize_allowed_apps(user.get("allowed_apps")),
        "accessible_project_names": access_payload["accessible_project_names"],
        "plan_code": active_plan_code,
        "subscription": subscription if isinstance(subscription, dict) else {},
        "pending_subscription_request": pending_subscription_request
        if isinstance(pending_subscription_request, dict)
        else {},
        "last_login_at": user.get("last_login_at"),
        "last_login_app": user.get("last_login_app", ""),
        "last_login_platform": user.get("last_login_platform", ""),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
    }


async def require_authenticated_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    access_token: Optional[str] = Query(default=None),
) -> Generator[AuthenticatedUser, None, None]:
    token = credentials.credentials.strip() if credentials else ""
    if not token and access_token is not None:
        token = access_token.strip()
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required.")

    user = raw_users_collection.find_one({"auth_sessions.access_token": token})
    if user:
        session = _find_session_by_access_token(user, token)
        if session:
            current_ms = _now_ms()
            if int(session.get("access_expires_at") or 0) <= current_ms:
                raise HTTPException(status_code=401, detail="Access token expired.")

            access_payload = _build_user_access_payload(user)
            auth_user = AuthenticatedUser(
                user_id=user.get("user_id", ""),
                email=user.get("email", ""),
                name=user.get("name", ""),
                role=access_payload["role"],
                accessible_project_names=tuple(access_payload["accessible_project_names"]),
                accessible_floorplan_ids=tuple(access_payload["accessible_floorplan_ids"]),
            )
            context_token = set_current_user(auth_user)
            try:
                yield auth_user
            finally:
                reset_current_user(context_token)
            return

    user = raw_users_collection.find_one({"session_token": token})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session token.")

    access_payload = _build_user_access_payload(user)
    auth_user = AuthenticatedUser(
        user_id=user.get("user_id", ""),
        email=user.get("email", ""),
        name=user.get("name", ""),
        role=access_payload["role"],
        accessible_project_names=tuple(access_payload["accessible_project_names"]),
        accessible_floorplan_ids=tuple(access_payload["accessible_floorplan_ids"]),
    )
    context_token = set_current_user(auth_user)
    try:
        yield auth_user
    finally:
        reset_current_user(context_token)
