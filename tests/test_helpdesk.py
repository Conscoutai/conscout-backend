from __future__ import annotations

import unittest
import os
import io
import tempfile
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

from api.routes import helpdesk
from core.auth_context import AuthenticatedUser
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile


def _value_at(document, dotted_key):
    value = document
    for part in dotted_key.split("."):
        if isinstance(value, list):
            return [_value_at(item, ".".join(dotted_key.split(".")[1:])) for item in value]
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _matches(document, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(document, clause) for clause in expected):
                return False
            continue
        if key == "$and":
            if not all(_matches(document, clause) for clause in expected):
                return False
            continue
        actual = _value_at(document, key)
        actual_values = actual if isinstance(actual, list) else [actual]
        if isinstance(expected, dict):
            if "$in" in expected and not any(
                value in expected["$in"] for value in actual_values
            ):
                return False
            if "$ne" in expected and any(
                value == expected["$ne"] for value in actual_values
            ):
                return False
            continue
        if expected not in actual_values:
            return False
    return True


class FakeCursor(list):
    def sort(self, key_or_list, direction=None):
        specs = key_or_list if isinstance(key_or_list, list) else [(key_or_list, direction)]
        for key, order in reversed(specs):
            super().sort(
                key=lambda document: _value_at(document, key) or 0,
                reverse=int(order or 1) < 0,
            )
        return self

    def limit(self, count):
        del self[count:]
        return self


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = [deepcopy(document) for document in (documents or [])]

    def find_one(self, query, _projection=None):
        return next(
            (document for document in self.documents if _matches(document, query)),
            None,
        )

    def find(self, query=None, _projection=None):
        return FakeCursor(
            [document for document in self.documents if _matches(document, query or {})]
        )

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
        document.update(deepcopy(update.get("$set", {})))
        for key, amount in update.get("$inc", {}).items():
            document[key] = int(document.get(key) or 0) + amount
        return SimpleNamespace(modified_count=1)

    def delete_one(self, query):
        before = len(self.documents)
        self.documents = [
            document for document in self.documents if not _matches(document, query)
        ]
        return SimpleNamespace(deleted_count=before - len(self.documents))

    def delete_many(self, query):
        return self.delete_one(query)


class HelpdeskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tickets = FakeCollection()
        self.messages = FakeCollection()
        self.flags = FakeCollection()
        self.admins = FakeCollection(
            [
                {
                    "user_id": "admin-1",
                    "email": "support@conscout.com",
                    "name": "Technical Support",
                    "account_role": "technical_admin",
                    "is_subscription_admin": True,
                }
            ]
        )
        self.user = AuthenticatedUser(
            user_id="user-1", email="owner@example.com", name="Project Owner"
        )
        self.other_user = AuthenticatedUser(
            user_id="user-2", email="other@example.com", name="Other User"
        )
        self.admin = AuthenticatedUser(
            user_id="admin-1", email="support@conscout.com", name="Technical Support"
        )
        self.patches = [
            patch.object(helpdesk, "raw_helpdesk_tickets_collection", self.tickets),
            patch.object(helpdesk, "raw_helpdesk_messages_collection", self.messages),
            patch.object(helpdesk, "raw_ai_quality_flags_collection", self.flags),
            patch.object(helpdesk, "raw_admins_collection", self.admins),
            patch.object(
                helpdesk,
                "_require_helpdesk_admin",
                lambda current_user: "technical_admin"
                if current_user.user_id == "admin-1"
                else (_ for _ in ()).throw(PermissionError()),
            ),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    async def test_customer_ticket_is_private_and_can_be_replied_to(self):
        created = await helpdesk.create_ticket(
            subject="Floor plan will not open",
            description="The floor plan remains on the loading screen.",
            category="technical_issue",
            priority="high",
            app="ConScout Web",
            attachments=None,
            current_user=self.user,
        )
        ticket = created["ticket"]

        owner_list = helpdesk.list_tickets(
            status="all", limit=100, current_user=self.user
        )
        other_list = helpdesk.list_tickets(
            status="all", limit=100, current_user=self.other_user
        )
        self.assertEqual(len(owner_list["tickets"]), 1)
        self.assertEqual(other_list["tickets"], [])
        self.assertEqual(ticket["priority"], "high")
        self.assertEqual(ticket["app"], "web")

        replied = await helpdesk.reply_to_ticket(
            ticket_id=ticket["id"],
            body="The same issue occurs after a browser refresh.",
            attachments=None,
            current_user=self.user,
        )
        self.assertEqual(replied["ticket"]["status"], "open")
        self.assertEqual(len(replied["ticket"]["messages"]), 2)

    async def test_admin_assignment_reply_and_internal_note_workflow(self):
        created = await helpdesk.create_ticket(
            subject="AI result needs review",
            description="The AI progress result differs from the inspection.",
            category="ai_response",
            priority="normal",
            app="web",
            attachments=None,
            current_user=self.user,
        )
        ticket_id = created["ticket"]["id"]

        assigned = helpdesk.admin_update_ticket(
            ticket_id=ticket_id,
            payload=helpdesk.AdminTicketUpdate(
                status="in_progress", assign_to_me=True
            ),
            current_user=self.admin,
        )["ticket"]
        self.assertEqual(assigned["status"], "in_progress")
        self.assertEqual(assigned["assigned_admin"]["id"], "admin-1")

        await helpdesk.admin_reply_to_ticket(
            ticket_id=ticket_id,
            body="Checking the model trace and verified inspection data.",
            internal_note=True,
            status="",
            attachments=None,
            current_user=self.admin,
        )
        public_ticket = helpdesk.get_ticket(ticket_id, current_user=self.user)[
            "ticket"
        ]
        self.assertEqual(len(public_ticket["messages"]), 1)

        admin_reply = await helpdesk.admin_reply_to_ticket(
            ticket_id=ticket_id,
            body="We found the mismatch. Please confirm the Aug 13 tour.",
            internal_note=False,
            status="waiting_for_user",
            attachments=None,
            current_user=self.admin,
        )
        self.assertEqual(admin_reply["ticket"]["status"], "waiting_for_user")
        self.assertEqual(len(admin_reply["ticket"]["messages"]), 3)

    async def test_attachment_is_stored_and_only_available_to_ticket_participants(self):
        upload = UploadFile(
            file=io.BytesIO(b"support screenshot"),
            filename="screen.png",
            headers=Headers({"content-type": "image/png"}),
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            helpdesk, "DATA_DIR", directory
        ):
            created = await helpdesk.create_ticket(
                subject="Screenshot of viewer issue",
                description="The screenshot shows the viewer loading failure.",
                category="technical_issue",
                priority="normal",
                app="web",
                attachments=[upload],
                current_user=self.user,
            )
            ticket = created["ticket"]
            attachment = ticket["messages"][0]["attachments"][0]

            response = helpdesk.download_attachment(
                ticket_id=ticket["id"],
                attachment_id=attachment["id"],
                current_user=self.user,
            )
            self.assertEqual(response.filename, "screen.png")
            self.assertTrue(os.path.isfile(response.path))

            with self.assertRaises(HTTPException) as denied:
                helpdesk.download_attachment(
                    ticket_id=ticket["id"],
                    attachment_id=attachment["id"],
                    current_user=self.other_user,
                )
            self.assertEqual(denied.exception.status_code, 403)

    async def test_ai_flag_creates_linked_quality_item_and_support_ticket(self):
        result = await helpdesk.create_ai_quality_flag(
            payload=helpdesk.AIFlagCreate(
                reason="incorrect",
                prompt="Summarize progress",
                response="Progress is 42 percent.",
                feedback="The verified inspection reports 55 percent.",
                project_context={"project_id": "fozan"},
                model="construction-summary",
                model_version="2026-08",
                app="web",
            ),
            current_user=self.user,
        )

        self.assertEqual(len(self.flags.documents), 1)
        self.assertEqual(len(self.tickets.documents), 1)
        self.assertEqual(result["flag"]["reason"], "incorrect")
        self.assertEqual(result["ticket"]["category"], "ai_response")
        self.assertEqual(
            self.flags.documents[0]["linked_ticket_id"], result["ticket"]["id"]
        )


if __name__ == "__main__":
    unittest.main()
