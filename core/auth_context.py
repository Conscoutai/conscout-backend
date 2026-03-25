from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    email: str
    name: str = ""


_current_user: ContextVar[Optional[AuthenticatedUser]] = ContextVar(
    "current_authenticated_user",
    default=None,
)


def set_current_user(user: Optional[AuthenticatedUser]):
    return _current_user.set(user)


def reset_current_user(token) -> None:
    _current_user.reset(token)


def get_current_user() -> Optional[AuthenticatedUser]:
    return _current_user.get()


def merge_owner_filter(filter_doc: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    user = get_current_user()
    if user is None:
        return dict(filter_doc or {})

    owner_clause = {"owner_user_id": user.user_id}
    if not filter_doc:
        return owner_clause

    scoped = dict(filter_doc)
    if "$and" in scoped and isinstance(scoped["$and"], list):
        return {**scoped, "$and": [*scoped["$and"], owner_clause]}
    return {"$and": [scoped, owner_clause]}


def stamp_owned_document(document: dict[str, Any]) -> dict[str, Any]:
    owned = dict(document)
    user = get_current_user()
    if user is None:
        return owned
    owned.setdefault("owner_user_id", user.user_id)
    owned.setdefault("owner_email", user.email)
    return owned
