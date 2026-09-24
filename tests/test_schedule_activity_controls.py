from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from fastapi import HTTPException

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

from services.progress.work_schedule import baseline_service


class ScheduleActivityControlsTests(unittest.TestCase):
    def setUp(self):
        self.baselines = Mock()
        self.baselines.find_one.return_value = {"baseline_id": "baseline-1"}
        self.activities = Mock()
        self.evidence = Mock()
        self.assignments = Mock()
        self.relationships = Mock()
        self.patches = [
            patch.object(
                baseline_service, "schedule_baselines_collection", self.baselines
            ),
            patch.object(
                baseline_service, "schedule_activities_collection", self.activities
            ),
            patch.object(
                baseline_service, "schedule_evidence_collection", self.evidence
            ),
            patch.object(
                baseline_service, "schedule_assignments_collection", self.assignments
            ),
            patch.object(
                baseline_service,
                "schedule_relationships_collection",
                self.relationships,
            ),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_remove_then_restore_preserves_evidence(self):
        self.activities.find_one.side_effect = [
            {"activity_id": "A1", "activity_internal_id": "internal-1"},
            {
                "activity_id": "A1",
                "activity_internal_id": "internal-1",
                "removed_at": datetime.now(timezone.utc),
            },
        ]
        removed = baseline_service.control_schedule_activity(
            baseline_id="baseline-1",
            activity_id="A1",
            action="remove",
            user_email="admin@example.com",
        )
        restored = baseline_service.control_schedule_activity(
            baseline_id="baseline-1",
            activity_id="A1",
            action="restore",
            user_email="admin@example.com",
        )
        self.assertEqual(
            (removed["status"], restored["status"]), ("removed", "restored")
        )
        self.assertIsNotNone(
            self.activities.update_one.call_args_list[0].args[1]["$set"]["removed_at"]
        )
        self.assertIsNone(
            self.activities.update_one.call_args_list[1].args[1]["$set"]["removed_at"]
        )
        self.evidence.delete_many.assert_not_called()

    def test_permanent_delete_requires_removal_and_clears_linked_records(self):
        self.activities.find_one.return_value = {
            "activity_id": "A1",
            "activity_internal_id": "internal-1",
        }
        with self.assertRaises(HTTPException) as error:
            baseline_service.control_schedule_activity(
                baseline_id="baseline-1",
                activity_id="A1",
                action="delete",
                user_email="admin@example.com",
            )
        self.assertEqual(error.exception.status_code, 409)
        self.activities.delete_one.assert_not_called()

        self.activities.find_one.return_value["removed_at"] = datetime.now(timezone.utc)
        result = baseline_service.control_schedule_activity(
            baseline_id="baseline-1",
            activity_id="A1",
            action="delete",
            user_email="admin@example.com",
        )
        self.assertEqual(result["status"], "deleted")
        self.activities.delete_one.assert_called_once_with(
            {"baseline_id": "baseline-1", "activity_id": "A1"}
        )
        self.evidence.delete_many.assert_called_once_with(
            {"baseline_id": "baseline-1", "activity_internal_id": "internal-1"}
        )
        self.assignments.delete_many.assert_called_once()
        self.relationships.delete_many.assert_called_once()


if __name__ == "__main__":
    unittest.main()
