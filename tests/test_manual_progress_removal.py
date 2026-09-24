from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

from services.progress.work_schedule import evidence_service


class ManualProgressRemovalTests(unittest.TestCase):
    def test_removes_one_manual_entry_and_invalidates_its_snapshot(self):
        evidence = Mock()
        evidence.find_one.return_value = {
            "evidence_id": "entry-1",
            "review_source": "manual",
            "tour_id": "manual:2026-09-24:abc",
            "project_id": "project-1",
            "activity_id": "activity-1",
            "captured_at": "2026-09-24T09:30:00Z",
        }
        evidence.delete_one.return_value = Mock(deleted_count=1)
        snapshots = Mock()
        with (
            patch.object(evidence_service, "schedule_evidence_collection", evidence),
            patch.object(
                evidence_service, "schedule_progress_snapshots_collection", snapshots
            ),
        ):
            result = evidence_service.remove_manual_activity_progress("entry-1")

        self.assertEqual(result["status"], "removed")
        self.assertEqual(result["activity_id"], "activity-1")
        evidence.delete_one.assert_called_once()
        self.assertEqual(
            snapshots.delete_many.call_args.args[0],
            {"project_id": "project-1", "snapshot_date": "2026-09-24"},
        )

    def test_rejects_tour_and_client_schedule_evidence(self):
        evidence = Mock()
        evidence.find_one.return_value = {
            "evidence_id": "tour-1",
            "review_source": "human",
            "tour_id": "tour-1",
        }
        with patch.object(evidence_service, "schedule_evidence_collection", evidence):
            with self.assertRaises(HTTPException) as error:
                evidence_service.remove_manual_activity_progress("tour-1")
        self.assertEqual(error.exception.status_code, 409)
        evidence.delete_one.assert_not_called()

    def test_rejects_unknown_manual_entry(self):
        evidence = Mock()
        evidence.find_one.return_value = None
        with patch.object(evidence_service, "schedule_evidence_collection", evidence):
            with self.assertRaises(HTTPException) as error:
                evidence_service.remove_manual_activity_progress("missing")
        self.assertEqual(error.exception.status_code, 404)
        evidence.delete_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
