from __future__ import annotations

import secrets
import time
from typing import Optional

import requests
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core.auth import (
    authenticate_user,
    change_user_password,
    create_user,
    ensure_user_allowed_for_app,
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
from core.config import GOOGLE_OAUTH_CLIENT_IDS, GOOGLE_OAUTH_HOSTED_DOMAIN
from core.database import raw_users_collection
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
    return normalized


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


class SignupRequest(BaseModel):
    name: str
    email: str
    password: str
    workspace: str = ""
    app: str
    role: Optional[str] = None


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
    photo_url = str(
        payload.photo_url
        or token_payload.get("picture")
        or ""
    ).strip()

    user = raw_users_collection.find_one({"email": email})
    if not user:
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
        "name": display_name or user.get("name", ""),
        "workspace": workspace or user.get("workspace", ""),
        "allowed_apps": [app_name],
        "auth_provider": "google",
        "google_user_id": google_user_id,
        "google_photo_url": photo_url,
        "google_email_verified": True,
    }
    if payload.platform:
        update_fields["last_login_platform"] = payload.platform.strip().lower()
    if payload.intent:
        update_fields["last_google_intent"] = payload.intent.strip().lower()
    raw_users_collection.update_one(
        {"_id": user["_id"]},
        {"$set": update_fields},
    )
    user = raw_users_collection.find_one({"_id": user["_id"]}) or user
    return _finalize_auth_response(user, app_name=app_name)


@router.post("/refresh")
def refresh_session(payload: RefreshSessionRequest):
    app_name = _normalize_app(payload.app or "lite")
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
