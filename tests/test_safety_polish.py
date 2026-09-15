from unittest.mock import MagicMock, patch
from fastapi import HTTPException
from services.safety import safety_service as safety

CONTEXT = {"project_id": "project1", "site_name": "Example", "floorplan_id": "floor1", "document": {"owner_user_id": "owner1"}}


def dashboard_with(data, tour_id="tour1", day="2026-09-09"):
    with patch.object(safety, "project_context", return_value=CONTEXT), \
         patch.object(safety, "list_records", side_effect=lambda project, kind, **kwargs: data.get(kind, [])), \
         patch.object(safety, "list_analysis_jobs", return_value=[]), \
         patch.object(safety, "get_weather", return_value={"work_state": "safe", "reasons": []}), \
         patch.object(safety, "_schedule_manpower_for_dates", return_value=({}, False)), \
         patch.object(safety, "_project_inspection_status_records", return_value=([], [])):
        return safety.build_dashboard("Example", record_date=day, tour_id=tour_id)


def test_dismissed_findings_leave_all_totals_but_remain_in_history():
    base = {"tour_id": "tour1", "record_date": "2026-09-09", "requires_review": True}
    records = [{**base, "record_id": "rejected", "status": "dismissed", "ppe_status": "non_compliant", "severity": "critical"},
               {**base, "record_id": "accepted", "status": "resolved", "ppe_status": "compliant", "source": "demo_scenario"},
               {**base, "record_id": "unknown", "status": "open"}]
    result = dashboard_with({"safety_finding": records})
    assert result["ppe"]["open_findings"] == 1
    assert result["ppe"]["non_compliant"] == 0
    assert result["ppe"]["unknown"] == 1
    assert result["ppe"]["compliance_percent"] == 100
    assert result["ppe"]["demo_count"] == 1
    assert result["counts"]["pending_reviews"] == 1
    assert len(result["recent"]["findings"]) == 3
    assert result["work_state"]["status"] != "stop_work"


def test_filters_keep_prior_unresolved_and_project_wide_items_but_not_future_or_other_tours():
    records = [{"record_id": "prior", "record_date": "2026-09-08", "tour_id": "tour1", "status": "open"},
               {"record_id": "future", "record_date": "2026-09-10", "tour_id": "tour1"},
               {"record_id": "other", "record_date": "2026-09-09", "tour_id": "tour2"},
               {"record_id": "shared", "record_date": "2026-09-09"}]
    result = dashboard_with({"safety_finding": records, "hazard": records, "permit": records, "safety_zone": records, "daily_report": records})
    for key in ["findings", "hazards", "permits", "zones"]:
        assert [r["record_id"] for r in result["recent"][key]] == ["prior", "shared"]
    assert [r["record_id"] for r in result["recent"]["reports"]] == ["shared"]


def test_permit_validity_and_zone_expiry_affect_active_counts():
    result = dashboard_with({"permit": [
        {"status": "active", "valid_from": "2026-09-01", "valid_until": "2026-09-08"},
        {"status": "approved", "valid_from": "2026-09-09", "valid_until": "2026-09-10"},
        {"status": "approved", "valid_from": "2026-09-10", "valid_until": "2026-09-11"}],
        "safety_zone": [{"status": "active", "valid_until": "2026-09-08"}]})
    assert result["counts"]["active_permits"] == 1
    assert result["counts"]["active_zones"] == 0


def test_finding_evidence_validates_project_and_exact_image():
    collection = MagicMock()
    collection.find_one.return_value = {"project_id": "project1", "name": "Example tour", "nodes": [{"id": "image1"}]}
    with patch.object(safety, "raw_tours_collection", collection):
        result = safety.validate_finding_evidence(CONTEXT, "safety_finding", {"tour_id": "tour1", "node_id": "image1", "ppe_status": "compliant"})
        assert result["point_index"] == 1
        assert result["tour_name"] == "Example tour"
        for payload in [{"tour_id": "tour1", "node_id": "wrong"}, {"node_id": "image1"}, {"ppe_status": "invalid"}]:
            try:
                safety.validate_finding_evidence(CONTEXT, "safety_finding", payload)
                assert False, "Invalid evidence accepted"
            except HTTPException as error:
                assert error.status_code == 400
        collection.find_one.return_value = {"project_id": "other", "site_name": "Example", "owner_user_id": "other"}
        try:
            safety.validate_finding_evidence(CONTEXT, "safety_finding", {"tour_id": "other", "node_id": "image1"})
            assert False, "Foreign project evidence accepted"
        except HTTPException as error:
            assert error.status_code == 400


def test_schedule_uses_real_period_end_and_does_not_invent_zero_for_missing_staffing():
    comparison = {"manpower": {"points": [{"date": "2026-09-07", "period_end": "2026-09-09", "planned_workers": None}]}}
    with patch.object(safety, "build_baseline_comparison", return_value=comparison):
        plans, _ = safety._schedule_manpower_for_dates("Example", ["2026-09-09", "2026-09-10"])
    assert plans["2026-09-09"]["planned_workers"] is None
    assert "2026-09-10" not in plans
