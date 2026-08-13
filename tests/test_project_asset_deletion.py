from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

from services.project_setup import project_assets_service
from services.progress.work_schedule import baseline_service


def _matched_result(count: int = 1) -> Mock:
    return Mock(matched_count=count)


class ProjectStorageDeletionTests(unittest.TestCase):
    def test_asset_directory_removal_is_scoped_to_the_project_folder(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            dxf_dir = Path(temporary_root, "project-1", "dxf")
            dxf_dir.mkdir(parents=True)
            Path(dxf_dir, "source.dxf").write_text("0\nEOF", encoding="utf-8")
            sibling_file = Path(temporary_root, "project-2", "dxf", "keep.dxf")
            sibling_file.parent.mkdir(parents=True)
            sibling_file.write_text("keep", encoding="utf-8")

            with patch.object(
                project_assets_service,
                "site_storage_roots",
                return_value=[temporary_root],
            ):
                directories, files = (
                    project_assets_service.remove_project_asset_directories(
                        {"project_id": "project-1"},
                        "dxf",
                    )
                )

            self.assertEqual(directories, 1)
            self.assertEqual(files, 1)
            self.assertFalse(dxf_dir.exists())
            self.assertTrue(sibling_file.exists())

    def test_dxf_delete_clears_source_marker_and_processed_objects_only(self):
        floorplans = Mock()
        floorplans.find_one.return_value = {
            "id": "project-1",
            "project_id": "project-1",
            "dxf_project_id": "project-1",
        }
        floorplans.update_many.return_value = _matched_result()

        with (
            patch.object(project_assets_service, "floorplans_collection", floorplans),
            patch.object(
                project_assets_service,
                "remove_project_asset_directories",
                return_value=(1, 3),
            ),
        ):
            result = project_assets_service.delete_project_dxf_assets("project-1")

        update = floorplans.update_many.call_args.args[1]
        self.assertIn("dxf_project_id", update["$unset"])
        self.assertEqual(update["$set"]["site_objects"], [])
        self.assertNotIn("site_config", update["$unset"])
        self.assertEqual(result["files_deleted"], 3)


class ScheduleAssetDeletionTests(unittest.TestCase):
    def setUp(self):
        self.project = {
            "id": "floorplan-1",
            "project_id": "project-1",
            "site_name": "Project One",
            "owner_user_id": "owner-1",
            "owner_email": "owner@example.com",
        }
        self.floorplans = Mock()
        self.floorplans.find_one.return_value = self.project
        self.floorplans.update_many.return_value = _matched_result()

    def _collection(self) -> Mock:
        collection = Mock()
        collection.delete_many.return_value = Mock(deleted_count=0)
        collection.delete_one.return_value = Mock(deleted_count=1)
        return collection

    def test_schedule_delete_removes_all_versioned_data_not_legacy_schedule(self):
        baselines = self._collection()
        baselines.find.return_value = [
            {"baseline_id": "baseline-1", "project_id": "project-1"},
            {"baseline_id": "baseline-2", "project_id": "project-1"},
        ]
        activities = self._collection()
        relationships = self._collection()
        assignments = self._collection()
        evidence = self._collection()
        snapshots = self._collection()

        with (
            patch.object(baseline_service, "floorplans_collection", self.floorplans),
            patch.object(baseline_service, "schedule_baselines_collection", baselines),
            patch.object(
                baseline_service, "schedule_activities_collection", activities
            ),
            patch.object(
                baseline_service, "schedule_relationships_collection", relationships
            ),
            patch.object(
                baseline_service, "schedule_assignments_collection", assignments
            ),
            patch.object(baseline_service, "schedule_evidence_collection", evidence),
            patch.object(
                baseline_service,
                "schedule_progress_snapshots_collection",
                snapshots,
            ),
            patch.object(
                baseline_service,
                "remove_project_asset_directories",
                return_value=(1, 2),
            ),
        ):
            result = baseline_service.delete_project_schedule_baselines("project-1")

        self.assertEqual(result["versions_deleted"], 2)
        self.assertEqual(activities.delete_many.call_count, 2)
        self.assertEqual(evidence.delete_many.call_count, 2)
        self.assertEqual(snapshots.delete_many.call_count, 2)
        update = self.floorplans.update_many.call_args.args[1]
        self.assertIn("schedule_baseline", update["$unset"])
        self.assertNotIn("work_schedule", update["$unset"])

    def test_zone_plan_delete_removes_active_and_proposed_zones(self):
        self.project.update(
            {
                "schedule_zones": [{"name": "Zone A"}],
                "schedule_zone_plan": {
                    "zone_plan_id": "zone-plan-1",
                    "confirmation_status": "confirmed",
                },
                "proposed_schedule_zones": [{"name": "Zone B"}],
                "proposed_schedule_zone_plan": {
                    "zone_plan_id": "zone-plan-2",
                    "confirmation_status": "needs_review",
                },
            }
        )

        with (
            patch.object(baseline_service, "floorplans_collection", self.floorplans),
            patch.object(
                baseline_service,
                "remove_project_asset_directories",
                return_value=(1, 2),
            ),
        ):
            result = baseline_service.delete_project_schedule_zone_plan("project-1")

        self.assertEqual(result["plans_deleted"], 2)
        self.assertEqual(result["zones_deleted"], 2)
        update = self.floorplans.update_many.call_args.args[1]
        self.assertIn("schedule_zone_plan", update["$unset"])
        self.assertIn("proposed_schedule_zone_plan", update["$unset"])
        self.assertNotIn("schedule_baseline", update["$unset"])


if __name__ == "__main__":
    unittest.main()
