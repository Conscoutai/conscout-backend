from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException

from core.auth_context import AuthenticatedUser

from services.safety import safety_service
from services.safety.safety_service import (
    DEFAULT_SAFETY_CONFIG,
    evaluate_weather_risk,
    point_in_polygon,
    preliminary_worker_count,
    render_daily_report_pdf,
    finalize_daily_report,
    verify_permit_for_start,
    validate_safety_config,
)
from services.safety.daily_report_scheduler import due_report_date


def test_weather_thresholds_return_safe_caution_and_stop_work():
    assert evaluate_weather_risk(
        wind_kph=12,
        heat_c=28,
        rain_mm_h=0,
        config=DEFAULT_SAFETY_CONFIG,
    )["work_state"] == "safe"
    assert evaluate_weather_risk(
        wind_kph=31,
        heat_c=28,
        rain_mm_h=0,
        config=DEFAULT_SAFETY_CONFIG,
    )["work_state"] == "caution"
    stopped = evaluate_weather_risk(
        wind_kph=46,
        heat_c=28,
        rain_mm_h=0,
        config=DEFAULT_SAFETY_CONFIG,
    )
    assert stopped["work_state"] == "stop_work"
    assert "Wind" in stopped["reasons"][0]


def test_unknown_weather_never_defaults_to_safe():
    result = evaluate_weather_risk(
        wind_kph=None,
        heat_c=None,
        rain_mm_h=None,
        config=DEFAULT_SAFETY_CONFIG,
    )
    assert result["work_state"] == "unknown"


def test_weather_cache_accepts_mongodb_naive_utc_datetime():
    now = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)
    context = {
        "project_id": "project_1",
        "site_name": "Test Project",
        "floorplan_id": "floorplan_1",
        "document": {},
    }
    cached_weather = {
        "project_id": "project_1",
        "record_type": "weather_observation",
        "provider": "cached",
        "created_at": (now - timedelta(minutes=5)).replace(tzinfo=None),
    }

    with patch(
        "services.safety.safety_service.project_context", return_value=context
    ), patch(
        "services.safety.safety_service.get_config",
        return_value={"config": DEFAULT_SAFETY_CONFIG},
    ), patch(
        "services.safety.safety_service._latest_weather",
        return_value=cached_weather,
    ), patch(
        "services.safety.safety_service.utc_now", return_value=now
    ), patch(
        "services.safety.safety_service.requests.get"
    ) as weather_request:
        result = safety_service.get_weather("project_1")

    assert result["provider"] == "cached"
    weather_request.assert_not_called()


def test_preliminary_tour_count_uses_maximum_not_sum():
    result = preliminary_worker_count(
        [
            {"worker_count": 3},
            {"worker_count": 8},
            {"worker_count": 5},
            {"worker_count": "invalid"},
        ]
    )
    assert result["observed_workers"] == 8
    assert result["sample_count"] == 3
    assert result["method"] == "max_existing_worker_count_per_tour"


def test_permit_verification_requires_active_dates_and_confirmed_controls():
    now = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)
    permit = {
        "status": "active",
        "valid_from": (now - timedelta(hours=1)).isoformat(),
        "valid_until": (now + timedelta(hours=2)).isoformat(),
        "controls": [
            {"label": "Barricade installed", "confirmed": True},
            {"label": "Fire watch assigned", "confirmed": True},
        ],
    }
    assert verify_permit_for_start(permit, at=now)["allowed"] is True

    permit["controls"][1]["confirmed"] = False
    blocked = verify_permit_for_start(permit, at=now)
    assert blocked["allowed"] is False
    assert "Fire watch assigned" in blocked["reasons"][-1]


