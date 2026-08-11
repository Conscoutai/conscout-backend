from __future__ import annotations

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from core import auth
from core.auth_context import AuthenticatedUser


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = [deepcopy(document) for document in (documents or [])]

    def find_one(self, query, _projection=None):
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                return document
        return None

    def insert_one(self, document):
        stored = deepcopy(document)
        stored.setdefault("_id", f"id-{len(self.documents) + 1}")
        document.setdefault("_id", stored["_id"])
        self.documents.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    def update_one(self, query, update):
        document = self.find_one(query)
        if not document:
            return SimpleNamespace(modified_count=0)
        document.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=1)


class AdminDirectoryTests(unittest.TestCase):
    def test_admin_authentication_uses_only_dedicated_directory(self):
        password = "secure-password"
        product_users = FakeCollection(
            [
                {
                    "_id": "product-1",
                    "user_id": "product-user",
                    "email": "same@example.com",
                    "password_hash": auth._hash_password(password),
                    "account_role": auth.ACCOUNT_ROLE_MAIN_USER,
                }
            ]
        )
        admins = FakeCollection(
            [
                {
                    "_id": "admin-1",
                    "user_id": "admin-user",
                    "email": "same@example.com",
                    "password_hash": auth._hash_password(password),
                    "account_role": auth.ACCOUNT_ROLE_SUPER_ADMIN,
                }
            ]
        )

        with patch.object(auth, "raw_users_collection", product_users), patch.object(
            auth, "raw_admins_collection", admins
        ):
            self.assertEqual(
                auth.authenticate_user("same@example.com", password)["user_id"],
                "product-user",
            )
            self.assertEqual(
                auth.authenticate_admin_user("same@example.com", password)[
                    "user_id"
                ],
                "admin-user",
            )

    def test_super_admin_creates_technical_admin_in_admin_database(self):
        product_users = FakeCollection()
        admins = FakeCollection()

        with patch.object(auth, "raw_users_collection", product_users), patch.object(
            auth, "raw_admins_collection", admins
        ):
            created = auth.create_technical_admin(
                name="Technical Operator",
                email="operator@example.com",
                password="secure-password",
            )

        self.assertEqual(product_users.documents, [])
        self.assertEqual(len(admins.documents), 1)
        self.assertEqual(
            created["account_role"], auth.ACCOUNT_ROLE_TECHNICAL_ADMIN
        )
        self.assertTrue(
            auth.verify_password(
                "secure-password", admins.documents[0]["password_hash"]
            )
        )

    def test_admin_access_guard_does_not_fall_back_to_product_users(self):
        product_users = FakeCollection(
            [
                {
                    "user_id": "shared-id",
                    "email": "customer@example.com",
                    "account_role": auth.ACCOUNT_ROLE_MAIN_USER,
                }
            ]
        )
        admins = FakeCollection(
            [
                {
                    "user_id": "shared-id",
                    "email": "admin@example.com",
                    "account_role": auth.ACCOUNT_ROLE_TECHNICAL_ADMIN,
                    "is_subscription_admin": True,
                }
            ]
        )
        current_user = AuthenticatedUser(
            user_id="shared-id", email="admin@example.com"
        )

        with patch.object(auth, "raw_users_collection", product_users), patch.object(
            auth, "raw_admins_collection", admins
        ):
            role = auth.ensure_subscription_admin_user(current_user)

        self.assertEqual(role, auth.ACCOUNT_ROLE_TECHNICAL_ADMIN)


if __name__ == "__main__":
    unittest.main()
