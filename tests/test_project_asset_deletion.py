from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi import HTTPException

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

    def test_remove_single_baseline_preserves_other_versions_and_active_baseline(self):
        baselines = self._collection()
        baselines.find_one.return_value = {
            "baseline_id": "old-version", "project_id": "project-1", "is_active": False
        }
        with (
            patch.object(baseline_service, "floorplans_collection", self.floorplans),
            patch.object(baseline_service, "schedule_baselines_collection", baselines),
        ):
            result = baseline_service.remove_schedule_baseline(
                project_ref="project-1", baseline_id="old-version"
            )
        self.assertEqual(result["baseline_id"], "old-version")
        baselines.update_one.assert_called_once()
        baselines.delete_many.assert_not_called()
        self.floorplans.update_many.assert_not_called()

        baselines.find_one.return_value = {
            "baseline_id": "active-version", "project_id": "project-1", "is_active": True
        }
        with (
            patch.object(baseline_service, "floorplans_collection", self.floorplans),
            patch.object(baseline_service, "schedule_baselines_collection", baselines),
        ):
            with self.assertRaises(HTTPException) as error:
                baseline_service.remove_schedule_baseline(
                    project_ref="project-1", baseline_id="active-version"
                )
        self.assertEqual(error.exception.status_code, 409)

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
        updates = self._collection()

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
            patch.object(baseline_service, "schedule_updates_collection", updates),
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
        self.assertEqual(updates.delete_many.call_count, 2)
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

    def test_legacy_unconfirmed_zone_plan_is_not_a_discardable_proposal(self):
        self.project.update(
            {
                "schedule_zones": [{"name": "Legacy"}],
                "schedule_zone_plan": {
                    "zone_plan_id": "legacy-plan",
                    "confirmation_status": "needs_review",
                },
            }
        )
        with (
            patch.object(baseline_service, "floorplans_collection", self.floorplans),
            patch.object(
                baseline_service, "_schedule_zone_activity_mapping", return_value={}
            ),
            patch.object(
                baseline_service, "_floorplan_asset_available", return_value=False
            ),
        ):
            result = baseline_service.get_schedule_zones("project-1")
        self.assertFalse(result["has_proposed_revision"])
        self.assertEqual(result["zone_plan"]["zone_plan_id"], "legacy-plan")

    def test_discard_proposed_zone_pdf_preserves_approved_plan_and_file(self):
        self.project.update(
            {
                "schedule_zones": [{"name": "Approved"}],
                "schedule_zone_plan": {"zone_plan_id": "approved-plan"},
                "proposed_schedule_zones": [{"name": "Draft"}],
                "proposed_schedule_zone_plan": {
                    "zone_plan_id": "draft-plan",
                    "source_url": "/sites/project-1/zone-plans/v2_draft-plan_draft.pdf",
                },
            }
        )
        with tempfile.TemporaryDirectory() as temporary_root:
            plan_dir = Path(temporary_root, "project-1", "zone-plans")
            plan_dir.mkdir(parents=True)
            approved_file = Path(plan_dir, "v1_approved-plan_approved.pdf")
            draft_file = Path(plan_dir, "v2_draft-plan_draft.pdf")
            approved_file.write_bytes(b"approved")
            draft_file.write_bytes(b"draft")

            with (
                patch.object(baseline_service, "floorplans_collection", self.floorplans),
                patch.object(
                    baseline_service, "site_storage_roots", return_value=[temporary_root]
                ),
            ):
                result = baseline_service.discard_proposed_schedule_zone_plan(
                    "project-1", "draft-plan"
                )

            self.assertEqual(result["status"], "discarded")
            self.assertEqual(result["files_deleted"], 1)
            self.assertFalse(draft_file.exists())
            self.assertTrue(approved_file.exists())
            query, update = self.floorplans.update_many.call_args.args
            self.assertEqual(
                query["$and"][1],
                {"proposed_schedule_zone_plan.zone_plan_id": "draft-plan"},
            )
            self.assertIn("proposed_schedule_zone_plan", update["$unset"])
            self.assertNotIn("schedule_zone_plan", update["$unset"])
            self.assertNotIn("schedule_zones", update["$unset"])

    def test_discard_stale_proposal_does_not_change_approved_or_draft_plan(self):
        self.project["proposed_schedule_zone_plan"] = {
            "zone_plan_id": "newer-plan",
        }
        with patch.object(baseline_service, "floorplans_collection", self.floorplans):
            with self.assertRaises(HTTPException) as error:
                baseline_service.discard_proposed_schedule_zone_plan(
                    "project-1", "older-plan"
                )
        self.assertEqual(error.exception.status_code, 409)
        self.floorplans.update_many.assert_not_called()

    def test_discard_boundary_revision_keeps_shared_approved_pdf(self):
        source_url = "/sites/project-1/zone-plans/v1_approved-plan_zones.pdf"
        self.project.update(
            {
                "schedule_zone_plan": {
                    "zone_plan_id": "approved-plan",
                    "source_url": source_url,
                },
                "proposed_schedule_zone_plan": {
                    "zone_plan_id": "boundary-revision",
                    "source_url": source_url,
                },
            }
        )
        with tempfile.TemporaryDirectory() as temporary_root:
            plan_dir = Path(temporary_root, "project-1", "zone-plans")
            plan_dir.mkdir(parents=True)
            approved_file = Path(plan_dir, "v1_approved-plan_zones.pdf")
            approved_file.write_bytes(b"approved")
            with (
                patch.object(baseline_service, "floorplans_collection", self.floorplans),
                patch.object(
                    baseline_service, "site_storage_roots", return_value=[temporary_root]
                ),
            ):
                result = baseline_service.discard_proposed_schedule_zone_plan(
                    "project-1", "boundary-revision"
                )
            self.assertEqual(result["files_deleted"], 0)
            self.assertTrue(approved_file.exists())


if __name__ == "__main__":
    unittest.main()
