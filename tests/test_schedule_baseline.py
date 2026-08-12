from __future__ import annotations

import unittest
import os
from datetime import date
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
