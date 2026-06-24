from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import uuid
from typing import Generator, Optional

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


DEFAULT_BOOTSTRAP_EMAIL = "saf@gmail.com"
DEFAULT_BOOTSTRAP_PASSWORD = "safwan123"
DEFAULT_ALLOWED_APP = "main"
SUPPORTED_APPS = {"main", "lite"}
_bearer_scheme = HTTPBearer(auto_error=False)


def _resolve_stakeholder_projects_for_email(email: str) -> list[str]:
    normalized_email = email.strip().lower()
    if not normalized_email:
        return []

    projects: set[str] = set()
    for floorplan in raw_floorplans_collection.find(
        {"stakeholder_emails": normalized_email},
        {"site_name": 1, "dxf_project_id": 1},
    ):
        site_name = (
            str(
                floorplan.get("site_name")
                or floorplan.get("dxf_project_id")
                or ""
            ).strip()
        )
        if site_name:
            projects.add(site_name)
    return sorted(projects)


def _resolve_owned_projects_for_user(user: dict) -> list[str]:
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
    if not owner_query["$or"]:
        return []

    projects: set[str] = set()
    for floorplan in raw_floorplans_collection.find(
        owner_query,
        {"site_name": 1, "dxf_project_id": 1},
    ):
        site_name = str(
            floorplan.get("site_name") or floorplan.get("dxf_project_id") or ""
        ).strip()
        if site_name:
            projects.add(site_name)
    return sorted(projects)


def _resolve_accessible_floorplan_ids(project_names: list[str]) -> list[str]:
    if not project_names:
        return []
    floorplan_ids: set[str] = set()
    for floorplan in raw_floorplans_collection.find(
        {
            "$or": [
                {"site_name": {"$in": project_names}},
                {"dxf_project_id": {"$in": project_names}},
            ]
        },
        {"id": 1},
    ):
        floorplan_id = str(floorplan.get("id") or "").strip()
        if floorplan_id:
            floorplan_ids.add(floorplan_id)
    return sorted(floorplan_ids)


def _build_user_access_payload(user: dict) -> dict:
    invited_projects = _resolve_stakeholder_projects_for_email(user.get("email", ""))
    owned_projects = _resolve_owned_projects_for_user(user)
    accessible_projects = sorted({*invited_projects, *owned_projects})
    stored_role = str(user.get("role") or "admin").strip().lower()
    role = stored_role if stored_role in {"admin", "stakeholder"} else "admin"
    return {
        "role": role,
        "accessible_project_names": accessible_projects,
        "accessible_floorplan_ids": _resolve_accessible_floorplan_ids(accessible_projects),
    }


def normalize_user_role(value: str | None) -> str:
    normalized = str(value or "admin").strip().lower()
    return normalized if normalized in {"admin", "stakeholder"} else "admin"


def normalize_allowed_apps(
    values: list[str] | tuple[str, ...] | set[str] | str | None,
) -> list[str]:
    candidates = values if isinstance(values, (list, tuple, set)) else [values]
    normalized = sorted(
        {
            str(item).strip().lower()
            for item in candidates
            if str(item or "").strip().lower() in SUPPORTED_APPS
        }
    )
    return normalized or [DEFAULT_ALLOWED_APP]


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


def bootstrap_default_user() -> dict:
    existing = raw_users_collection.find_one({"email": DEFAULT_BOOTSTRAP_EMAIL})
    if existing:
        raw_users_collection.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {
                    "name": "Safwan",
                    "password_hash": _hash_password(DEFAULT_BOOTSTRAP_PASSWORD),
                    "updated_at": int(__import__("time").time() * 1000),
                }
            },
        )
        return raw_users_collection.find_one({"email": DEFAULT_BOOTSTRAP_EMAIL}) or existing

    now = int(__import__("time").time() * 1000)
    doc = {
        "user_id": uuid.uuid4().hex,
        "email": DEFAULT_BOOTSTRAP_EMAIL,
        "name": "Safwan",
        "password_hash": _hash_password(DEFAULT_BOOTSTRAP_PASSWORD),
        "role": "admin",
        "allowed_apps": [DEFAULT_ALLOWED_APP],
        "session_token": "",
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
    allowed_apps: list[str] | None = None,
) -> dict:
    normalized_email = email.strip().lower()
    existing = raw_users_collection.find_one({"email": normalized_email})
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    now = int(__import__("time").time() * 1000)
    doc = {
        "user_id": uuid.uuid4().hex,
        "email": normalized_email,
        "name": name.strip(),
        "workspace": workspace.strip(),
        "password_hash": _hash_password(password),
        "role": normalize_user_role(role),
        "allowed_apps": normalize_allowed_apps(allowed_apps),
        "session_token": "",
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

    now = int(__import__("time").time() * 1000)
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


def start_user_session(user: dict, *, app_name: str | None = None) -> dict:
    token = issue_session_token()
    access_payload = _build_user_access_payload(user)
    allowed_apps = normalize_allowed_apps(user.get("allowed_apps"))
    update_fields = {
        "session_token": token,
        "updated_at": int(__import__("time").time() * 1000),
        "allowed_apps": allowed_apps,
    }
    if app_name:
        update_fields["last_login_app"] = app_name
    raw_users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": update_fields
        },
    )
    refreshed = raw_users_collection.find_one({"_id": user["_id"]}) or user
    refreshed["session_token"] = token
    return refreshed


def sanitize_user_payload(user: dict) -> dict:
    access_payload = _build_user_access_payload(user)
    subscription = user.get("subscription")
    return {
        "user_id": user.get("user_id", ""),
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        "workspace": user.get("workspace", ""),
        "role": normalize_user_role(user.get("role")),
        "allowed_apps": normalize_allowed_apps(user.get("allowed_apps")),
        "accessible_project_names": access_payload["accessible_project_names"],
        "subscription": subscription if isinstance(subscription, dict) else {},
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
