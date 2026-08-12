from __future__ import annotations

import unittest
import os
from datetime import date
from pathlib import Path

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

from services.progress.work_schedule.analytics_service import _planned_percent
from services.progress.work_schedule.pdf_parser import parse_pdf_schedule
from services.progress.work_schedule.xer_parser import (
    XerParseError,
    normalize_work_category,
    parse_xer,
)


SAMPLE_XER_PATH = Path(
    r"C:\Users\safwa\Downloads\SH.ABDULLATIF ALFOZAN STREET RROJECT.xer"
)


class XerParserTests(unittest.TestCase):
    def test_real_client_xer_imports_expected_schedule_tables(self):
        if not SAMPLE_XER_PATH.exists():
            self.skipTest("Real client XER is not available on this machine")

        parsed = parse_xer(
            SAMPLE_XER_PATH.read_bytes(), filename=SAMPLE_XER_PATH.name
        )

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
