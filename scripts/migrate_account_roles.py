"""Backfill platform-level account roles in the Main and Lite user databases.

Run this from the deployed API container after releasing the role model.  It
does not modify the legacy ``role`` field because that field controls
construction-project permissions (admin/stakeholder).
"""

from __future__ import annotations

from core.auth import (
    ACCOUNT_ROLE_ADMIN,
    ACCOUNT_ROLE_LITE_USER,
    ACCOUNT_ROLE_MAIN_USER,
    ACCOUNT_ROLE_SUPER_ADMIN,
    SUBSCRIPTION_ADMIN_EMAIL,
)
from core.config import DB_NAME, LITE_ADMIN_DB_NAME
from core.database import client


def _role_for(document: dict, *, default_role: str) -> str:
    email = str(document.get("email") or "").strip().lower()
    if email == SUBSCRIPTION_ADMIN_EMAIL:
        return ACCOUNT_ROLE_SUPER_ADMIN
    current = str(document.get("account_role") or "").strip().lower()
    if current in {
        ACCOUNT_ROLE_MAIN_USER,
        ACCOUNT_ROLE_LITE_USER,
        ACCOUNT_ROLE_ADMIN,
        ACCOUNT_ROLE_SUPER_ADMIN,
    }:
        return current
    if document.get("is_subscription_admin") is True:
        return ACCOUNT_ROLE_ADMIN
    return default_role


def _migrate_database(database_name: str, default_role: str) -> dict[str, int]:
    collection = client[database_name]["users"]
    updated = 0
    for document in collection.find({}, {"email": 1, "account_role": 1, "is_subscription_admin": 1}):
        account_role = _role_for(document, default_role=default_role)
        if document.get("account_role") != account_role:
            collection.update_one(
                {"_id": document["_id"]},
                {"$set": {"account_role": account_role}},
            )
            updated += 1
    return {"users": collection.count_documents({}), "updated": updated}


if __name__ == "__main__":
    print(
        {
            "main": _migrate_database(DB_NAME, ACCOUNT_ROLE_MAIN_USER),
            "lite": _migrate_database(LITE_ADMIN_DB_NAME, ACCOUNT_ROLE_LITE_USER),
        }
    )
