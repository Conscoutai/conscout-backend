from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

from services.progress.work_schedule import work_schedule_service as service


class ManualScheduleActivityControlsTests(unittest.TestCase):
    def test_duplicate_old_activity_ids_are_removed_one_at_a_time(self):
        document = {
            "_id": "project-doc",
            "site_name": "fozan",
            "work_schedule": {
                "activities": [
                    {
                        "activity_id": "A-01",
                        "activity_name": "Cycle path Painting",
                        "start_date": "2026-01-01",
                        "end_date": "2026-01-07",
                    },
                    {
                        "activity_id": "A-01",
                        "activity_name": "Planting Shrubs",
                        "start_date": "2026-01-01",
                        "end_date": "2026-01-07",
                    },
                ]
            },
        }
        collection = Mock()
        collection.find_one.side_effect = lambda *args, **kwargs: document

        def save(selector, mutation):
            self.assertEqual(
                selector["work_schedule.activities"],
                document["work_schedule"]["activities"],
            )
            document["work_schedule"]["activities"] = mutation["$set"][
                "work_schedule.activities"
            ]
            return Mock(matched_count=1)

        collection.update_one.side_effect = save
        with (
            patch.object(service, "floorplans_collection", collection),
            patch.object(service, "build_baseline_comparison", return_value=None),
            patch.object(service, "_fetch_tours_for_project", return_value=[]),
        ):
            initial = service.work_schedule_comparison("fozan")
            first, second = initial["activities"]
            self.assertNotEqual(first["entry_id"], second["entry_id"])

            service.control_manual_schedule_activity(
                project_id="fozan", entry_id=first["entry_id"], action="remove"
            )
            after_remove = service.work_schedule_comparison("fozan")
            self.assertEqual(
                [row["activity_name"] for row in after_remove["activities"]],
                ["Planting Shrubs"],
            )
            self.assertEqual(
                after_remove["deleted_activities"][0]["entry_id"], first["entry_id"]
            )

            service.control_manual_schedule_activity(
                project_id="fozan", entry_id=first["entry_id"], action="restore"
            )
            self.assertEqual(
                len(service.work_schedule_comparison("fozan")["activities"]), 2
            )

            service.control_manual_schedule_activity(
                project_id="fozan", entry_id=first["entry_id"], action="remove"
            )
            service.control_manual_schedule_activity(
                project_id="fozan", entry_id=first["entry_id"], action="delete"
            )
            final = service.work_schedule_comparison("fozan")
            self.assertEqual(len(final["activities"]), 1)
            self.assertEqual(final["activities"][0]["entry_id"], second["entry_id"])
            self.assertEqual(final["deleted_activities"], [])


if __name__ == "__main__":
    unittest.main()
