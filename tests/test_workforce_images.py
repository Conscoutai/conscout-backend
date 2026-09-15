from unittest.mock import MagicMock, patch

from services.safety import safety_service as safety


def sample_job():
    return {"job_id": "job1", "project_id": "project1", "tour_id": "tour1", "analysis_version": safety.ANALYSIS_VERSION}


def sample_tour():
    return {"name": "Example tour", "date": "2026-09-09", "nodes": [
        {"id": "a", "worker_count": 1}, {"id": "b", "worker_count": 1},
        {"id": "c", "worker_count": 0}, {"id": "d"},
        {"id": "a", "worker_count": 4}, {"worker_count": 9},
    ]}


def test_separate_images_have_separate_records_and_exact_source_links():
    records = safety.workforce_image_observations(sample_job(), sample_tour())
    assert [r["node_id"] for r in records] == ["a", "b", "c"]
    assert [r["observed_workers"] for r in records] == [1, 1, 0]
    assert [r["requires_review"] for r in records] == [True, True, False]
    assert len({r["record_id"] for r in records}) == 3
    assert records[1]["title"] == "Workforce observation · Point 2"
    assert records[1]["calculation"]["node_id"] == "b"
    # A retry or new analysis version uses the same review records.
    other_job = {**sample_job(), "job_id": "job2", "analysis_version": "next"}
    assert [r["record_id"] for r in records] == [r["record_id"] for r in safety.workforce_image_observations(other_job, sample_tour())]


def test_retry_preserves_independent_reviews_and_retains_legacy_audit_record():
    stored = {}
    collection = MagicMock()

    def upsert(query, update, upsert=False):
        assert upsert
        assert set(update) == {"$setOnInsert"}
        stored.setdefault(query["record_id"], dict(update["$setOnInsert"]))

    collection.update_one.side_effect = upsert
    with patch.object(safety, "raw_safety_records_collection", collection):
        ids = safety.store_workforce_image_observations(sample_job(), sample_tour())
        stored[ids[0]].update(status="confirmed", requires_review=False, observed_workers=2)
        safety.store_workforce_image_observations(sample_job(), sample_tour())
    assert len(stored) == 3
    assert stored[ids[0]]["observed_workers"] == 2
    assert stored[ids[0]]["status"] == "confirmed"
    assert stored[ids[1]]["requires_review"] is True
    assert collection.update_many.call_args.args[1]["$set"]["status"] == "superseded"
    collection.delete_many.assert_not_called()


def test_daily_count_is_peak_not_sum_or_most_recent_and_dismissal_is_excluded():
    records = safety.workforce_image_observations(sample_job(), sample_tour())
    records[0]["observed_workers"] = 2
    records[0]["requires_review"] = False
    # A lower-count image still needs review even when the peak is confirmed.
    daily = safety._workforce_daily_observation(list(reversed(records)), "2026-09-09")
    assert daily["observed_workers"] == 2
    assert daily["requires_review"] is True
    records[0]["status"] = "dismissed"
    daily = safety._workforce_daily_observation(records, "2026-09-09")
    assert daily["observed_workers"] == 1
    history = safety._manpower_history([], records, through_date="2026-09-09", days=2)
    assert history[0]["observed_workers"] is None
    assert history[1]["observed_workers"] == 1


def test_dashboard_shows_all_worker_images_for_selected_day_and_no_superseded_summary():
    tour = {"date": "2026-09-09", "nodes": [{"id": str(i), "worker_count": 1} for i in range(12)]}
    records = safety.workforce_image_observations(sample_job(), tour)
    records += [{**records[0], "record_id": "old", "record_date": "2026-09-08"},
                {**records[0], "record_id": "legacy", "status": "superseded"}]
    context = {"project_id": "project1", "site_name": "Example", "floorplan_id": "floor1", "document": {}}
    with patch.object(safety, "project_context", return_value=context), \
         patch.object(safety, "list_records", side_effect=lambda project, kind, **kwargs: records if kind == "workforce_observation" else []), \
         patch.object(safety, "list_analysis_jobs", return_value=[]), \
         patch.object(safety, "get_weather", return_value={"work_state": "safe", "reasons": []}), \
         patch.object(safety, "_schedule_manpower_for_dates", return_value=({}, False)), \
         patch.object(safety, "_project_inspection_status_records", return_value=([], [])):
        dashboard = safety.build_dashboard("Example", record_date="2026-09-09")
    assert len(dashboard["recent"]["observations"]) == 12
    assert [r["point_index"] for r in dashboard["recent"]["observations"]] == list(range(1, 13))
    assert dashboard["manpower"]["observed_workers"] == 1


def test_analysis_job_writes_one_record_per_image():
    job_collection, tour_collection, records_collection = MagicMock(), MagicMock(), MagicMock()
    job_collection.find_one.return_value = sample_job()
    tour_collection.find_one.return_value = sample_tour()
    with patch.object(safety, "raw_safety_analysis_jobs_collection", job_collection), \
         patch.object(safety, "raw_tours_collection", tour_collection), \
         patch.object(safety, "raw_safety_records_collection", records_collection):
        safety.run_analysis_job("job1")
    result = job_collection.update_one.call_args.args[1]["$set"]
    assert result["status"] == "completed"
    assert len(result["result"]["observation_ids"]) == 3
    assert records_collection.update_one.call_count == 3
