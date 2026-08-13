from __future__ import annotations

import unittest
import os
from collections import Counter
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

from services.progress.work_schedule.analytics_service import _planned_percent
from services.progress.work_schedule.pdf_parser import parse_pdf_schedule
from services.progress.work_schedule.xer_parser import (
    XerParseError,
    normalize_work_category,
    parse_xer,
)
from services.progress.work_schedule.zone_plan_parser import parse_zone_plan_pdf
from services.progress.work_schedule import zone_plan_parser as zone_parser
from services.progress.work_schedule import (
    analytics_service,
    baseline_service,
    evidence_service,
)


SAMPLE_XER_PATH = Path(
    r"C:\Users\safwa\Downloads\SH.ABDULLATIF ALFOZAN STREET RROJECT.xer"
)
SAMPLE_ZONE_PLAN_PATH = Path(r"C:\Users\safwa\Downloads\zones.pdf")


class XerParserTests(unittest.TestCase):
    def test_real_client_xer_imports_expected_schedule_tables(self):
        if not SAMPLE_XER_PATH.exists():
            self.skipTest("Real client XER is not available on this machine")

        parsed = parse_xer(SAMPLE_XER_PATH.read_bytes(), filename=SAMPLE_XER_PATH.name)

        self.assertEqual(parsed["summary"]["activity_count"], 398)
        self.assertEqual(parsed["summary"]["wbs_count"], 162)
        self.assertEqual(parsed["summary"]["relationship_count"], 930)
        self.assertEqual(parsed["summary"]["assignment_count"], 344)
        self.assertEqual(parsed["summary"]["cost_loaded_activity_count"], 288)
        self.assertEqual(parsed["summary"]["labor_loaded_activity_count"], 7)
        self.assertAlmostEqual(parsed["summary"]["target_cost"], 22001352.511)
        self.assertEqual(
            [zone["name"] for zone in parsed["summary"]["zones"]],
            ["Zone A", "Zone B", "Zone C", "Zone D", "Zone E", "Zone F"],
        )
        zone_f_excavation = next(
            activity
            for activity in parsed["activities"]
            if activity["activity_id"] == "FAW.CONS.26"
        )
        self.assertEqual(zone_f_excavation["zone"], "Zone F")
        self.assertEqual(zone_f_excavation["work_category"], "excavation")
        self.assertTrue(zone_f_excavation["photo_trackable"])
        project_wide = [
            activity for activity in parsed["activities"] if not activity["zone"]
        ]
        self.assertEqual(len(project_wide), 104)
        self.assertTrue(
            all(not activity["photo_trackable"] for activity in project_wide)
        )
        self.assertTrue(all(activity["target_cost"] == 0 for activity in project_wide))
        self.assertEqual(
            Counter(activity["wbs_path"][1] for activity in project_wide),
            {
                "Materials Procurement": 78,
                "Engineering": 16,
                "Project Handing Over": 6,
                "Project Milestones": 2,
                "Mobilization & Preliminaries": 1,
                "Testing & Commissioning": 1,
            },
        )
        self.assertEqual(
            sum(
                1
                for activity in parsed["activities"]
                if activity["zone"] and activity["photo_trackable"]
            ),
            214,
        )

    def test_invalid_xer_is_rejected(self):
        with self.assertRaises(XerParseError):
            parse_xer(b"not an xer")

    def test_work_categories_match_ai_labels(self):
        self.assertEqual(normalize_work_category("Thermoplastic Paint"), "painting")
        self.assertEqual(
            normalize_work_category("Supply & Installation Light grey Interlock"),
            "paving_installation",
        )
        self.assertEqual(normalize_work_category("Planting palm trees"), "planting")


class PdfParserTests(unittest.TestCase):
    def test_text_pdf_extracts_reviewable_activity_rows(self):
        from fpdf import FPDF

        document = FPDF()
        document.add_page()
        document.set_font("Arial", size=12)
        document.cell(
            0,
            10,
            "A100 Excavation Zone A 2026-08-01 2026-08-15",
            ln=1,
        )
        raw_pdf = document.output(dest="S")
        if isinstance(raw_pdf, str):
            raw_pdf = raw_pdf.encode("latin-1")

        parsed = parse_pdf_schedule(bytes(raw_pdf), filename="schedule.pdf")

        self.assertEqual(parsed["summary"]["activity_count"], 1)
        self.assertEqual(parsed["activities"][0]["activity_id"], "A100")
        self.assertEqual(parsed["activities"][0]["work_category"], "excavation")
        self.assertEqual(parsed["activities"][0]["mapping_status"], "needs_review")


