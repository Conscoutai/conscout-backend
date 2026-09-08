"""Pure regression tests: no database or live service access required."""
import ast
from datetime import date, timedelta
from pathlib import Path
import random
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]

def load_function(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    function.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), path, "exec"), namespace)
    return namespace[name]

class ProgressPerformanceTests(unittest.TestCase):
    def test_working_days_matches_daily_count_for_calendar_boundaries(self):
        count = load_function("services/progress/work_schedule/analytics_service.py", "_working_days", {})
        rng = random.Random(19)
        for _ in range(1000):
            start = date(2020, 1, 1) + timedelta(days=rng.randrange(2500))
            end = start + timedelta(days=rng.randrange(-10, 1000))
            weekdays = {day for day in range(7) if rng.choice([True, False])}
            expected = sum((start + timedelta(days=n)).weekday() in (weekdays or {0, 1, 2, 3, 4}) for n in range(max(0, (end-start).days+1)))
            self.assertEqual(count(start, end, weekdays), expected)
        self.assertEqual(count(date(2024, 2, 28), date(2024, 3, 1), None), 3)
        self.assertEqual(count(date(2024, 1, 1), date(2024, 1, 31), {7, 8}), 0)

    def test_notification_services_reuse_supplied_comparison(self):
        for path, name in [
            ("services/progress/work_schedule/work_schedule_notification_service.py", "sync_schedule_delay_notifications"),
            ("services/progress/prediction_notification_service.py", "sync_prediction_notifications"),
        ]:
            calculate = Mock(return_value={"activities": []})
            namespace = {"_project_doc": Mock(return_value={}), "_site_name": Mock(return_value="Fozan"), "work_schedule_comparison": calculate, "_resolve_project_recipients": Mock(side_effect=RuntimeError("stop before notification writes"))}
            sync = load_function(path, name, namespace)
            with self.assertRaisesRegex(RuntimeError, "stop before"):
                sync(project_id="Fozan", comparison={"activities": []})
            calculate.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "stop before"):
                sync(project_id="Fozan")
            calculate.assert_called_once_with("Fozan")

    def test_comparison_route_passes_one_result_to_both_notification_syncs(self):
        comparison = {"activities": []}
        schedule, prediction = Mock(return_value={}), Mock(return_value={})
        calculate = Mock(return_value=comparison)
        route = load_function("api/routes/progress/work_shedule.py", "work_schedule_comparison", {"work_schedule_comparison_service": calculate, "_best_effort_schedule_notification_sync": schedule, "_best_effort_prediction_notification_sync": prediction})
        self.assertIs(route("Fozan"), comparison)
        calculate.assert_called_once_with("Fozan")
        self.assertIs(schedule.call_args.kwargs["comparison"], comparison)
        self.assertIs(prediction.call_args.kwargs["comparison"], comparison)

if __name__ == "__main__":
    unittest.main()
