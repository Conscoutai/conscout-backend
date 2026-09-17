import copy
import os
from datetime import date
from unittest.mock import Mock, patch

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

from services.progress.work_schedule import approved_progress_service as service
from services.progress.work_schedule import analytics_service as analytics
from services.progress.work_schedule import update_service
from fastapi import HTTPException
import pytest

ACTIVITIES = [
    {
        "activity_id": "A",
        "activity_internal_id": "old-1",
        "target_cost": 1,
        "start_date": "2024-01-01",
        "end_date": "2024-12-01",
        "target_duration_hours": 100,
    },
    {
        "activity_id": "B",
        "activity_internal_id": "old-2",
        "target_cost": 3,
        "start_date": "2024-01-01",
        "end_date": "2024-12-01",
        "target_duration_hours": 100,
    },
]


def snapshot(day="2024-10-27", percentages=(100, 40), **kwargs):
    return {
        "update_id": day,
        "baseline_id": "base",
        "data_date": day,
        "source_filename": "Client.xer",
        "status": "accepted",
        "accepted_at": day + "T12:00:00Z",
        "accepted_by_email": "reviewer@example.com",
        "activities": [
            {
                "activity_id": a["activity_id"],
                "activity_internal_id": "different-p6-id",
                "reported_percent": pct,
                "reported_status": "Completed" if pct == 100 else "In progress",
                "matched": True,
                "actual_start_at": "",
                "actual_end_at": "",
                "forecast_finish_at": "",
            }
            for a, pct in zip(ACTIVITIES, percentages)
        ],
        **kwargs,
    }


def evidence(day="2024-11-01", pct=60, **kwargs):
    return {
        "evidence_id": "e1",
        "activity_internal_id": "old-2",
        "captured_at": day,
        "approved_percent": pct,
        "status": "approved",
        "review_source": "manual",
        "updated_at": day + "T13:00:00Z",
        **kwargs,
    }


def resolve(e=(), u=None, as_of=date(2024, 12, 1)):
    return service.resolve_progress(
        ACTIVITIES, list(e), [snapshot()] if u is None else u, as_of
    )


def test_accepted_schedule_counts_as_approved_matches_external_ids_and_keeps_history():
    update = snapshot()
    original = copy.deepcopy(update)
    result = resolve(u=[update])
    assert result["values"] == {"old-1": 100, "old-2": 40}
    assert (
        analytics._weighted_percent(
            ACTIVITIES, result["values"], weight_field="target_cost"
        )
        == 55
    )
    item = result["history"]["old-2"][0]
    assert item["review_source"] == "client_schedule"
    assert item["is_current_progress"] and item["approved_percent"] == 40
    assert update == original


def test_older_tour_uploaded_later_cannot_replace_newer_schedule():
    result = resolve([evidence("2024-09-01", 20, updated_at="2025-01-01T12:00:00Z")])
    assert result["values"]["old-2"] == 40
    assert len(result["history"]["old-2"]) == 2


def test_newer_approved_tour_advances_progress_and_rejected_ai_does_not():
    result = resolve(
        [
            evidence(review_source="human"),
            evidence("2024-11-02", 100, status="needs_review"),
        ]
    )
    assert result["values"]["old-2"] == 60
    assert result["selected"]["old-2"]["review_source"] == "human"
    assert result["timeline"]["2024-10-27"]["old-2"] == 40
    assert result["timeline"]["2024-11-01"]["old-2"] == 60


@pytest.mark.parametrize("day,pct", [("2024-10-27", 60), ("2024-11-01", 20)])
def test_same_date_disagreement_and_regression_wait_for_review(day, pct):
    result = resolve([evidence(day, pct)])
    assert result["values"]["old-2"] == 40
    item = next(i for i in result["history"]["old-2"] if i["evidence_id"] == "e1")
    assert item["status"] == "needs_review" and item["progress_conflict"]
    assert item["suggested_percent"] == pct and item["approved_percent"] is None


def test_explicit_conflict_resolution_allows_correction():
    result = resolve([evidence(pct=20, progress_conflict_confirmed=True)])
    assert result["values"]["old-2"] == 20


def test_schedule_conflict_can_be_approved_or_rejected_without_mutating_xer():
    for decision, expected in [("approved", 20), ("rejected", 40)]:
        later = snapshot(
            "2024-11-24",
            (100, 20),
            progress_reviews=[
                {
                    "activity_id": "B",
                    "decision": decision,
                    "reviewed_at": "2024-11-25T12:00:00Z",
                }
            ],
        )
        result = resolve(u=[snapshot(), later])
        assert result["values"]["old-2"] == expected
        assert len(result["history"]["old-2"]) == 2


def test_future_and_pending_snapshots_excluded_unknown_does_not_erase_known():
    result = resolve(
        [evidence("2025-01-01", 90)],
        [snapshot(), snapshot("2024-11-24", (100, None)), snapshot(status="pending")],
    )
    assert result["values"]["old-2"] == 40
    assert result["history"]["old-2"][0]["status"] == "needs_review"


def test_project_timezone_used_and_owner_fields_never_exposed():
    result = service.resolve_progress(
        ACTIVITIES,
        [
            evidence(
                "2024-10-27T22:30:00Z",
                60,
                owner_email="private",
                owner_user_id="secret",
            )
        ],
        [snapshot()],
        date(2024, 10, 28),
        "Asia/Riyadh",
    )
    assert result["selected"]["old-2"]["observed_at"] == "2024-10-28"
    assert "owner_email" not in result["selected"]["old-2"]


