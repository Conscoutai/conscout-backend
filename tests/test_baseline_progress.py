import copy
import os
from datetime import date
from unittest.mock import Mock, patch

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")
from services.progress.work_schedule import baseline_progress_service as carry
from services.progress.work_schedule import approved_progress_service as progress
from services.progress.work_schedule import baseline_service as baseline_service


class Store:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)

    def find(self, query, projection=None):
        return copy.deepcopy(
            [r for r in self.rows if all(r.get(k) == v for k, v in query.items())]
        )

    def find_one(self, query, **kwargs):
        return next(iter(self.find(query)), None)

    def update_one(self, query, update):
        for r in self.rows:
            if all(r.get(k) == v for k, v in query.items()):
                r.update(copy.deepcopy(update["$set"]))

    def update_many(self, query, update):
        self.update_one(query, update)


OLD = [
    {
        "activity_id": "A",
        "activity_internal_id": "old1",
        "activity_name": "Paving",
        "task_type": "TT_Task",
        "baseline_id": "v1",
    },
    {
        "activity_id": "B",
        "activity_internal_id": "old2",
        "activity_name": "Planting",
        "task_type": "TT_Task",
        "baseline_id": "v1",
    },
]
NEW = [
    {
        "activity_id": "A",
        "activity_internal_id": "new9",
        "activity_name": "Paving renamed",
        "task_type": "TT_Task",
        "baseline_id": "v2",
    },
    {
        "activity_id": "B",
        "activity_internal_id": "new8",
        "activity_name": "Planting",
        "task_type": "TT_Task",
        "baseline_id": "v2",
    },
    {
        "activity_id": "C",
        "activity_internal_id": "old1",
        "activity_name": "New work",
        "task_type": "TT_Task",
        "baseline_id": "v2",
    },
]
V1 = {
    "baseline_id": "v1",
    "project_id": "project",
    "source_type": "xer",
    "is_active": True,
}
V2 = {
    "baseline_id": "v2",
    "project_id": "project",
    "source_type": "xer",
    "progress_parent": carry.carry_plan(V1, OLD, NEW),
}
UPDATE = {
    "baseline_id": "v1",
    "update_id": "xer1",
    "source_filename": "Original.xer",
    "status": "accepted",
    "data_date": "2024-11-24",
    "activities": [
        {"activity_id": "A", "matched": True, "reported_percent": 100},
        {"activity_id": "B", "matched": True, "reported_percent": 40},
    ],
}
MANUAL = {
    "baseline_id": "v1",
    "evidence_id": "manual",
    "activity_internal_id": "old1",
    "captured_at": "2025-01-01",
    "review_source": "manual",
    "status": "approved",
    "approved_percent": 76,
}


def test_matching_preserves_only_unique_compatible_ids_and_flags_renames():
    plan = carry.carry_plan(V1, OLD, NEW)
    assert plan["activity_ids"] == ["A", "B"] and plan["new_or_changed_count"] == 1
    assert plan["renamed_activity_ids"] == ["A"]
    changed = copy.deepcopy(NEW)
    changed[0]["task_type"] = "TT_Mile"
    assert carry.carry_plan(V1, OLD, changed)["activity_ids"] == ["B"]
    assert carry.carry_plan(V1, OLD, NEW + [NEW[0]])["activity_ids"] == ["B"]


def test_revision_carries_xer_manual_history_conflicts_and_never_reuses_internal_ids():
    baselines = Store([V1, V2])
    activities = Store(OLD + NEW)
    updates = Store([UPDATE])
    evidence = Store([MANUAL])
    original = copy.deepcopy(
        [baselines.rows, activities.rows, updates.rows, evidence.rows]
    )
    with patch.object(carry, "schedule_baselines_collection", baselines), patch.object(
        carry, "schedule_activities_collection", activities
    ), patch.object(progress, "schedule_updates_collection", updates), patch.object(
        progress, "schedule_evidence_collection", evidence
    ):
        r = progress.load_progress("v2", NEW, date(2026, 1, 1), "UTC", baseline=V2)
        assert r["values"] == {"new9": 100, "new8": 40}
        assert "old1" not in r["values"]
        assert {e["evidence_id"] for e in r["history"]["new9"]} == {
            "manual",
            "scheduleupdate:xer1:A",
        }
        assert (
            next(e for e in r["history"]["new9"] if e["evidence_id"] == "manual")[
                "status"
            ]
            == "needs_review"
        )
        assert r["timeline"]["2024-11-24"] == {"new9": 100, "new8": 40}
        again = progress.load_progress("v2", NEW, date(2026, 1, 1), "UTC", baseline=V2)
        assert again == r
    assert [baselines.rows, activities.rows, updates.rows, evidence.rows] == original