class ZonePlanParserTests(unittest.TestCase):
    def test_vector_pdf_creates_six_floorplan_polygons(self):
        from fpdf import FPDF

        document = FPDF(orientation="L", unit="mm", format="A4")
        document.add_page()
        boundary_x = [12, 57, 102, 147, 192, 237, 282]
        chainages = ["0+000", "0+500", "1+000", "1+500", "2+000", "2+500", "3+100"]
        document.set_font("Arial", size=12)
        for x, chainage in zip(boundary_x, chainages):
            document.set_xy(x, 75)
            document.cell(25, 8, chainage)
        document.set_font("Arial", size=28)
        for index, code in enumerate("ABCDEF"):
            document.set_xy((boundary_x[index] + boundary_x[index + 1]) / 2, 95)
            document.cell(15, 12, code)
        raw_pdf = document.output(dest="S")
        if isinstance(raw_pdf, str):
            raw_pdf = raw_pdf.encode("latin-1")

        parsed = parse_zone_plan_pdf(
            bytes(raw_pdf),
            filename="zones.pdf",
            floorplan_bounds={"width": 1200, "height": 400},
        )

        self.assertEqual(parsed["summary"]["zone_count"], 6)
        self.assertEqual(
            parsed["summary"]["zone_names"], [f"Zone {code}" for code in "ABCDEF"]
        )
        self.assertEqual(parsed["summary"]["chainage_start_m"], 0)
        self.assertEqual(parsed["summary"]["chainage_end_m"], 3100)
        self.assertEqual(parsed["orientation"], "left_to_right")
        for zone in parsed["zones"]:
            self.assertGreaterEqual(len(zone["points"]), 3)
            for point in zone["points"]:
                self.assertGreaterEqual(point["x"], 0)
                self.assertLessEqual(point["x"], 1200)
                self.assertGreaterEqual(point["y"], 0)
                self.assertLessEqual(point["y"], 400)

    def test_real_client_zone_plan_extracts_marked_chainages(self):
        if not SAMPLE_ZONE_PLAN_PATH.exists():
            self.skipTest("Real client zone plan is not available on this machine")

        parsed = parse_zone_plan_pdf(
            SAMPLE_ZONE_PLAN_PATH.read_bytes(),
            filename=SAMPLE_ZONE_PLAN_PATH.name,
            floorplan_bounds={"width": 3500, "height": 2473},
        )

        self.assertEqual(
            parsed["summary"]["zone_names"], [f"Zone {code}" for code in "ABCDEF"]
        )
        self.assertEqual(parsed["summary"]["chainage_start_m"], 0)
        self.assertEqual(parsed["summary"]["chainage_end_m"], 3100)
        self.assertEqual(
            [zone["start_chainage_m"] for zone in parsed["zones"]],
            [0, 500, 1000, 1500, 2000, 2500],
        )

    def test_vector_pdf_uses_detected_numeric_zone_count_and_names(self):
        from fpdf import FPDF

        document = FPDF(orientation="L", unit="mm", format="A4")
        document.add_page()
        boundary_x = [20, 85, 150, 215, 280]
        document.set_font("Arial", size=12)
        for index, x in enumerate(boundary_x):
            document.set_xy(x, 70)
            document.cell(20, 8, f"{index}+000")
        document.set_font("Arial", size=22)
        for index in range(4):
            document.set_xy((boundary_x[index] + boundary_x[index + 1]) / 2, 95)
            document.cell(30, 10, f"Zone {index + 1}")
        raw_pdf = document.output(dest="S")
        if isinstance(raw_pdf, str):
            raw_pdf = raw_pdf.encode("latin-1")

        parsed = parse_zone_plan_pdf(bytes(raw_pdf), filename="numeric-zones.pdf")

        self.assertEqual(parsed["summary"]["zone_count"], 4)
        self.assertEqual(
            parsed["summary"]["zone_names"],
            ["Zone 1", "Zone 2", "Zone 3", "Zone 4"],
        )
        self.assertEqual(parsed["summary"]["extraction_method"], "vector_text")
        self.assertFalse(parsed["summary"]["ocr_used"])

    def test_scanned_pdf_uses_backend_ocr_zone_labels(self):
        from fpdf import FPDF

        document = FPDF(orientation="L", unit="pt", format="A4")
        document.add_page()
        raw_pdf = document.output(dest="S")
        if isinstance(raw_pdf, str):
            raw_pdf = raw_pdf.encode("latin-1")

        def token(text, x, y, *, value=None, order=0, line_id="labels"):
            box = (x - 14, y - 14, x + 14, y + 14)
            return zone_parser._LocatedText(
                text=text,
                center=(x, y),
                box=box,
                size=28,
                value=value,
                source="ocr",
                order=order,
                line_id=line_id,
            )

        ocr_tokens = [
            token(code, 150 + index * 180, 280, order=index)
            for index, code in enumerate("ABCD")
        ] + [
            token(
                chainage, 60 + index * 180, 360, order=10 + index, line_id=f"c{index}"
            )
            for index, chainage in enumerate(
                ["0+000", "0+500", "1+000", "1+500", "2+000"]
            )
        ]

        with patch.object(zone_parser, "_ocr_page_tokens", return_value=ocr_tokens):
            parsed = parse_zone_plan_pdf(bytes(raw_pdf), filename="scanned-zones.pdf")

        self.assertEqual(
            parsed["summary"]["zone_names"], [f"Zone {code}" for code in "ABCD"]
        )
        self.assertEqual(parsed["summary"]["extraction_method"], "ocr")
        self.assertTrue(parsed["summary"]["ocr_used"])


