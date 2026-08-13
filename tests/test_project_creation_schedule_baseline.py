from __future__ import annotations

import asyncio
import os
import unittest
from io import BytesIO
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

from api.routes.project_setup import create_project
from core.auth_context import AuthenticatedUser


class ProjectCreationScheduleBaselineTests(unittest.TestCase):
    def _create(self, *, filename: str, timezone_name: str = "Asia/Riyadh"):
        current_user = AuthenticatedUser(
            user_id="admin-1",
            email="admin@example.com",
            name="Admin",
        )
        access = create_project.ProjectCreationAccess(
            user=current_user,
            requested_project_id="project-1",
        )
        floorplan = UploadFile(file=BytesIO(b"image"), filename="floor.png")
        baseline = UploadFile(file=BytesIO(b"baseline"), filename=filename)

        with (
            patch.object(
                create_project,
                "create_floorplan",
                return_value={"floorPlan": {"id": "floorplan-1"}},
            ),
            patch.object(
                create_project,
                "save_baseline_xer",
                return_value=("/sites/project-1/baseline/schedule.pdf", filename),
            ),
            patch.object(
                create_project,
                "import_schedule_baseline",
                return_value={"status": "active"},
            ) as import_baseline,
        ):
            result = asyncio.run(
                create_project.create_project_floorplan(
                    site_name="Project One",
                    file=floorplan,
                    name="Project One",
                    pointA_px=None,
                    pointA_py=None,
                    pointA_lat=None,
                    pointA_lon=None,
                    pointB_px=None,
                    pointB_py=None,
                    pointB_lat=None,
                    pointB_lon=None,
                    calibration_points=None,
                    dxf_project_id=None,
                    location="Riyadh",
                    project_location=None,
                    area_location=None,
                    site_name_form=None,
                    dxf_zip=None,
                    site_config=None,
                    baseline_xer=baseline,
                    schedule_timezone=timezone_name,
                    zone_plan_pdf=None,
                    capture_mode="indoor",
                    currency_code="SAR",
                    currency=None,
                    creation_access=access,
                )
            )
        return result, import_baseline

    def test_pdf_baseline_is_imported_with_selected_timezone(self):
        result, import_baseline = self._create(filename="schedule.pdf")

        self.assertEqual(result["schedule_baseline_import"]["status"], "active")
        import_baseline.assert_called_once_with(
            project_ref="project-1",
            filename="schedule.pdf",
            raw_bytes=b"baseline",
            timezone_name="Asia/Riyadh",
            activate=True,
        )

    def test_legacy_csv_is_rejected_as_a_schedule_baseline(self):
        with self.assertRaises(HTTPException) as raised:
            self._create(filename="schedule.csv")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn(".xer or .pdf", str(raised.exception.detail))


if __name__ == "__main__":
    unittest.main()