def test_new_revision_observation_can_advance_inherited_progress():
    local = {
        "baseline_id": "v2",
        "evidence_id": "new-manual",
        "activity_internal_id": "new8",
        "captured_at": "2025-02-01",
        "review_source": "manual",
        "status": "approved",
        "approved_percent": 60,
    }
    with patch.object(
        carry, "schedule_baselines_collection", Store([V1, V2])
    ), patch.object(
        carry, "schedule_activities_collection", Store(OLD + NEW)
    ), patch.object(
        progress, "schedule_updates_collection", Store([UPDATE])
    ), patch.object(
        progress, "schedule_evidence_collection", Store([local])
    ):
        r = progress.load_progress("v2", NEW, date(2026, 1, 1), "UTC", baseline=V2)
        assert r["values"] == {"new9": 100, "new8": 60}
        assert len(r["history"]["new8"]) == 2


def test_ancestor_chain_intersects_ids_and_cannot_cross_projects_or_cycle():
    v3 = {
        "baseline_id": "v3",
        "project_id": "project",
        "progress_parent": {"baseline_id": "v2", "activity_ids": ["B", "C"]},
    }
    with patch.object(carry, "schedule_baselines_collection", Store([V1, V2, v3])):
        assert [(s["baseline_id"], ids) for s, ids in carry.inherited_sources(v3)] == [
            ("v2", {"B", "C"}),
            ("v1", {"B"}),
        ]
    foreign = {**V1, "project_id": "different"}
    with patch.object(carry, "schedule_baselines_collection", Store([foreign])):
        assert list(carry.inherited_sources(V2)) == []
    cycle = {**V1, "progress_parent": {"baseline_id": "v2", "activity_ids": ["A"]}}
    with patch.object(carry, "schedule_baselines_collection", Store([cycle, V2])):
        assert len(list(carry.inherited_sources(V2))) == 1


def test_activation_and_reactivation_keep_the_reviewed_source_link():
    store = Store([V1, V2])
    with patch.object(
        baseline_service, "resolve_project", return_value={"project_id": "project"}
    ), patch.object(
        baseline_service, "schedule_baselines_collection", store
    ), patch.object(
        baseline_service, "floorplans_collection", Mock()
    ):
        baseline_service.activate_schedule_baseline(
            project_ref="project", baseline_id="v2"
        )
        baseline_service.activate_schedule_baseline(
            project_ref="project", baseline_id="v1"
        )
        baseline_service.activate_schedule_baseline(
            project_ref="project", baseline_id="v2"
        )
    assert (
        store.find_one({"baseline_id": "v2"})["progress_parent"]
        == V2["progress_parent"]
    )
    assert "progress_parent" not in store.find_one({"baseline_id": "v1"})


def test_import_preview_stores_carry_plan_before_activation(tmp_path):
    store = Store([{**V1, "version": 1}])
    store.insert_one = lambda row: store.rows.append(copy.deepcopy(row))
    activities = Store(OLD)
    activities.insert_many = lambda rows, **kw: activities.rows.extend(
        copy.deepcopy(rows)
    )
    parsed = {
        "project": {},
        "source_type": "xer",
        "summary": {},
        "warnings": [],
        "calendars": [],
        "activities": NEW,
        "relationships": [],
        "assignments": [],
    }
    with patch.object(
        baseline_service,
        "resolve_project",
        return_value={
            "project_id": "project",
            "site_name": "Fozan",
            "floorplan_id": "floor",
        },
    ), patch.object(
        baseline_service, "schedule_baselines_collection", store
    ), patch.object(
        baseline_service, "schedule_activities_collection", activities
    ), patch.object(
        baseline_service, "site_baseline_dir", return_value=str(tmp_path)
    ), patch.object(
        baseline_service, "parse_xer", return_value=parsed
    ):
        result = baseline_service.import_schedule_baseline(
            project_ref="project", filename="Revision.xer", raw_bytes=b"new revision"
        )
    assert result["status"] == "needs_review"
    assert result["baseline"]["progress_carry_forward"]["matched_count"] == 2
    assert any(
        "2 matching activities" in w["message"] for w in result["baseline"]["warnings"]
    )
    assert store.find_one({"baseline_id": "v1"})["is_active"] is True


def test_inherited_client_snapshot_remains_available_after_revision():
    from services.progress.work_schedule import update_service

    update = {
        **UPDATE,
        "summary": {"reported_percent": 70},
        "forecast_finish_date": "2025-06-08",
    }
    with patch.object(
        carry, "schedule_baselines_collection", Store([V1, V2])
    ), patch.object(
        update_service,
        "latest_accepted",
        side_effect=lambda bid, as_of: update if bid == "v1" else None,
    ):
        result = update_service.attach_reported_progress(
            {"activities": []}, "v2", "2026-01-01", baseline=V2
        )
    assert result["reported_update"]["update_id"] == "xer1"
    assert result["reported_update"]["summary"]["reported_percent"] == 70
    assert result["reported_update"]["baseline_id"] == "v1"
