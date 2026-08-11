"""Reset administrator identities into the dedicated admin database.

The command is dry-run by default. Execution backs up every affected document
to ``<ADMIN_DB_NAME>.admin_backups`` and then performs the reset atomically.
The bootstrap password is read from ``ADMIN_BOOTSTRAP_PASSWORD`` or a hidden
interactive prompt; it is never accepted as a command-line argument.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
load_dotenv(ROOT_DIR / ".env")

from core.auth import ACCOUNT_ROLE_SUPER_ADMIN, _hash_password, verify_password
from core.config import ADMIN_DB_NAME, DB_NAME, LITE_ADMIN_DB_NAME
from core.database import client, ensure_admin_directory_indexes


ADMIN_FILTER = {
    "$or": [
        {
            "account_role": {
                "$in": ["admin", "technical_admin", "super_admin"]
            }
        },
        {"is_subscription_admin": True},
    ]
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email",
        default=os.getenv("ADMIN_BOOTSTRAP_EMAIL", "").strip().lower(),
        help="Email for the sole Super Admin account.",
    )
    parser.add_argument(
        "--name",
        default=os.getenv("ADMIN_BOOTSTRAP_NAME", "ConScout AI").strip(),
        help="Display name for the Super Admin account.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the reset. Without this option, only an audit is printed.",
    )
    parser.add_argument(
        "--confirm-email",
        default="",
        help="Must exactly match --email when --execute is used.",
    )
    return parser.parse_args()


def _source_specs():
    return [
        (DB_NAME, "users", ADMIN_FILTER),
        (LITE_ADMIN_DB_NAME, "users", ADMIN_FILTER),
        (ADMIN_DB_NAME, "admins", {}),
    ]


def _audit() -> list[dict]:
    results = []
    for database_name, collection_name, filter_doc in _source_specs():
        collection = client[database_name][collection_name]
        results.append(
            {
                "database": database_name,
                "collection": collection_name,
                "matched_admin_records": collection.count_documents(filter_doc),
                "total_records": collection.count_documents({}),
            }
        )
    return results


def _password() -> str:
    password = os.getenv("ADMIN_BOOTSTRAP_PASSWORD", "")
    if not password:
        password = getpass.getpass("New Super Admin password: ")
    if len(password) < 12:
        raise SystemExit("Super Admin password must be at least 12 characters.")
    return password


def _super_admin_document(*, email: str, name: str, password: str) -> dict:
    now = int(time.time() * 1000)
    return {
        "user_id": uuid.uuid4().hex,
        "email": email,
        "name": name,
        "workspace": "ConScout Administration",
        "password_hash": _hash_password(password),
        "role": "admin",
        "account_role": ACCOUNT_ROLE_SUPER_ADMIN,
        "admin_type": ACCOUNT_ROLE_SUPER_ADMIN,
        "is_subscription_admin": True,
        "account_status": "active",
        "allowed_apps": ["main"],
        "session_token": "",
        "auth_sessions": [],
        "last_login_at": None,
        "created_at": now,
        "updated_at": now,
    }


def _execute(*, email: str, name: str, password: str) -> dict:
    if ADMIN_DB_NAME in {DB_NAME, LITE_ADMIN_DB_NAME}:
        raise SystemExit("ADMIN_DB_NAME must differ from both product databases.")

    backup_id = f"admin-reset-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    backed_up_at = datetime.now(timezone.utc)
    backup_collection = client[ADMIN_DB_NAME]["admin_backups"]
    backup_records = []
    for database_name, collection_name, filter_doc in _source_specs():
        for document in client[database_name][collection_name].find(filter_doc):
            backup_records.append(
                {
                    "backup_id": backup_id,
                    "source_database": database_name,
                    "source_collection": collection_name,
                    "backed_up_at": backed_up_at,
                    "document": document,
                }
            )
    if backup_records:
        backup_collection.insert_many(backup_records, ordered=True)
    backup_collection.insert_one(
        {
            "backup_id": backup_id,
            "record_type": "manifest",
            "backed_up_at": backed_up_at,
            "record_count": len(backup_records),
            "target_super_admin_email": email,
        }
    )
    backup_collection.create_index("backup_id", name="admin_backup_id")

    super_admin = _super_admin_document(email=email, name=name, password=password)
    with client.start_session() as session:
        with session.start_transaction():
            client[ADMIN_DB_NAME]["admins"].delete_many({}, session=session)
            client[ADMIN_DB_NAME]["admins"].insert_one(
                super_admin, session=session
            )
            client[DB_NAME]["users"].delete_many(ADMIN_FILTER, session=session)
            client[LITE_ADMIN_DB_NAME]["users"].delete_many(
                ADMIN_FILTER, session=session
            )

    ensure_admin_directory_indexes()
    stored = client[ADMIN_DB_NAME]["admins"].find_one({"email": email})
    if not stored or not verify_password(password, stored.get("password_hash", "")):
        raise RuntimeError("Super Admin verification failed after reset.")

    return {
        "backup_id": backup_id,
        "backed_up_records": len(backup_records),
        "super_admin_email": email,
        "admin_database": ADMIN_DB_NAME,
        "admin_collection": "admins",
        "post_reset": _audit(),
    }


def main() -> None:
    args = _arguments()
    if not args.email or "@" not in args.email:
        raise SystemExit("A valid --email is required.")
    if not args.name:
        raise SystemExit("A non-empty --name is required.")

    client.admin.command("ping")
    before = _audit()
    if not args.execute:
        print(json.dumps({"mode": "dry-run", "before": before}, indent=2))
        return
    if args.confirm_email.strip().lower() != args.email:
        raise SystemExit("--confirm-email must exactly match --email.")

    result = _execute(email=args.email, name=args.name, password=_password())
    print(json.dumps({"mode": "executed", "before": before, **result}, indent=2))


if __name__ == "__main__":
    main()
