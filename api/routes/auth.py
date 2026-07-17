from __future__ import annotations

import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core.auth import (
    ACCOUNT_ROLE_ADMIN,
    ACCOUNT_ROLE_LITE_USER,
    ACCOUNT_ROLE_MAIN_USER,
    _hash_password,
    account_role_for_user,
    authenticate_user,
    change_user_password,
    create_user,
    ensure_account_admin_access,
    ensure_super_admin_user,
    ensure_user_allowed_for_app,
    ensure_subscription_admin_user,
    normalize_user_role,
    refresh_user_session,
    require_authenticated_user,
    reset_user_password_by_email,
    revoke_user_session,
    sanitize_user_payload,
    start_user_session,
    update_user_profile,
)
from core.auth_context import AuthenticatedUser
from core.config import (
    APP_SURFACE,
    DB_NAME,
    GOOGLE_OAUTH_CLIENT_IDS,
    GOOGLE_OAUTH_HOSTED_DOMAIN,
    LITE_ADMIN_DB_NAME,
)
from core.database import client, raw_users_collection
from services.account_deletion_service import delete_user_account


router = APIRouter(prefix="/auth", tags=["Auth"])
_SUPPORTED_APPS = {"main", "lite"}
_bearer_scheme = HTTPBearer(auto_error=False)


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _normalize_app(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in _SUPPORTED_APPS:
        raise HTTPException(status_code=400, detail="Invalid app identifier.")
    if normalized != APP_SURFACE:
        raise HTTPException(
            status_code=403,
            detail=f"This API serves the {APP_SURFACE} product only.",
        )
    return normalized


def _admin_directory_collection(app_name: str):
    normalized = app_name.strip().lower()
    if normalized not in _SUPPORTED_APPS:
        raise HTTPException(status_code=400, detail="Invalid app identifier.")
    if APP_SURFACE != "main":
        raise HTTPException(
            status_code=403,
            detail="The account directory is available through the main admin API only.",
        )
    database_name = DB_NAME if normalized == "main" else LITE_ADMIN_DB_NAME
    return client[database_name]["users"], normalized


def _admin_product_collections(app_name: str):
    users, normalized = _admin_directory_collection(app_name)
    database = users.database
    return users, database["subscription_requests"], normalized


def _admin_request_payload(request: dict) -> dict:
    payload = dict(request)
    payload.pop("_id", None)
    return payload


def _admin_request_status_filter(status: str) -> dict:
    normalized = status.strip().lower()
    if normalized in {"pending", "pending_approval"}:
        return {"status": "pending_approval"}
    if normalized in {"approved", "rejected"}:
        return {"status": normalized}
    if normalized in {"", "all"}:
        return {}
    raise HTTPException(status_code=400, detail="Invalid request status filter.")


def _admin_project_registry_payload(project: dict, owner: Optional[dict]) -> dict:
    """Return the minimal, serializable project record needed by Admin."""
    owner = owner or {}
    owner_id = str(
        project.get("owner_user_id")
        or project.get("owner_id")
        or project.get("created_by_id")
        or project.get("user_id")
        or ""
    ).strip()
    owner_email = _normalize_email(
        str(
            project.get("owner_email")
            or project.get("created_by_email")
            or project.get("user_email")
            or ""
        )
    )
    owner_name = str(
        project.get("owner_name")
        or project.get("created_by_name")
        or project.get("created_by")
        or owner.get("name")
        or ""
    ).strip()
    return {
        "id": str(project.get("_id") or project.get("id") or ""),
        "site_name": str(
            project.get("site_name")
            or project.get("project_name")
            or project.get("name")
            or project.get("dxf_project_id")
            or ""
        ).strip(),
        "location": str(
            project.get("location")
            or project.get("project_location")
            or project.get("area_location")
            or ""
        ).strip(),
        "owner_id": owner_id or str(owner.get("user_id") or "").strip(),
        "owner_email": owner_email or _normalize_email(str(owner.get("email") or "")),
        "owner_name": owner_name,
        "created_at": project.get("created_at") or project.get("createdAt"),
        "updated_at": project.get("updated_at") or project.get("updatedAt"),
    }


def _admin_directory_user_payload(user: dict, *, app_name: str) -> dict:
    subscription = user.get("subscription")
    subscription = subscription if isinstance(subscription, dict) else {}
    pending_request = user.get("pending_subscription_request")
    pending_request = pending_request if isinstance(pending_request, dict) else {}
    plan_name = str(subscription.get("plan_name") or "").strip()
    if not plan_name:
        plan_name = str(pending_request.get("plan_name") or "Starter Access").strip()
    subscription_status = str(subscription.get("status") or "").strip().lower()
    if not subscription_status:
        subscription_status = "pending_approval" if pending_request else "active"
    plan_joined_at = (
        subscription.get("activated_at")
        or subscription.get("approved_at")
        or user.get("created_at")
    )
    account_role = account_role_for_user(user)
    return {
        "user_id": str(user.get("user_id") or ""),
        "email": str(user.get("email") or "").strip().lower(),
        "name": str(user.get("name") or "").strip(),
        "workspace": str(user.get("workspace") or "").strip(),
        "role": normalize_user_role(user.get("role")),
        "account_role": account_role,
        "account_status": str(user.get("account_status") or "active").strip().lower()
        or "active",
        "is_subscription_admin": account_role in {"admin", "super_admin"},
        "app": app_name,
        "plan_name": plan_name or "Starter Access",
        "subscription_status": subscription_status,
        "plan_joined_at": plan_joined_at,
        "last_login_at": user.get("last_login_at"),
        "last_login_app": str(user.get("last_login_app") or "").strip(),
        "created_at": user.get("created_at"),
    }


def _delete_account_page(*, message: str = "", is_error: bool = False) -> str:
    tone = "#b91c1c" if is_error else "#0f766e"
    bg = "#fef2f2" if is_error else "#ecfeff"
    border = "#fecaca" if is_error else "#a5f3fc"
    panel = ""
    if message:
        panel = (
            f'<div style="margin-bottom:16px;padding:12px 14px;border-radius:12px;'
            f'background:{bg};border:1px solid {border};color:{tone};font-weight:600;">{message}</div>'
        )

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Delete Conscout Account</title>
</head>
<body style=\"margin:0;background:#f8fafc;font-family:Arial,sans-serif;color:#0f172a;\">
  <div style=\"max-width:520px;margin:48px auto;padding:24px;\">
    <div style=\"background:#ffffff;border:1px solid #e2e8f0;border-radius:18px;padding:24px;box-shadow:0 18px 40px rgba(15,23,42,0.08);\">
      <h1 style=\"margin:0 0 12px;font-size:28px;\">Delete Account</h1>
      <p style=\"margin:0 0 18px;line-height:1.6;color:#475569;\">Use this page to permanently delete your Conscout Lite account and its related data.</p>
      <p style=\"margin:0 0 18px;line-height:1.6;color:#475569;\">This removes your account record, owned projects, tours, work schedules, inspections, notifications, stakeholder access references, and server-side user files.</p>
      {panel}
      <form method=\"post\" action=\"/auth/delete-account\">
        <label for=\"email\" style=\"display:block;margin-bottom:8px;font-weight:700;\">Email</label>
        <input id=\"email\" name=\"email\" type=\"email\" required style=\"width:100%;box-sizing:border-box;padding:12px 14px;margin-bottom:16px;border:1px solid #cbd5e1;border-radius:12px;\" />
        <label for=\"password\" style=\"display:block;margin-bottom:8px;font-weight:700;\">Password</label>
        <input id=\"password\" name=\"password\" type=\"password\" required style=\"width:100%;box-sizing:border-box;padding:12px 14px;margin-bottom:20px;border:1px solid #cbd5e1;border-radius:12px;\" />
        <button type=\"submit\" style=\"width:100%;padding:13px 16px;border:none;border-radius:12px;background:#dc2626;color:#fff;font-weight:700;cursor:pointer;\">Delete Account Permanently</button>
      </form>
    </div>
  </div>
</body>
</html>"""


class LoginRequest(BaseModel):
    email: str
    password: str
    app: str
    admin_access: Optional[str] = None


class SignupRequest(BaseModel):
    name: str
    email: str
    password: str
    workspace: str = ""
    app: str
    role: Optional[str] = None


class CreateAdminRequest(BaseModel):
    name: str
    email: str
    password: str


class CreateManagedUserRequest(BaseModel):
    name: str
    email: str
    password: str
    app: str
    workspace: str = ""


class DeleteManagedUserRequest(BaseModel):
    confirmation_email: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class UpdateProfileRequest(BaseModel):
    name: str
    workspace: Optional[str] = None


class ForgotPasswordRequest(BaseModel):
    email: str
    new_password: str


class RefreshSessionRequest(BaseModel):
    refresh_token: str
    app: Optional[str] = None


class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = None


class GoogleAuthRequest(BaseModel):
    id_token: str
    provider: str = "google"
    intent: str = "login"
    app: str
    email: Optional[str] = None
    name: Optional[str] = None
    google_user_id: Optional[str] = None
    photo_url: Optional[str] = None
    workspace: Optional[str] = None
    platform: Optional[str] = None


def _verify_google_id_token(id_token: str) -> dict:
    token = id_token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Google ID token is required.")

    try:
        response = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": token},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to verify Google sign-in right now.",
        ) from exc

    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Google ID token.")

    payload = response.json()
    issuer = str(payload.get("iss") or "").strip()
    if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
        raise HTTPException(status_code=401, detail="Invalid Google token issuer.")

    audience = str(payload.get("aud") or "").strip()
    if GOOGLE_OAUTH_CLIENT_IDS and audience not in GOOGLE_OAUTH_CLIENT_IDS:
        raise HTTPException(status_code=401, detail="Google token audience mismatch.")

    email_verified = str(payload.get("email_verified") or "").strip().lower()
    if email_verified != "true":
        raise HTTPException(status_code=403, detail="Google email is not verified.")

    hosted_domain = GOOGLE_OAUTH_HOSTED_DOMAIN
    if hosted_domain:
        token_domain = str(payload.get("hd") or "").strip().lower()
        if token_domain != hosted_domain:
            raise HTTPException(
                status_code=403,
                detail="This Google account is not allowed for this workspace.",
            )

    return payload


def _finalize_auth_response(user: dict, *, app_name: str) -> dict:
    ensure_user_allowed_for_app(user, app_name)
    user = start_user_session(user, app_name=app_name)
    return {
        "token": user.get("session_token", ""),
        "refresh_token": user.get("refresh_token", ""),
        "token_expires_at": user.get("session_expires_at"),
        "refresh_token_expires_at": user.get("refresh_expires_at"),
        "user": sanitize_user_payload(user),
    }


@router.post("/login")
def login(payload: LoginRequest):
    app_name = _normalize_app(payload.app)
    user = authenticate_user(payload.email, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    if payload.admin_access is not None:
        if app_name != "main":
            raise HTTPException(status_code=400, detail="Admin Console access uses the main API.")
        ensure_account_admin_access(user, required_role=payload.admin_access)
    return _finalize_auth_response(user, app_name=app_name)


@router.post("/signup")
def signup(payload: SignupRequest):
    app_name = _normalize_app(payload.app)
    if len(payload.password.strip()) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")

    user = create_user(
        name=payload.name,
        email=payload.email,
        password=payload.password,
        workspace=payload.workspace,
        role=normalize_user_role(payload.role),
        allowed_apps=[app_name],
    )
    return _finalize_auth_response(user, app_name=app_name)


@router.post("/google")
def google_auth(payload: GoogleAuthRequest):
    if payload.provider.strip().lower() != "google":
        raise HTTPException(status_code=400, detail="Unsupported auth provider.")

    app_name = _normalize_app(payload.app)
    intent = payload.intent.strip().lower()
    if intent not in {"login", "signup"}:
        raise HTTPException(status_code=400, detail="Invalid Google sign-in intent.")

    token_payload = _verify_google_id_token(payload.id_token)

    email = _normalize_email(
        str(token_payload.get("email") or payload.email or "")
    )
    if not email:
        raise HTTPException(status_code=400, detail="Google account email is missing.")

    display_name = str(
        payload.name
        or token_payload.get("name")
        or email.split("@", 1)[0]
    ).strip()
    workspace = str(payload.workspace or "").strip()
    google_user_id = str(
        token_payload.get("sub") or payload.google_user_id or ""
    ).strip()
    if not google_user_id:
        raise HTTPException(
            status_code=400,
            detail="Google account identifier is missing.",
        )
    photo_url = str(
        payload.photo_url
        or token_payload.get("picture")
        or ""
    ).strip()

    user = raw_users_collection.find_one(
        {
            "$or": [
                {"google_user_id": google_user_id},
                {"email": email},
            ]
        }
    )
    if intent == "login" and not user:
        raise HTTPException(
            status_code=404,
            detail=(
                "We couldn't find a Conscout account for this Google account. "
                "Create an account to get started."
            ),
        )
    if intent == "signup" and user:
        raise HTTPException(
            status_code=409,
            detail=(
                "A Conscout account already exists for this Google account. "
                "Sign in to continue."
            ),
        )

    is_new_google_account = user is None
    if is_new_google_account:
        user = create_user(
            name=display_name or "Google User",
            email=email,
            password=secrets.token_urlsafe(32),
            workspace=workspace,
            role="admin",
            allowed_apps=[app_name],
        )

    update_fields = {
        "updated_at": int(time.time() * 1000),
        "google_user_id": google_user_id,
        "google_photo_url": photo_url,
        "google_email_verified": True,
    }
    if is_new_google_account:
        update_fields.update(
            {
                "name": display_name or "Google User",
                "workspace": workspace,
                "auth_provider": "google",
            }
        )
    if payload.platform:
        update_fields["last_login_platform"] = payload.platform.strip().lower()
    update_fields["last_google_intent"] = intent
    raw_users_collection.update_one(
        {"_id": user["_id"]},
        {"$set": update_fields},
    )
    user = raw_users_collection.find_one({"_id": user["_id"]}) or user
    return _finalize_auth_response(user, app_name=app_name)


@router.post("/refresh")
def refresh_session(payload: RefreshSessionRequest):
    app_name = _normalize_app(payload.app or APP_SURFACE)
    user = refresh_user_session(payload.refresh_token, app_name=app_name)
    return {
        "token": user.get("session_token", ""),
        "refresh_token": user.get("refresh_token", ""),
        "token_expires_at": user.get("session_expires_at"),
        "refresh_token_expires_at": user.get("refresh_expires_at"),
        "user": sanitize_user_payload(user),
    }


@router.get("/me")
def me(current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    user = raw_users_collection.find_one({"user_id": current_user.user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return {"user": sanitize_user_payload(user)}


@router.put("/profile")
def update_profile(
    payload: UpdateProfileRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    user = update_user_profile(
        user_id=current_user.user_id,
        name=payload.name,
        workspace=payload.workspace,
    )
    return {
        "message": "Profile updated successfully.",
        "user": sanitize_user_payload(user),
    }


@router.get("/users/exists")
def user_exists(
    email: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    normalized_email = _normalize_email(email)
    if not normalized_email:
        raise HTTPException(status_code=400, detail="Email is required.")

    user = raw_users_collection.find_one(
        {"email": normalized_email},
        {"_id": 0, "email": 1, "name": 1, "role": 1},
    )
    return {
        "exists": user is not None,
        "user": sanitize_user_payload(user) if user else None,
    }


@router.post("/admins")
def create_subscription_admin(
    payload: CreateAdminRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    """Create an additional administrator for the web subscription console."""
    ensure_super_admin_user(current_user)
    if APP_SURFACE != "main":
        raise HTTPException(
            status_code=403,
            detail="Administrators can be created through the main API only.",
        )
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Admin name is required.")
    if len(payload.password.strip()) < 8:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters.",
        )

    admin = create_user(
        name=payload.name,
        email=payload.email,
        password=payload.password,
        role="admin",
        account_role=ACCOUNT_ROLE_ADMIN,
        allowed_apps=["main"],
    )
    raw_users_collection.update_one(
        {"_id": admin["_id"]},
        {
            "$set": {
                "is_subscription_admin": True,
                "account_role": ACCOUNT_ROLE_ADMIN,
                "updated_at": int(time.time() * 1000),
            }
        },
    )
    admin = raw_users_collection.find_one({"_id": admin["_id"]}) or admin
    return {
        "message": "Administrator created successfully.",
        "admin": sanitize_user_payload(admin),
    }


@router.get("/admin/access")
def admin_access(
    required_role: str = Query(default="admin"),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    """Verify the signed-in account's Admin Console access level."""
    account_role = ensure_subscription_admin_user(
        current_user,
        required_role=required_role,
    )
    return {
        "account_role": account_role,
        "is_super_admin": account_role == "super_admin",
    }


def _managed_customer_or_404(collection, user_id: str) -> dict:
    user = collection.find_one({"user_id": user_id.strip()})
    if not user:
        raise HTTPException(status_code=404, detail="User account was not found.")
    if account_role_for_user(user) in {"admin", "super_admin"}:
        raise HTTPException(
            status_code=403,
            detail="Admin Console accounts must be managed separately.",
        )
    return user


@router.post("/admin/users")
def create_managed_user(
    payload: CreateManagedUserRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    """Create a regular Main or Lite account from the Admin Console."""
    ensure_subscription_admin_user(current_user)
    app_name = payload.app.strip().lower()
    collection, app_name = _admin_directory_collection(app_name)
    name = payload.name.strip()
    email = _normalize_email(payload.email)
    password = payload.password.strip()
    if not name:
        raise HTTPException(status_code=400, detail="User name is required.")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if collection.find_one({"email": email}, {"_id": 1}):
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    now = int(time.time() * 1000)
    user = {
        "user_id": uuid.uuid4().hex,
        "email": email,
        "name": name,
        "workspace": payload.workspace.strip(),
        "password_hash": _hash_password(password),
        "role": "admin",
        "account_role": (
            ACCOUNT_ROLE_LITE_USER if app_name == "lite" else ACCOUNT_ROLE_MAIN_USER
        ),
        "account_status": "active",
        "allowed_apps": [app_name],
        "session_token": "",
        "auth_sessions": [],
        "last_login_at": None,
        "created_at": now,
        "updated_at": now,
    }
    collection.insert_one(user)
    return {
        "message": f"{app_name.title()} user created successfully.",
        "user": _admin_directory_user_payload(user, app_name=app_name),
    }


@router.post("/admin/users/{app}/{user_id}/deactivate")
def deactivate_managed_user(
    app: str,
    user_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_subscription_admin_user(current_user)
    collection, _ = _admin_directory_collection(app)
    user = _managed_customer_or_404(collection, user_id)
    collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "account_status": "deactivated",
                "deactivated_at": int(time.time() * 1000),
                "updated_at": int(time.time() * 1000),
                "session_token": "",
                "auth_sessions": [],
            }
        },
    )
    return {"message": "User account deactivated."}


