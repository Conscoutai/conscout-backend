from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import (
    authenticate_user,
    change_user_password,
    create_user,
    require_authenticated_user,
    sanitize_user_payload,
    start_user_session,
)
from core.auth_context import AuthenticatedUser
from core.database import raw_users_collection


router = APIRouter(prefix="/auth", tags=["Auth"])


def _normalize_email(value: str) -> str:
    return value.strip().lower()


class LoginRequest(BaseModel):
    email: str
    password: str


class SignupRequest(BaseModel):
    name: str
    email: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/login")
def login(payload: LoginRequest):
    user = authenticate_user(payload.email, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    user = start_user_session(user)
    return {
        "token": user.get("session_token", ""),
        "user": sanitize_user_payload(user),
    }


@router.post("/signup")
def signup(payload: SignupRequest):
    if len(payload.password.strip()) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")

    user = create_user(
        name=payload.name,
        email=payload.email,
        password=payload.password,
    )
    user = start_user_session(user)
    return {
        "token": user.get("session_token", ""),
        "user": sanitize_user_payload(user),
    }


@router.get("/me")
def me(current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    user = raw_users_collection.find_one({"user_id": current_user.user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return {"user": sanitize_user_payload(user)}


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