def test_permit_verification_enforces_signed_approval_chain():
    now = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)
    permit = {
        "status": "approved",
        "valid_from": (now - timedelta(hours=1)).isoformat(),
        "valid_until": (now + timedelta(hours=1)).isoformat(),
        "controls": [{"label": "Barricade", "confirmed": True}],
        "approvals": [],
    }
    blocked = verify_permit_for_start(permit, at=now, required_approvals=1)
    assert blocked["allowed"] is False
    assert "signed approval" in blocked["reasons"][-1]

    permit["approvals"] = [
        {
            "approver_name": "Safety Manager",
            "signature": "approval-ref-123",
            "status": "approved",
        }
    ]
    assert verify_permit_for_start(
        permit, at=now, required_approvals=1
    )["allowed"] is True


def test_daily_report_cutoff_uses_project_timezone():
    config = {
        **DEFAULT_SAFETY_CONFIG,
        "auto_daily_reports": True,
        "timezone": "Asia/Kolkata",
        "daily_report_cutoff": "18:00",
    }
    before = datetime(2026, 8, 18, 11, 30, tzinfo=timezone.utc)
    after = datetime(2026, 8, 18, 12, 31, tzinfo=timezone.utc)
    assert due_report_date(config, now=before) is None
    assert due_report_date(config, now=after) == "2026-08-18"


def test_extended_safety_config_validation_accepts_phase1_controls():
    config = {
        **DEFAULT_SAFETY_CONFIG,
        "timezone": "UTC",
        "daily_report_cutoff": "19:30",
        "hazard_resolution_hours": 12,
        "required_permit_approvals": 2,
    }
    assert validate_safety_config(config) is config


def test_geofence_includes_inside_and_boundary_points():
    polygon = [
        {"x": 0, "y": 0},
        {"x": 10, "y": 0},
        {"x": 10, "y": 10},
        {"x": 0, "y": 10},
    ]
    assert point_in_polygon({"x": 5, "y": 5}, polygon) is True
    assert point_in_polygon({"x": 10, "y": 4}, polygon) is True
    assert point_in_polygon({"x": 11, "y": 5}, polygon) is False


def test_daily_report_renderer_returns_a_pdf():
    report = {
        "record_id": "report_1",
        "record_date": "2026-08-18",
        "snapshot": {
            "work_state": {"status": "caution", "reasons": ["Open hazard"]},
            "manpower": {
                "planned_workers": 20,
                "observed_workers": 18,
                "variance": -2,
            },
            "ppe": {"status": "preliminary", "open_findings": 1},
            "weather": {"wind_kph": 22, "apparent_temperature_c": 39},
            "counts": {"open_hazards": 1, "active_permits": 2},
        },
    }
    context = {
        "project_id": "project_1",
        "site_name": "Test Project",
        "floorplan_id": "project_1",
        "document": {},
    }
    with patch(
        "services.safety.safety_service.project_context", return_value=context
    ), patch.object(
        safety_service.safety_records_collection,
        "find_one",
        return_value=report,
    ):
        content, filename = render_daily_report_pdf("project_1", "report_1")

    assert content.startswith(b"%PDF")
    assert filename == "safety-manpower-2026-08-18.pdf"


def test_daily_report_must_be_reviewed_before_finalization():
    context = {
        "project_id": "project_1",
        "site_name": "Test Project",
        "floorplan_id": "floorplan_1",
        "document": {},
    }
    user = AuthenticatedUser(
        user_id="manager_1", email="manager@example.com", name="Manager"
    )
    with patch(
        "services.safety.safety_service.project_context", return_value=context
    ), patch.object(
        safety_service.safety_records_collection,
        "find_one",
        return_value={"record_id": "report_1", "status": "draft"},
    ):
        try:
            finalize_daily_report("project_1", "report_1", user=user)
            assert False, "draft report finalization should fail"
        except HTTPException as error:
            assert error.status_code == 409

    with patch(
        "services.safety.safety_service.project_context", return_value=context
    ), patch.object(
        safety_service.safety_records_collection,
        "find_one",
        return_value={"record_id": "report_1", "status": "reviewed"},
    ), patch(
        "services.safety.safety_service.update_record",
        return_value={"record_id": "report_1", "status": "finalized"},
    ):
        finalized = finalize_daily_report("project_1", "report_1", user=user)
    assert finalized["status"] == "finalized"
