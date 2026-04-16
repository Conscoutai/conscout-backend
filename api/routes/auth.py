from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import (
    authenticate_user,
    create_user,
    require_authenticated_user,
    sanitize_user_payload,
    start_user_session,
)
from core.auth_context import AuthenticatedUser
from core.database import raw_users_collection


router = APIRouter(prefix="/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class SignupRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str = "admin"


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
        role=payload.role,
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
