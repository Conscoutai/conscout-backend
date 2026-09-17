from __future__ import annotations

import copy
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

from fastapi import HTTPException
from services.progress.work_schedule import update_service as service
from services.progress.work_schedule.xer_parser import parse_xer


def xer(data_date="2024-10-27", complete=False):
    fields = [
        "task_id",
        "proj_id",
        "task_code",
        "task_name",
        "status_code",
        "complete_pct_type",
        "phys_complete_pct",
        "target_drtn_hr_cnt",
        "remain_drtn_hr_cnt",
        "target_start_date",
        "target_end_date",
    ]
    lines = [
        "ERMHDR\t20.12\t2024-10-27",
        "%T\tPROJECT",
        "%F\tproj_id\tproj_short_name\tlast_recalc_date\tplan_start_date\tscd_end_date",
        f"%R\t1\tFozan\t{data_date} 00:00\t2024-02-04 00:00\t2025-05-17 00:00",
        "%T\tTASK",
        "%F\t" + "\t".join(fields),
        "%R\t100\t1\tA\tPaving\tTK_Complete\tCP_Drtn\t0\t100\t0\t2024-02-04\t2024-12-05",
        f"%R\t200\t1\tB\tPlanting\t{'TK_Complete' if complete else 'TK_Active'}\tCP_Drtn\t0\t100\t60\t2024-02-04\t2024-12-05",
        "%E",
    ]
    return "\n".join(lines).encode()


BASELINE = {
    "baseline_id": "baseline-1",
    "source_type": "xer",
    "project": {"name": "Fozan"},
    "summary": {"relationship_count": 0},
}
ACTIVITIES = [
    {
        "activity_id": "A",
        "activity_internal_id": "old-1",
        "activity_name": "Paving",
        "target_cost": 100,
        "target_duration_hours": 100,
        "zone": "Zone A",
        "mapping_status": "approved",
    },
    {
        "activity_id": "B",
        "activity_internal_id": "old-2",
        "activity_name": "Planting",
        "target_cost": 300,
        "target_duration_hours": 100,
        "zone": "Zone B",
        "mapping_status": "approved",
    },
]


class Cursor(list):
    def sort(self, keys, direction=None):
        pairs = [(keys, direction)] if isinstance(keys, str) else keys
        for key, order in reversed(pairs):
            super().sort(key=lambda row: row.get(key, ""), reverse=order == -1)
        return self


class MemoryUpdates:
    """Small storage double; stores copies so accidental mutation is visible."""

    def __init__(self):
        self.rows = []

    def find(self, query, projection=None):
        def matches(row):
            for key, value in query.items():
                if isinstance(value, dict) and "$lte" in value:
                    if row.get(key, "") > value["$lte"]:
                        return False
                elif row.get(key) != value:
                    return False
            return True

        return Cursor(copy.deepcopy([row for row in self.rows if matches(row)]))

    def find_one(self, query, sort=None):
        rows = self.find(query)
        if sort:
            rows.sort(sort)
        return rows[0] if rows else None

    def insert_one(self, row):
        self.rows.append(copy.deepcopy(row))

    def update_one(self, query, change):
        for row in self.rows:
            if all(row.get(key) == value for key, value in query.items()):
                row.update(copy.deepcopy(change["$set"]))
                break


class ScheduleUpdateTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryUpdates()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original = copy.deepcopy(ACTIVITIES)
        self.activity_store = Mock()
        self.activity_store.find.return_value = self.original
        for name, value in [
            ("schedule_updates_collection", self.store),
            ("schedule_activities_collection", self.activity_store),
            (
                "resolve_project",
                Mock(
                    return_value={
                        "project_id": "project-1",
                        "floorplan_id": "floor-1",
                        "site_name": "Fozan",
                    }
                ),
            ),
            ("active_schedule_baseline", Mock(return_value=BASELINE)),
            ("site_baseline_dir", Mock(return_value=self.temp.name)),
        ]:
            patcher = patch.object(service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def upload(self, data_date="2024-10-27", complete=False):
        return service.import_schedule_update(
            project_ref="project-1",
            filename="update.xer",
            raw_bytes=xer(data_date, complete),
        )["update"]

    def accept(self, update, acknowledge=True):
        return service.accept_schedule_update(
            project_ref="project-1",
            update_id=update["update_id"],
            acknowledge_warnings=acknowledge,
            reviewer_email="reviewer@example.com",
        )

    def test_p6_completion_types_and_status_take_precedence(self):
        self.assertEqual(
            service.reported_percent(
                {"status_code": "TK_Complete", "physical_complete_percent": 0}
            ),
            100,
        )
        self.assertEqual(
            service.reported_percent(
                {"status_code": "TK_NotStart", "physical_complete_percent": 50}
            ),
            0,
        )
        self.assertEqual(
            service.reported_percent(
                {
                    "status_code": "TK_Active",
                    "completion_type": "CP_Drtn",
                    "target_duration_hours": 100,
                    "remaining_duration_hours": 60,
                    "physical_complete_percent": 0,
                }
            ),
            40,
        )
        self.assertEqual(
            service.reported_percent(
                {
                    "status_code": "TK_Active",
                    "completion_type": "CP_Phys",
                    "physical_complete_percent": 25,
                }
            ),
            25,
        )
        self.assertEqual(
            service.reported_percent(
                {
                    "status_code": "TK_Active",
                    "completion_type": "CP_Units",
                    "actual_units": 30,
                    "remaining_units": 70,
                }
            ),
            30,
        )
        self.assertIsNone(
            service.reported_percent(
                {"status_code": "TK_Active", "completion_type": "CP_Units"}
            )
        )

        self.assertIsNone(
            service.reported_percent(
                {
                    "status_code": "TK_Active",
                    "completion_type": "CP_Phys",
                    "physical_complete_percent": float("nan"),
                }
            )
        )

    def test_windows_encoded_export_is_not_misread_as_utf16(self):
        raw = xer() + b"\n%T\tCURRENCY\n%F\tname\n%R\t\xa3"
        if len(raw) % 2:
            raw += b"\n"
        self.assertEqual(parse_xer(raw)["project"]["data_date"][:10], "2024-10-27")
        self.assertEqual(
            len(parse_xer(xer().decode().encode("utf-16"))["activities"]), 2
        )

    def test_filename_date_warning_uses_internal_reporting_date(self):
        result = service.import_schedule_update(
            project_ref="project-1", filename="Update 28-10-2024.xer", raw_bytes=xer()
        )["update"]
        self.assertEqual(result["data_date"], "2024-10-27")
        self.assertIn("filename_date_mismatch", [w["code"] for w in result["warnings"]])

    def test_http_upload_review_accept_history_and_access_control(self):
        # Load this router directly so the test does not initialize unrelated
        # budget/material/AI routers or a running application database.
        import importlib.util
        import sys
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core.auth_context import AuthenticatedUser

        spec = importlib.util.spec_from_file_location(
            "schedule_updates_route_test",
            Path(__file__).parents[1] / "api/routes/progress/work_shedule.py",
        )
        routes = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = routes
        self.addCleanup(lambda: sys.modules.pop(spec.name, None))
        spec.loader.exec_module(routes)
        app = FastAPI()
        app.include_router(routes.router)
        actor = {"role": "admin"}
        app.dependency_overrides[routes.require_authenticated_user] = (
            lambda: AuthenticatedUser(
                user_id="owner-1", email="reviewer@example.com", role=actor["role"]
            )
        )
        with TestClient(app) as client:
            response = client.post(
                "/projects/project-1/schedule-updates",
                files={"file": ("update.xer", xer())},
            )
            self.assertEqual(response.status_code, 200, response.text)
            uid = response.json()["update"]["update_id"]
            self.assertIsNone(service.latest_accepted("baseline-1"))
            accept_url = f"/projects/project-1/schedule-updates/{uid}/accept"
            response = client.post(accept_url, json={"acknowledge_warnings": True})
            self.assertEqual(response.status_code, 200, response.text)
            history = client.get("/projects/project-1/schedule-updates")
            self.assertEqual(history.json()["latest_update_id"], uid)
            self.assertNotIn("activities", history.json()["updates"][0])
            actor["role"] = "stakeholder"
            self.assertEqual(client.post(accept_url, json={}).status_code, 403)
            self.assertEqual(
                client.post(
                    "/projects/project-1/schedule-updates",
                    files={"file": ("update.xer", xer())},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.get("/projects/project-1/schedule-updates").status_code, 200
            )
            app.dependency_overrides.clear()
            self.assertIn(
                client.get("/projects/project-1/schedule-updates").status_code,
                (401, 403),
            )

    def test_update_cannot_be_accepted_through_another_project(self):
        update = self.upload()
        with patch.object(
            service, "resolve_project", return_value={"project_id": "other-project"}
        ):
            with self.assertRaises(HTTPException) as error:
                self.accept(update)
        self.assertEqual(error.exception.status_code, 404)

    def test_import_review_accept_and_reload_preserve_baseline_and_evidence(self):
        update = self.upload()
        self.assertEqual(update["data_date"], "2024-10-27")
        self.assertEqual(update["summary"]["reported_percent"], 55)
        self.assertEqual(update["summary"]["completed_count"], 1)
        self.assertIsNone(service.latest_accepted("baseline-1"))
        self.assertEqual(update["floorplan_id"], "floor-1")
        accepted = self.accept(update)["update"]
        self.assertEqual(accepted["accepted_by_email"], "reviewer@example.com")
        original_evidence = {
            "evidence_id": "tour-evidence",
            "tour_id": "tour-1",
            "status": "approved",
            "approved_percent": 20,
            "observed_at": "2024-10-20",
        }
        payload = {
            "activities": [
                {
                    "activity_id": "A",
                    "actual_percent": 20,
                    "evidence": [original_evidence],
                    "zone": "Zone A",
                },
                {"activity_id": "B", "actual_percent": 0, "evidence": []},
            ],
            "actual_percent": 5,
        }
        result = service.attach_reported_progress(payload, "baseline-1", "2024-11-01")
        self.assertEqual(result["activities"][0]["reported_percent"], 100)
        self.assertEqual(result["activities"][0]["actual_percent"], 20)
        self.assertEqual(result["activities"][0]["evidence"], [original_evidence])
        self.assertTrue(result["activities"][0]["progress_difference"])
        self.assertFalse(result["activities"][1]["progress_difference"])
        self.assertEqual(self.original, ACTIVITIES)
        self.activity_store.update_one.assert_not_called()
        self.activity_store.insert_many.assert_not_called()
        newer = self.upload("2024-11-24", complete=True)
        self.accept(newer)
        self.assertEqual(
            service.latest_accepted("baseline-1")["summary"]["completed_count"], 2
        )
        self.assertEqual(
            service.latest_accepted("baseline-1", "2024-11-01")["update_id"],
            update["update_id"],
        )
        self.assertIsNone(service.latest_accepted("baseline-1", "2024-10-01"))
        self.assertEqual(len(service.list_schedule_updates("project-1")["updates"]), 2)

    def test_duplicate_upload_is_idempotent_and_old_acceptance_is_rejected(self):
        newer = self.upload("2024-11-24")
        self.accept(newer)
        duplicate = self.upload("2024-11-24")
        self.assertEqual(duplicate["update_id"], newer["update_id"])
        self.assertEqual(len(list(Path(self.temp.name).rglob("*.xer"))), 1)
        older = self.upload()
        with self.assertRaises(HTTPException) as error:
            self.accept(older)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(
            service.latest_accepted("baseline-1")["update_id"], newer["update_id"]
        )
        # Even if two reviews race, reporting date determines the current snapshot.
        self.store.rows[-1].update(
            status="accepted", accepted_at=datetime.now(timezone.utc)
        )
        self.assertEqual(
            service.latest_accepted("baseline-1")["update_id"], newer["update_id"]
        )

    def test_warning_acknowledgement_and_baseline_change_are_enforced(self):
        update = self.upload()
        self.store.rows[0]["warnings"] = [{"message": "Review invalid actual date"}]
        with self.assertRaises(HTTPException):
            self.accept(update, acknowledge=False)
        with patch.object(
            service,
            "active_schedule_baseline",
            return_value={**BASELINE, "baseline_id": "new-baseline"},
        ):
            with self.assertRaises(HTTPException) as error:
                self.accept(update)
            self.assertEqual(error.exception.status_code, 409)
        self.accept(update)

    def test_added_missing_and_invalid_dates_are_reviewable_not_silent_zeroes(self):
        parsed = parse_xer(xer())
        parsed["activities"][1]["activity_id"] = "NEW"
        parsed["activities"][0]["actual_start_at"] = "2025-02-04T00:00:00"
        review = service.build_review(parsed, ACTIVITIES, BASELINE)
        self.assertEqual(review["summary"]["missing_count"], 1)
        self.assertEqual(review["summary"]["added_count"], 1)
        self.assertIsNone(review["summary"]["reported_percent"])
        self.assertEqual(review["summary"]["weight_coverage_percent"], 25)
        self.assertIn("invalid_actual_dates", [w["code"] for w in review["warnings"]])
        parsed["activities"][1]["activity_id"] = "A"
        with self.assertRaises(HTTPException):
            service.build_review(parsed, ACTIVITIES, BASELINE)

    def test_wrong_project_missing_date_and_large_upload_are_rejected(self):
        parsed = parse_xer(xer())
        with self.assertRaises(HTTPException):
            service.build_review(
                parsed, [{**ACTIVITIES[0], "activity_id": "OTHER"}], BASELINE
            )
        with self.assertRaises(HTTPException):
            self.upload("")
        with self.assertRaises(HTTPException) as error:
            service.import_schedule_update(
                project_ref="project-1",
                filename="update.xer",
                raw_bytes=b"x" * (service.MAX_UPDATE_BYTES + 1),
            )
        self.assertEqual(error.exception.status_code, 413)

    def test_actual_client_files_match_all_398_activities_and_expected_statuses(self):
        baseline_path = Path(
            r"D:\Cosysta\conscout\Sites\fozan Street\PROJECT SETUP\SH.ABDULLATIF ALFOZAN STREET RROJECT.xer"
        )
        october = Path(r"C:\Users\safwa\Downloads\Update 28-10-2024.xer")
        november = Path(r"C:\Users\safwa\Downloads\Update24-11-2024.xer")
        if not all(p.exists() for p in (baseline_path, october, november)):
            self.skipTest("Private client XER fixtures are not available")
        original = parse_xer(baseline_path.read_bytes())
        for path, counts, data_date in [
            (october, (255, 56, 87), "2024-10-27"),
            (november, (280, 49, 69), "2024-11-24"),
        ]:
            review = service.build_review(
                parse_xer(path.read_bytes()), original["activities"], original
            )
            self.assertEqual(review["data_date"], data_date)
            self.assertEqual(review["summary"]["matched_count"], 398)
            self.assertEqual(
                tuple(
                    review["summary"][key]
                    for key in [
                        "completed_count",
                        "in_progress_count",
                        "not_started_count",
                    ]
                ),
                counts,
            )
            self.assertIsNotNone(review["summary"]["reported_percent"])
            if path == november:
                self.assertTrue(
                    any(
                        w.get("activity_ids") == ["FAW.CONS.2116"]
                        and w["code"] == "invalid_actual_dates"
                        for w in review["warnings"]
                    )
                )


if __name__ == "__main__":
    unittest.main()