@router.post("/admin/users/{app}/{user_id}/reactivate")
def reactivate_managed_user(
    app: str,
    user_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_subscription_admin_user(current_user)
    collection, _ = _admin_directory_collection(app)
    user = _managed_customer_or_404(collection, user_id)
    collection.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "account_status": "active",
                "reactivated_at": int(time.time() * 1000),
                "updated_at": int(time.time() * 1000),
            },
            "$unset": {"deactivated_at": ""},
        },
    )
    return {"message": "User account reactivated."}


@router.post("/admin/users/{app}/{user_id}/delete")
def delete_managed_user(
    app: str,
    user_id: str,
    payload: DeleteManagedUserRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    """Permanently delete a regular account after exact-email confirmation."""
    ensure_subscription_admin_user(current_user)
    collection, app_name = _admin_directory_collection(app)
    user = _managed_customer_or_404(collection, user_id)
    if _normalize_email(payload.confirmation_email) != _normalize_email(user.get("email", "")):
        raise HTTPException(status_code=400, detail="Email confirmation does not match this account.")

    if app_name == "main":
        deleted = delete_user_account(user)
    else:
        database = collection.database
        user_filter = {
            "$or": [
                {"user_id": user.get("user_id", "")},
                {"email": user.get("email", "")},
            ]
        }
        requests_deleted = database["subscription_requests"].delete_many(
            user_filter
        ).deleted_count
        checkout_deleted = database["subscription_checkout_sessions"].delete_many(
            user_filter
        ).deleted_count
        users_deleted = collection.delete_one({"_id": user["_id"]}).deleted_count
        deleted = {
            "users_deleted": users_deleted,
            "subscription_requests_deleted": requests_deleted,
            "checkout_sessions_deleted": checkout_deleted,
        }
    return {"message": "User account permanently deleted.", "deleted": deleted}


@router.get("/admin/subscription-requests")
def list_admin_subscription_requests(
    app: str = Query(default="lite"),
    status: str = Query(default="pending"),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    """Read Main or Lite plan requests using the central Admin session."""
    ensure_subscription_admin_user(current_user)
    _, requests, app_name = _admin_product_collections(app)
    docs = list(
        requests.find(_admin_request_status_filter(status), {"_id": 0})
        .sort([("requested_at", -1), ("updated_at", -1)])
        .limit(limit)
    )
    return {
        "app": app_name,
        "requests": [_admin_request_payload(doc) for doc in docs],
        "count": len(docs),
    }


@router.get("/admin/projects")
def list_admin_projects(
    app: str = Query(default="main"),
    limit: int = Query(default=500, ge=1, le=500),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    """List Main or Lite projects for the central Admin Console.

    This deliberately reads the raw product collection after verifying the
    central admin session; the regular ``/projects`` route is owner-scoped.
    """
    ensure_subscription_admin_user(current_user)
    users, _, app_name = _admin_product_collections(app)
    projects_collection = users.database["sites"]
    candidates = list(
        projects_collection.find(
            {},
            {
                "site_name": 1,
                "project_name": 1,
                "name": 1,
                "dxf_project_id": 1,
                "location": 1,
                "project_location": 1,
                "area_location": 1,
                "owner_user_id": 1,
                "owner_id": 1,
                "owner_email": 1,
                "owner_name": 1,
                "created_by_id": 1,
                "created_by_email": 1,
                "created_by_name": 1,
                "created_by": 1,
                "user_id": 1,
                "user_email": 1,
                "created_at": 1,
                "createdAt": 1,
                "updated_at": 1,
                "updatedAt": 1,
            },
        ).sort([("created_at", -1), ("updated_at", -1), ("_id", -1)])
    )

    projects = []
    seen_site_names = set()
    owner_ids = set()
    owner_emails = set()
    for project in candidates:
        site_name = str(
            project.get("site_name")
            or project.get("project_name")
            or project.get("name")
            or project.get("dxf_project_id")
            or ""
        ).strip()
        site_key = site_name.lower()
        if not site_name or site_key in seen_site_names:
            continue
        seen_site_names.add(site_key)
        projects.append(project)
        owner_id = str(
            project.get("owner_user_id")
            or project.get("owner_id")
            or project.get("created_by_id")
            or project.get("user_id")
            or ""
        ).strip()
        owner_email = _normalize_email(
            str(
                project.get("owner_email")
                or project.get("created_by_email")
                or project.get("user_email")
                or ""
            )
        )
        if owner_id:
            owner_ids.add(owner_id)
        if owner_email:
            owner_emails.add(owner_email)
        if len(projects) >= limit:
            break

    owner_filters = []
    if owner_ids:
        owner_filters.append({"user_id": {"$in": list(owner_ids)}})
    if owner_emails:
        owner_filters.append({"email": {"$in": list(owner_emails)}})
    owners_by_id = {}
    owners_by_email = {}
    if owner_filters:
        for owner in users.find(
            {"$or": owner_filters},
            {"user_id": 1, "email": 1, "name": 1},
        ):
            owner_id = str(owner.get("user_id") or "").strip()
            owner_email = _normalize_email(str(owner.get("email") or ""))
            if owner_id:
                owners_by_id[owner_id] = owner
            if owner_email:
                owners_by_email[owner_email] = owner

    payload = []
    for project in projects:
        owner_id = str(
            project.get("owner_user_id")
            or project.get("owner_id")
            or project.get("created_by_id")
            or project.get("user_id")
            or ""
        ).strip()
        owner_email = _normalize_email(
            str(
                project.get("owner_email")
                or project.get("created_by_email")
                or project.get("user_email")
                or ""
            )
        )
        owner = owners_by_id.get(owner_id) or owners_by_email.get(owner_email)
        payload.append(_admin_project_registry_payload(project, owner))
    return {"app": app_name, "projects": payload, "count": len(payload)}


def _review_admin_subscription_request(
    *,
    app: str,
    request_id: str,
    approve: bool,
    current_user: AuthenticatedUser,
) -> dict:
    users, requests, app_name = _admin_product_collections(app)
    request = requests.find_one({"request_id": request_id.strip()})
    if not request:
        raise HTTPException(status_code=404, detail="Subscription request not found.")
    if str(request.get("status") or "").strip().lower() not in {
        "pending",
        "pending_approval",
    }:
        raise HTTPException(
            status_code=400,
            detail="Only pending subscription requests can be reviewed.",
        )
    user = users.find_one({"user_id": request.get("user_id")})
    if not user:
        raise HTTPException(status_code=404, detail="Request owner not found.")

    now_ms = int(time.time() * 1000)
    now_iso = datetime.now(timezone.utc).isoformat()
    if approve:
        subscription = {
            "plan_code": str(request.get("plan_code") or "").strip().lower(),
            "plan_name": str(request.get("plan_name") or "").strip(),
            "monthly_price_usd": request.get("monthly_price_usd"),
            "project_limit": request.get("project_limit"),
            "company_name": str(request.get("company_name") or "").strip(),
            "status": "active",
            "payment_status": "approved",
            "source": "admin_approval",
            "activated_at": now_iso,
            "approved_at": now_iso,
            "approved_by_user_id": current_user.user_id,
            "approved_by_email": current_user.email,
            "request_id": str(request.get("request_id") or "").strip(),
        }
        users.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "subscription": subscription,
                    "workspace": str(request.get("company_name") or "").strip()
                    or str(user.get("workspace") or "").strip(),
                    "updated_at": now_ms,
                },
                "$unset": {"pending_subscription_request": ""},
            },
        )
        status = "approved"
        extra = {"approved_subscription": subscription}
    else:
        users.update_one(
            {"_id": user["_id"]},
            {
                "$unset": {"pending_subscription_request": ""},
                "$set": {"updated_at": now_ms},
            },
        )
        status = "rejected"
        extra = {}

    requests.update_one(
        {"_id": request["_id"]},
        {
            "$set": {
                "status": status,
                "reviewed_at": now_iso,
                "reviewed_by_user_id": current_user.user_id,
                "reviewed_by_email": current_user.email,
                "updated_at": now_iso,
                **extra,
            }
        },
    )
    updated = requests.find_one({"_id": request["_id"]}) or request
    return {
        "message": f"Subscription request {status}.",
        "app": app_name,
        "request": _admin_request_payload(updated),
    }


