"""Notification contract regressions with database and push providers mocked."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]


def load_functions(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for function in functions:
        function.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), path, "exec"), namespace)
    return namespace


class NotificationParityTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(user_id="alice", email="alice@example.com")
        self.collection = Mock()
        self.ns = {
            "Depends": lambda value: None, "require_authenticated_user": None,
            "notifications_collection": self.collection, "_now_ms": lambda: 123,
            "_recipient_filter": lambda user: {"recipient_user_id": user.user_id},
            "_find_notification_for_current_user": Mock(return_value={"_id": "n1", "status": "accepted"}),
            "_serialize_notification": lambda record: record,
        }
        load_functions("api/routes/notifications.py", {
            "unread_notification_count", "mark_all_notifications_read",
            "_update_notification_state", "mark_notification_as_unread",
            "archive_notification", "unarchive_notification",
        }, self.ns)

    def test_badge_counts_all_unread_active_records_for_current_recipient(self):
        self.collection.count_documents.return_value = 4
        self.assertEqual(self.ns["unread_notification_count"](self.user), {"count": 4})
        query = self.collection.count_documents.call_args.args[0]
        self.assertEqual(query["recipient_user_id"], "alice")
        self.assertEqual(query["is_archived"], {"$ne": True})
        self.assertEqual(query["status"], {"$ne": "archived"})

    def test_mark_all_read_is_recipient_scoped_and_excludes_archives(self):
        self.collection.update_many.return_value.modified_count = 3
        result = self.ns["mark_all_notifications_read"](self.user)
        self.assertEqual(result["updated_count"], 3)
        query, update = self.collection.update_many.call_args.args
        self.assertEqual(query["recipient_user_id"], "alice")
        self.assertEqual(query["is_archived"], {"$ne": True})
        self.assertTrue(update["$set"]["is_read"])

    def test_archive_restore_preserve_invite_decision(self):
        for name, expected in [("archive_notification", True), ("unarchive_notification", False)]:
            self.ns[name]("n1", self.user)
            self.ns["_find_notification_for_current_user"].assert_called_with("n1", self.user)
            changes = self.collection.update_one.call_args.args[1]["$set"]
            self.assertEqual(changes["is_archived"], expected)
            self.assertNotIn("status", changes)

    def test_unread_restores_server_read_state(self):
        self.ns["mark_notification_as_unread"]("n1", self.user)
        changes = self.collection.update_one.call_args.args[1]["$set"]
        self.assertFalse(changes["is_read"])
        self.assertEqual(changes["read_at"], 0)

    def test_unauthorized_notification_does_not_write(self):
        self.ns["_find_notification_for_current_user"].side_effect = PermissionError("Not your notification")
        with self.assertRaises(PermissionError):
            self.ns["archive_notification"]("other-user-record", self.user)
        self.collection.update_one.assert_not_called()

    def test_unregister_exact_token_owner_and_app_without_upsert(self):
        devices = Mock()
        ns = load_functions("services/notifications/push_notification_service.py", {
            "_normalize_text", "unregister_device_token",
        }, {"notification_devices_collection": devices, "_now_ms": lambda: 123})
        devices.update_one.return_value.matched_count = 0
        result = ns["unregister_device_token"](user_id="alice", fcm_token=" bob-device ", app="main")
        self.assertFalse(result["unregistered"])
        self.assertEqual(devices.update_one.call_args.args[0], {
            "user_id": "alice", "fcm_token": "bob-device", "app": "main",
        })
        self.assertFalse(devices.update_one.call_args.kwargs.get("upsert", False))
        self.assertFalse(devices.update_one.call_args.args[1]["$set"]["is_active"])

    def test_push_payload_preserves_exact_evidence_ids(self):
        ns = load_functions("services/notifications/push_notification_service.py", {
            "_normalize_text", "_message_data",
        }, {})
        data = ns["_message_data"]({"_id": "n1", "route": "/progress/budget", "metadata": {
            "tour_id": "t1", "pano_id": "p1", "comment_id": "c1", "activity_id": "a1",
        }})
        self.assertEqual(data["comment_id"], "c1")
        self.assertEqual(data["pano_id"], "p1")
        self.assertEqual(data["tour_id"], "t1")
        self.assertTrue(all(isinstance(value, str) for value in data.values()))

    def test_list_can_include_archived_records_for_web_and_mobile(self):
        syncs = {name: Mock() for name in (
            "_best_effort_inspection_notification_sync", "_best_effort_comment_notification_sync",
            "_best_effort_prediction_notification_sync", "_best_effort_safety_notification_sync",
        )}
        self.ns.update(syncs)
        self.collection.find.return_value.sort.return_value = []
        load_functions("api/routes/notifications.py", {"list_notifications"}, self.ns)
        self.ns["list_notifications"](current_user=self.user)
        self.assertEqual(self.collection.find.call_args.args[0]["is_archived"], {"$ne": True})
        self.ns["list_notifications"](include_archived=True, current_user=self.user)
        self.assertEqual(self.collection.find.call_args.args[0], {"recipient_user_id": "alice"})


if __name__ == "__main__":
    unittest.main()
