from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    email: str
    name: str = ""
    role: str = "admin"
    accessible_project_names: tuple[str, ...] = ()
    accessible_floorplan_ids: tuple[str, ...] = ()


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

    if user.role == "stakeholder":
        access_clauses: list[dict[str, Any]] = []
        if user.accessible_project_names:
            access_clauses.extend(
                [
                    {"site_name": {"$in": list(user.accessible_project_names)}},
                    {"dxf_project_id": {"$in": list(user.accessible_project_names)}},
                    {"project_id": {"$in": list(user.accessible_project_names)}},
                ]
            )
        if user.accessible_floorplan_ids:
            access_clauses.append(
                {"floorplan_id": {"$in": list(user.accessible_floorplan_ids)}}
            )

        if not access_clauses:
            access_clause = {"_id": {"$exists": False}}
        elif len(access_clauses) == 1:
            access_clause = access_clauses[0]
        else:
            access_clause = {"$or": access_clauses}
    else:
        access_clause = {"owner_user_id": user.user_id}

    if not filter_doc:
        return access_clause

    scoped = dict(filter_doc)
    if "$and" in scoped and isinstance(scoped["$and"], list):
        return {**scoped, "$and": [*scoped["$and"], access_clause]}
    return {"$and": [scoped, access_clause]}


def stamp_owned_document(document: dict[str, Any]) -> dict[str, Any]:
    owned = dict(document)
    user = get_current_user()
    if user is None:
        return owned
    owned.setdefault("owner_user_id", user.user_id)
    owned.setdefault("owner_email", user.email)
    return owned