@router.post("/admin/subscription-requests/{app}/{request_id}/approve")
def approve_admin_subscription_request(
    app: str,
    request_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_subscription_admin_user(current_user)
    return _review_admin_subscription_request(
        app=app,
        request_id=request_id,
        approve=True,
        current_user=current_user,
    )


@router.post("/admin/subscription-requests/{app}/{request_id}/reject")
def reject_admin_subscription_request(
    app: str,
    request_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_subscription_admin_user(current_user)
    return _review_admin_subscription_request(
        app=app,
        request_id=request_id,
        approve=False,
        current_user=current_user,
    )


@router.get("/users")
def list_admin_users(
    app: str = Query(default="main"),
    limit: int = Query(default=500, ge=1, le=500),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    """List actual account records for the designated subscription admin."""
    ensure_subscription_admin_user(current_user)
    collection, app_name = _admin_directory_collection(app)
    users = list(
        collection.find(
            {},
            {
                "_id": 0,
                "user_id": 1,
                "email": 1,
                "name": 1,
                "workspace": 1,
                "role": 1,
                "account_role": 1,
                "is_subscription_admin": 1,
                "subscription": 1,
                "pending_subscription_request": 1,
                "account_status": 1,
                "created_at": 1,
                "last_login_at": 1,
                "last_login_app": 1,
            },
        )
        .sort([("created_at", -1), ("email", 1)])
        .limit(limit)
    )
    return {
        "app": app_name,
        "users": [
            _admin_directory_user_payload(user, app_name=app_name)
            for user in users
        ],
        "count": len(users),
    }


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    current_password = payload.current_password.strip()
    new_password = payload.new_password.strip()
    if not current_password or not new_password:
        raise HTTPException(status_code=400, detail="Both password fields are required.")
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if current_password == new_password:
        raise HTTPException(
            status_code=400,
            detail="New password must be different from current password.",
        )

    change_user_password(
        user_id=current_user.user_id,
        current_password=current_password,
        new_password=new_password,
    )
    return {"message": "Password updated successfully."}


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest):
    reset_user_password_by_email(
        email=payload.email,
        new_password=payload.new_password,
    )
    return {"message": "Password updated successfully."}


@router.delete("/account")
def delete_account(current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    user = raw_users_collection.find_one({"user_id": current_user.user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    deleted = delete_user_account(user)
    return {
        "message": "Account deleted successfully.",
        "deleted": deleted,
    }


@router.post("/logout")
def logout(
    payload: LogoutRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
):
    access_token = credentials.credentials.strip() if credentials else ""
    revoke_user_session(
        access_token=access_token,
        refresh_token=payload.refresh_token,
    )
    return {"message": "Logged out successfully."}


@router.get("/delete-account", response_class=HTMLResponse)
def delete_account_page():
    return HTMLResponse(_delete_account_page())


@router.post("/delete-account", response_class=HTMLResponse)
def delete_account_via_web(
    email: str = Form(...),
    password: str = Form(...),
):
    user = authenticate_user(email, password)
    if not user:
        return HTMLResponse(
            _delete_account_page(message="Invalid email or password.", is_error=True),
            status_code=401,
        )

    delete_user_account(user)
    return HTMLResponse(
        _delete_account_page(
            message="Your account and related data were deleted successfully."
        )
    )