class ZonePlanConfirmationTests(unittest.TestCase):
    def test_activity_scope_separates_project_wide_from_zone_review(self):
        baselines = Mock()
        baselines.find_one.return_value = {"baseline_id": "baseline-1"}
        activities = Mock()
        activities.count_documents.side_effect = [398, 41, 51, 51, 50, 50, 51, 0]

        with (
            patch.object(baseline_service, "schedule_baselines_collection", baselines),
            patch.object(
                baseline_service, "schedule_activities_collection", activities
            ),
        ):
            result = baseline_service._schedule_zone_activity_mapping(
                project_id="project-1",
                zones=[{"name": f"Zone {code}"} for code in "ABCDEF"],
            )

        self.assertEqual(result["matched_activity_count"], 294)
        self.assertEqual(result["project_wide_activity_count"], 104)
        self.assertEqual(result["zone_required_activity_count"], 0)
        self.assertEqual(result["unmapped_activity_count"], 104)

    def test_manual_polygon_change_creates_a_reviewable_revision(self):
        floorplans = Mock()
        floorplans.update_many.return_value = Mock(matched_count=1)
        with (
            patch.object(
                baseline_service,
                "resolve_project",
                return_value={
                    "document": {
                        "schedule_zone_plan": {
                            "zone_plan_id": "zoneplan-3",
                            "version": 3,
                        }
                    },
                    "project_id": "project-1",
                    "site_name": "Demo",
                    "floorplan_id": "floorplan-1",
                },
            ),
            patch.object(baseline_service, "floorplans_collection", floorplans),
        ):
            result = baseline_service.update_schedule_zones(
                project_ref="project-1",
                zones=[
                    {
                        "name": "Zone A",
                        "points": [
                            {"x": 0, "y": 0},
                            {"x": 100, "y": 0},
                            {"x": 100, "y": 100},
                        ],
                    }
                ],
            )

        self.assertEqual(result["version"], 4)
        self.assertEqual(result["confirmation_status"], "needs_review")
        update = floorplans.update_many.call_args.args[1]["$set"]
        self.assertEqual(update["proposed_schedule_zone_plan"]["version"], 4)
        self.assertEqual(
            update["proposed_schedule_zone_plan"]["parent_zone_plan_id"],
            "zoneplan-3",
        )

    def test_unconfirmed_zone_plan_does_not_assign_tour_nodes(self):
        node = {"x": 20, "y": 20}
        floorplan = {
            "schedule_zones": [
                {
                    "name": "Zone A",
                    "points": [
                        {"x": 0, "y": 0},
                        {"x": 100, "y": 0},
                        {"x": 100, "y": 100},
                        {"x": 0, "y": 100},
                    ],
                }
            ],
            "schedule_zone_plan": {"confirmation_status": "needs_review"},
        }

        self.assertEqual(evidence_service._node_zone(node, floorplan), "")
        floorplan["schedule_zone_plan"]["confirmation_status"] = "confirmed"
        self.assertEqual(evidence_service._node_zone(node, floorplan), "Zone A")

    def test_confirmation_marks_current_zone_plan_as_reviewed(self):
        project = {
            "id": "floorplan-1",
            "project_id": "project-1",
            "site_name": "Demo",
            "imageUrl": "/sites/floorplan-1/floorplan/layout.png",
            "bounds": {"width": 100, "height": 100},
            "schedule_zones": [
                {
                    "name": "Zone A",
                    "points": [
                        {"x": 0, "y": 0},
                        {"x": 100, "y": 0},
                        {"x": 100, "y": 100},
                    ],
                }
            ],
            "schedule_zone_plan": {
                "zone_plan_id": "zoneplan-1",
                "version": 1,
                "confirmation_status": "needs_review",
            },
        }
        floorplans = Mock()
        floorplans.update_many.return_value = Mock(matched_count=1)
        baselines = Mock()
        baselines.find_one.return_value = {
            "baseline_id": "baseline-1",
            "project_id": "project-1",
        }
        activities = Mock()
        activities.count_documents.side_effect = [1, 1, 0]

        with (
            patch.object(
                baseline_service,
                "resolve_project",
                return_value={
                    "document": project,
                    "project_id": "project-1",
                    "site_name": "Demo",
                    "floorplan_id": "floorplan-1",
                },
            ),
            patch.object(baseline_service, "floorplans_collection", floorplans),
            patch.object(baseline_service, "schedule_baselines_collection", baselines),
            patch.object(
                baseline_service, "schedule_activities_collection", activities
            ),
            patch.object(
                baseline_service, "_floorplan_asset_available", return_value=True
            ),
        ):
            result = baseline_service.confirm_schedule_zone_plan(
                project_ref="project-1",
                reviewer_user_id="admin-1",
                reviewer_email="admin@example.com",
                expected_zone_plan_id="zoneplan-1",
                expected_version=1,
                floorplan_loaded=True,
                note="Overlay checked",
            )

        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["zone_plan"]["confirmation_status"], "confirmed")
        self.assertEqual(result["zone_plan"]["confirmation_note"], "Overlay checked")
        confirmation_filter = floorplans.update_many.call_args.args[0]
        self.assertIn(
            {"schedule_zone_plan.zone_plan_id": "zoneplan-1"},
            confirmation_filter["$and"],
        )
        update = floorplans.update_many.call_args.args[1]["$set"]
        self.assertEqual(
            update["schedule_zone_plan"]["confirmed_by_user_id"], "admin-1"
        )

    def test_confirmation_requires_a_loaded_floorplan_preview(self):
        project = {
            "imageUrl": "/sites/floorplan-1/floorplan/layout.png",
            "bounds": {"width": 100, "height": 100},
            "schedule_zones": [{"name": "Zone A"}],
            "schedule_zone_plan": {
                "zone_plan_id": "zoneplan-1",
                "version": 1,
                "confirmation_status": "needs_review",
            },
        }
        with patch.object(
            baseline_service,
            "resolve_project",
            return_value={
                "document": project,
                "project_id": "project-1",
                "site_name": "Demo",
                "floorplan_id": "floorplan-1",
            },
        ), patch.object(
            baseline_service, "_floorplan_asset_available", return_value=True
        ):
            with self.assertRaises(HTTPException) as raised:
                baseline_service.confirm_schedule_zone_plan(
                    project_ref="project-1",
                    reviewer_user_id="admin-1",
                    reviewer_email="admin@example.com",
                    expected_zone_plan_id="zoneplan-1",
                    expected_version=1,
                    floorplan_loaded=False,
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("visually review", raised.exception.detail)

    def test_proposed_revision_replaces_active_zones_only_after_confirmation(self):
        project = {
            "id": "floorplan-1",
            "project_id": "project-1",
            "site_name": "Demo",
            "imageUrl": "/sites/floorplan-1/floorplan/layout.png",
            "bounds": {"width": 100, "height": 100},
            "schedule_zones": [{"name": "Old Zone"}],
            "schedule_zone_plan": {
                "zone_plan_id": "zoneplan-1",
                "version": 1,
                "confirmation_status": "confirmed",
            },
            "proposed_schedule_zones": [{"name": "New Zone"}],
            "proposed_schedule_zone_plan": {
                "zone_plan_id": "zoneplan-2",
                "version": 2,
                "confirmation_status": "needs_review",
            },
        }
        floorplans = Mock()
        floorplans.update_many.return_value = Mock(matched_count=1)
        baselines = Mock()
        baselines.find_one.return_value = None
        with (
            patch.object(
                baseline_service,
                "resolve_project",
                return_value={
                    "document": project,
                    "project_id": "project-1",
                    "site_name": "Demo",
                    "floorplan_id": "floorplan-1",
                },
            ),
            patch.object(baseline_service, "floorplans_collection", floorplans),
            patch.object(baseline_service, "schedule_baselines_collection", baselines),
            patch.object(
                baseline_service, "_floorplan_asset_available", return_value=True
            ),
        ):
            result = baseline_service.confirm_schedule_zone_plan(
                project_ref="project-1",
                reviewer_user_id="admin-1",
                reviewer_email="admin@example.com",
                expected_zone_plan_id="zoneplan-2",
                expected_version=2,
                floorplan_loaded=True,
            )

        self.assertEqual(result["zones"], [{"name": "New Zone"}])
        update = floorplans.update_many.call_args.args[1]
        self.assertEqual(update["$set"]["schedule_zones"], [{"name": "New Zone"}])
        self.assertIn("proposed_schedule_zones", update["$unset"])

    def test_three_point_alignment_transforms_proposed_polygons(self):
        project = {
            "bounds": {"width": 100, "height": 100},
            "proposed_schedule_zones": [
                {
                    "name": "Zone A",
                    "points": [
                        {"x": 0, "y": 0},
                        {"x": 50, "y": 0},
                        {"x": 50, "y": 100},
                    ],
                    "source_points": [
                        {"x": 0, "y": 0},
                        {"x": 50, "y": 0},
                        {"x": 50, "y": 100},
                    ],
                }
            ],
            "proposed_schedule_zone_plan": {
                "zone_plan_id": "zoneplan-1",
                "version": 1,
                "confirmation_status": "needs_review",
            },
        }
        floorplans = Mock()
        floorplans.update_many.return_value = Mock(matched_count=1)
        with (
            patch.object(
                baseline_service,
                "resolve_project",
                return_value={
                    "document": project,
                    "project_id": "project-1",
                    "site_name": "Demo",
                    "floorplan_id": "floorplan-1",
                },
            ),
            patch.object(baseline_service, "floorplans_collection", floorplans),
        ):
            result = baseline_service.align_schedule_zones(
                project_ref="project-1",
                expected_zone_plan_id="zoneplan-1",
                expected_version=1,
                source_points=[
                    {"x": 0, "y": 0},
                    {"x": 1, "y": 0},
                    {"x": 0, "y": 1},
                ],
                floorplan_points=[
                    {"x": 10, "y": 20},
                    {"x": 90, "y": 20},
                    {"x": 10, "y": 80},
                ],
            )

        first_point = result["zones"][0]["points"][0]
        self.assertAlmostEqual(first_point["x"], 10)
        self.assertAlmostEqual(first_point["y"], 20)
        self.assertEqual(result["alignment"]["method"], "three_point_affine")

    def test_confirmation_rejects_a_stale_review(self):
        with patch.object(
            baseline_service,
            "resolve_project",
            return_value={
                "document": {
                    "schedule_zones": [{"name": "Zone A"}],
                    "schedule_zone_plan": {
                        "zone_plan_id": "zoneplan-2",
                        "version": 2,
                    },
                },
                "project_id": "project-1",
                "site_name": "Demo",
                "floorplan_id": "floorplan-1",
            },
        ):
            with self.assertRaisesRegex(HTTPException, "changed during review"):
                baseline_service.confirm_schedule_zone_plan(
                    project_ref="project-1",
                    reviewer_user_id="admin-1",
                    reviewer_email="admin@example.com",
                    expected_zone_plan_id="zoneplan-1",
                    expected_version=1,
                )


class ScheduleAnalyticsTests(unittest.TestCase):
    def test_planned_progress_uses_observation_date(self):
        activity = {
            "start_date": "2026-08-03",
            "end_date": "2026-08-14",
            "task_type": "TT_Task",
        }
        self.assertEqual(_planned_percent(activity, date(2026, 8, 2)), 0)
        self.assertEqual(_planned_percent(activity, date(2026, 8, 14)), 100)
        self.assertGreater(_planned_percent(activity, date(2026, 8, 7)), 0)
        self.assertLess(_planned_percent(activity, date(2026, 8, 7)), 100)


class _EvidenceCursor(list):
    def sort(self, *args, **kwargs):
        return self


class EvidenceHistoryTests(unittest.TestCase):
    def test_manual_review_preserves_source_and_adds_audit_fields(self):
        evidence_collection = Mock()
        evidence_collection.find_one.side_effect = [
            {
                "evidence_id": "evidence-1",
                "baseline_id": "baseline-1",
                "project_id": "project-1",
                "activity_internal_id": "activity-1",
                "captured_at": "2026-08-13T06:31:00Z",
                "review_source": "manual",
            },
            {"evidence_id": "evidence-1", "status": "approved"},
        ]
        activities = Mock()
        activities.find_one.return_value = {
            "planned_quantity": 500,
            "quantity_unit": "m2",
        }

        with (
            patch.object(
                evidence_service,
                "schedule_evidence_collection",
                evidence_collection,
            ),
            patch.object(
                evidence_service,
                "schedule_activities_collection",
                activities,
            ),
            patch.object(
                evidence_service,
                "_latest_approved_percent",
                return_value=25,
            ),
            patch.object(
                evidence_service,
                "build_baseline_comparison",
                return_value=None,
            ),
        ):
            evidence_service.review_schedule_evidence(
                evidence_id="evidence-1",
                decision="approved",
                approved_percent=40,
                verified_quantity=200,
                review_note="Measured on site",
                reviewer_user_id="admin-1",
                reviewer_email="admin@example.com",
            )

        stored = evidence_collection.update_one.call_args.args[1]["$set"]
        self.assertEqual(stored["review_source"], "manual")
        self.assertEqual(stored["previous_approved_percent"], 25)
        self.assertEqual(stored["approved_percent"], 40)
        self.assertEqual(stored["verified_quantity"], 200)
        self.assertEqual(stored["quantity_unit"], "m2")
        self.assertEqual(stored["reviewed_by_email"], "admin@example.com")

    def test_activity_evidence_response_includes_audit_metadata(self):
        evidence_collection = Mock()
        evidence_collection.find.return_value = _EvidenceCursor(
            [
                {
                    "evidence_id": "evidence-1",
                    "baseline_id": "baseline-1",
                    "activity_internal_id": "activity-1",
                    "tour_id": "manual:2026-08-13",
                    "tour_name": "Manual verified update",
                    "captured_at": "2026-08-13T06:31:00Z",
                    "status": "approved",
                    "approved_percent": 100,
                    "previous_approved_percent": 40,
                    "verified_quantity": 500,
                    "quantity_unit": "m2",
                    "review_source": "manual",
                    "reviewed_at": "2026-08-13T06:32:00Z",
                    "reviewed_by_email": "admin@example.com",
                    "review_note": "Completed",
                    "rationale": "Verified by a project administrator",
                }
            ]
        )

        with patch.object(
            analytics_service,
            "schedule_evidence_collection",
            evidence_collection,
        ):
            _, evidence_by_activity, _ = analytics_service._evidence_by_activity(
                baseline_id="baseline-1",
                as_of=date(2026, 8, 13),
                timezone_name="Asia/Riyadh",
            )

        public_item = evidence_by_activity["activity-1"][0]
        self.assertEqual(public_item["review_source"], "manual")
        self.assertEqual(public_item["previous_approved_percent"], 40)
        self.assertEqual(public_item["verified_quantity"], 500)
        self.assertEqual(public_item["quantity_unit"], "m2")
        self.assertEqual(public_item["reviewed_by_email"], "admin@example.com")
        self.assertEqual(public_item["review_note"], "Completed")


if __name__ == "__main__":
    unittest.main()