class Cursor(list):
    def sort(self, *args):
        return self


def test_dashboard_activity_and_curve_use_identical_weighted_progress():
    baseline = {
        "baseline_id": "base",
        "project_id": "project",
        "summary": {"target_cost": 4},
        "project": {"planned_start_at": "2024-01-01", "planned_end_at": "2024-12-01"},
    }
    with patch.object(
        analytics,
        "resolve_project",
        return_value={"project_id": "project", "site_name": "Fozan"},
    ), patch.object(
        analytics,
        "schedule_baselines_collection",
        Mock(find_one=Mock(return_value=baseline)),
    ), patch.object(
        analytics,
        "schedule_activities_collection",
        Mock(find=Mock(return_value=Cursor(ACTIVITIES))),
    ), patch.object(
        analytics,
        "schedule_evidence_collection",
        Mock(find=Mock(return_value=Cursor([]))),
    ), patch.object(
        analytics, "schedule_relationships_collection", Mock(find=Mock(return_value=[]))
    ), patch.object(
        analytics, "schedule_assignments_collection", Mock(find=Mock(return_value=[]))
    ), patch.object(
        service, "schedule_evidence_collection", Mock(find=Mock(return_value=[]))
    ), patch.object(
        service,
        "schedule_updates_collection",
        Mock(find=Mock(return_value=[snapshot()])),
    ), patch.object(
        update_service, "latest_accepted", return_value=snapshot()
    ):
        result = analytics.build_baseline_comparison("project", as_of=date(2024, 11, 1))
    assert result["actual_percent"] == result["summary"]["actual_percent"] == 55
    assert result["curves"]["actual"][-1]["percent"] == 55
    assert result["activities"][1]["actual_percent"] == 40
    assert result["activities"][1]["progress_source"] == "client_schedule"
    assert result["summary"]["progress_as_of"] == "2024-10-27"


def test_review_unknown_or_other_owner_snapshot_is_not_found():
    with patch.object(
        service, "schedule_updates_collection", Mock(find_one=Mock(return_value=None))
    ):
        with pytest.raises(HTTPException) as error:
            service.review_schedule_observation(
                "scheduleupdate:other:A", "approved", 100, "", "reviewer"
            )
        assert error.value.status_code == 404


def test_older_higher_observation_cannot_turn_newer_approval_into_conflict():
    result = resolve([evidence("2024-09-01", 90, updated_at="2025-01-01T12:00:00Z")])
    assert result["values"]["old-2"] == 40
    assert result["timeline"]["2024-10-27"]["old-2"] == 40
    assert not any(i.get("progress_conflict") for i in result["history"]["old-2"])


def test_review_persists_audited_decision_without_replacing_snapshot():
    from core import database

    updates = Mock(find_one=Mock(return_value=snapshot()))
    with patch.object(service, "schedule_updates_collection", updates), patch.object(
        database,
        "schedule_activities_collection",
        Mock(find_one=Mock(return_value=ACTIVITIES[1])),
    ):
        response = service.review_schedule_observation(
            "scheduleupdate:2024-10-27:B",
            "approved",
            40,
            "Checked with client",
            "admin@example.com",
        )
        assert response["status"] == "approved"
        change = updates.update_one.call_args.args[1]
        assert list(change) == ["$push"]
        assert (
            change["$push"]["progress_reviews"]["reviewed_by_email"]
            == "admin@example.com"
        )
        with pytest.raises(HTTPException) as error:
            service.review_schedule_observation(
                "scheduleupdate:2024-10-27:B", "approved", 80, "", "admin"
            )
        assert error.value.status_code == 422


def test_real_fozan_files_resolve_expected_approved_percentages():
    from pathlib import Path
    from services.progress.work_schedule.xer_parser import parse_xer

    baseline_file = Path(
        r"D:\Cosysta\conscout\Sites\fozan Street\PROJECT SETUP\xer\SH.ABDULLATIF ALFOZAN STREET RROJECT.xer"
    )
    files = [
        Path(r"C:\Users\safwa\Downloads\Update 28-10-2024.xer"),
        Path(r"C:\Users\safwa\Downloads\Update24-11-2024.xer"),
    ]
    if not baseline_file.exists() or not all(p.exists() for p in files):
        pytest.skip("Private Fozan source files are unavailable")
    original = parse_xer(baseline_file.read_bytes(), filename=baseline_file.name)
    baseline = {
        "baseline_id": "real",
        "project": original["project"],
        "summary": original["summary"],
    }
    updates = []
    for file, expected in zip(files, [68.972, 79.261]):
        parsed = parse_xer(file.read_bytes(), filename=file.name)
        review = update_service.build_review(parsed, original["activities"], baseline)
        update = {
            **review,
            "status": "accepted",
            "update_id": file.stem,
            "source_filename": file.name,
            "accepted_at": review["data_date"] + "T12:00:00Z",
        }
        updates.append(update)
        resolved = service.resolve_progress(
            original["activities"], [], updates, date(2025, 1, 1)
        )
        assert len(resolved["values"]) == 398
        assert (
            analytics._weighted_percent(
                original["activities"], resolved["values"], weight_field="target_cost"
            )
            == expected
        )
