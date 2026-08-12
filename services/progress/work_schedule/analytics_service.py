from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from core.database import (
    schedule_activities_collection,
    schedule_assignments_collection,
    schedule_baselines_collection,
    schedule_evidence_collection,
    schedule_relationships_collection,
)

from .baseline_service import resolve_project


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


def _project_today(timezone_name: str) -> date:
    try:
        return datetime.now(ZoneInfo(timezone_name or "UTC")).date()
    except ZoneInfoNotFoundError:
        return datetime.now(timezone.utc).date()


def _project_date(value: Any, timezone_name: str) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(raw)
    except ValueError:
        return _parse_date(value)
    if parsed.tzinfo is None:
        return parsed.date()
    try:
        return parsed.astimezone(ZoneInfo(timezone_name or "UTC")).date()
    except ZoneInfoNotFoundError:
        return parsed.astimezone(timezone.utc).date()


def _working_days(
    start: date, end: date, working_weekdays: Optional[set[int]] = None
) -> int:
    if end < start:
        return 0
    total = 0
    current = start
    while current <= end:
        if current.weekday() in (working_weekdays or {0, 1, 2, 3, 4}):
            total += 1
        current += timedelta(days=1)
    return total


def _add_working_days(
    start: date, days: int, working_weekdays: Optional[set[int]] = None
) -> date:
    weekdays = working_weekdays or {0, 1, 2, 3, 4}
    if days <= 0:
        return start
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() in weekdays:
            remaining -= 1
    return current


def _subtract_working_days(
    finish: date, days: int, working_weekdays: Optional[set[int]] = None
) -> date:
    weekdays = working_weekdays or {0, 1, 2, 3, 4}
    if days <= 0:
        return finish
    current = finish
    remaining = days
    while remaining > 0:
        current -= timedelta(days=1)
        if current.weekday() in weekdays:
            remaining -= 1
    return current


def _calendar_weekdays(
    activity: dict[str, Any], calendars: Optional[dict[str, dict[str, Any]]]
) -> set[int]:
    calendar = (calendars or {}).get(str(activity.get("calendar_id") or ""), {})
    raw = calendar.get("working_weekdays") or [0, 1, 2, 3, 4]
    weekdays = {int(item) for item in raw if isinstance(item, (int, float))}
    return weekdays or {0, 1, 2, 3, 4}


def _planned_percent(
    activity: dict[str, Any],
    as_of: date,
    calendars: Optional[dict[str, dict[str, Any]]] = None,
) -> float:
    start = _parse_date(activity.get("start_date"))
    end = _parse_date(activity.get("end_date"))
    if not start or not end:
        return 0.0
    if end <= start or activity.get("task_type") in {"TT_Mile", "TT_FinMile"}:
        return 100.0 if as_of >= end else 0.0
    if as_of < start:
        return 0.0
    if as_of >= end:
        return 100.0
    weekdays = _calendar_weekdays(activity, calendars)
    total_days = max(_working_days(start, end, weekdays), 1)
    elapsed_days = _working_days(start, as_of, weekdays)
    return round(min(100.0, max(0.0, elapsed_days * 100.0 / total_days)), 3)


def _weighted_percent(
    activities: Iterable[dict[str, Any]],
    percentages: dict[str, float],
    *,
    weight_field: str,
    positive_only: bool = True,
) -> float:
    numerator = 0.0
    denominator = 0.0
    for activity in activities:
        weight = float(activity.get(weight_field) or 0.0)
        if positive_only and weight <= 0:
            continue
        if weight <= 0:
            weight = 1.0
        activity_key = str(activity.get("activity_internal_id") or "")
        numerator += weight * float(percentages.get(activity_key, 0.0))
        denominator += weight
    return round(numerator / denominator, 3) if denominator else 0.0


def _baseline_payload(baseline: dict[str, Any]) -> dict[str, Any]:
    return {
        "baseline_id": str(baseline.get("baseline_id") or ""),
        "version": int(baseline.get("version") or 0),
        "name": str(baseline.get("name") or ""),
        "source_type": str(baseline.get("source_type") or ""),
        "source_filename": str(baseline.get("source_filename") or ""),
        "source_url": str(baseline.get("source_url") or ""),
        "status": str(baseline.get("status") or ""),
        "is_active": baseline.get("is_active") is True,
        "timezone": str(baseline.get("timezone") or "UTC"),
        "project": baseline.get("project") or {},
        "summary": baseline.get("summary") or {},
        "warnings": baseline.get("warnings") or [],
        "uploaded_at": baseline.get("uploaded_at"),
        "activated_at": baseline.get("activated_at"),
    }


def _evidence_by_activity(
    *, baseline_id: str, as_of: date, timezone_name: str
) -> tuple[dict[str, float], dict[str, list[dict[str, Any]]], Optional[date]]:
    actual_by_activity: dict[str, float] = {}
    evidence_by_activity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    latest_observation: Optional[date] = None
    evidence_documents = list(
        schedule_evidence_collection.find(
            {"baseline_id": baseline_id}, {"_id": 0}
        ).sort([("captured_at", 1), ("updated_at", 1)])
    )
    for evidence in evidence_documents:
        observed_on = _project_date(
            evidence.get("captured_at") or evidence.get("observed_at"),
            timezone_name,
        )
        if observed_on and observed_on > as_of:
            continue
        activity_key = str(evidence.get("activity_internal_id") or "")
        public_item = {
            "evidence_id": str(evidence.get("evidence_id") or ""),
            "tour_id": str(evidence.get("tour_id") or ""),
            "tour_name": str(evidence.get("tour_name") or "Tour"),
            "site_name": str(evidence.get("site_name") or ""),
            "observed_at": observed_on.isoformat() if observed_on else "",
            "captured_at": evidence.get("captured_at"),
            "uploaded_at": evidence.get("uploaded_at"),
            "node_id": str(evidence.get("node_id") or ""),
            "node_index": evidence.get("node_index"),
            "total_nodes": evidence.get("total_nodes"),
            "work_type": str(evidence.get("work_type") or ""),
            "work_category": str(evidence.get("work_category") or ""),
            "zone": str(evidence.get("zone") or ""),
            "image_url": str(evidence.get("image_url") or ""),
            "confidence": evidence.get("confidence"),
            "status": str(evidence.get("status") or "needs_review"),
            "suggested_percent": evidence.get("suggested_percent"),
            "approved_percent": evidence.get("approved_percent"),
            "review_note": str(evidence.get("review_note") or ""),
        }
        evidence_by_activity[activity_key].append(public_item)
        if evidence.get("status") == "approved" and evidence.get("approved_percent") is not None:
            actual_by_activity[activity_key] = min(
                100.0, max(0.0, float(evidence["approved_percent"]))
            )
            if observed_on and (latest_observation is None or observed_on > latest_observation):
                latest_observation = observed_on
    for activity_key, items in evidence_by_activity.items():
        evidence_by_activity[activity_key] = list(reversed(items[-10:]))
    return actual_by_activity, evidence_by_activity, latest_observation


def _forecast_schedule_dates(
    *,
    baseline: dict[str, Any],
    activities: list[dict[str, Any]],
    actual_by_activity: dict[str, float],
    evidence_by_activity: dict[str, list[dict[str, Any]]],
    as_of: date,
) -> dict[str, dict[str, Any]]:
    calendars = {
        str(item.get("calendar_id") or ""): item
        for item in baseline.get("calendars") or []
    }
    activity_by_id = {
        str(activity.get("activity_internal_id") or ""): activity
        for activity in activities
    }
    forecast: dict[str, dict[str, Any]] = {}
    for activity_id, activity in activity_by_id.items():
        baseline_start = _parse_date(activity.get("start_date")) or as_of
        baseline_finish = _parse_date(activity.get("end_date")) or baseline_start
        actual = actual_by_activity.get(activity_id, 0.0)
        evidence_dates = [
            observed
            for item in evidence_by_activity.get(activity_id, [])
            if (
                observed := _project_date(
                    item.get("captured_at") or item.get("observed_at"),
                    str(baseline.get("timezone") or "UTC"),
                )
            )
        ]
        weekdays = _calendar_weekdays(activity, calendars)
        baseline_workdays = max(
            _working_days(baseline_start, baseline_finish, weekdays), 1
        )
        remaining_workdays = 0 if actual >= 100 else max(
            1, int(round(baseline_workdays * (1.0 - actual / 100.0)))
        )
        if actual >= 100:
            finish = max(evidence_dates) if evidence_dates else baseline_finish
            start = min(evidence_dates) if evidence_dates else baseline_start
        else:
            start = max(as_of, baseline_start)
            finish = _add_working_days(start, max(remaining_workdays - 1, 0), weekdays)
        forecast[activity_id] = {
            "start": start,
            "finish": finish,
            "remaining_workdays": remaining_workdays,
            "weekdays": weekdays,
            "complete": actual >= 100,
        }

    relationships = list(
        schedule_relationships_collection.find(
            {"baseline_id": baseline["baseline_id"]}, {"_id": 0}
        )
    )
    # Relax relationship constraints repeatedly. This supports mixed P6 FS/SS/
    # FF/SF logic while remaining safe if a malformed import contains a cycle.
    for _ in range(max(1, min(len(activities), 500))):
        changed = False
        for relationship in relationships:
            successor_id = str(relationship.get("activity_internal_id") or "")
            predecessor_id = str(
                relationship.get("predecessor_internal_id") or ""
            )
            successor = forecast.get(successor_id)
            predecessor = forecast.get(predecessor_id)
            if not successor or not predecessor or successor["complete"]:
                continue
            lag_days = int(round(float(relationship.get("lag_hours") or 0.0) / 8.0))
            relation_type = str(relationship.get("relationship_type") or "PR_FS")
            required_start = successor["start"]
            if relation_type == "PR_SS":
                required_start = predecessor["start"] + timedelta(days=lag_days)
            elif relation_type == "PR_FF":
                required_finish = predecessor["finish"] + timedelta(days=lag_days)
                required_start = _subtract_working_days(
                    required_finish,
                    max(successor["remaining_workdays"] - 1, 0),
                    successor["weekdays"],
                )
            elif relation_type == "PR_SF":
                required_finish = predecessor["start"] + timedelta(days=lag_days)
                required_start = _subtract_working_days(
                    required_finish,
                    max(successor["remaining_workdays"] - 1, 0),
                    successor["weekdays"],
                )
            else:
                required_start = predecessor["finish"] + timedelta(days=lag_days)
            required_start = max(as_of, required_start)
            if required_start > successor["start"]:
                successor["start"] = required_start
                successor["finish"] = _add_working_days(
                    required_start,
                    max(successor["remaining_workdays"] - 1, 0),
                    successor["weekdays"],
                )
                changed = True
        if not changed:
            break
    return forecast


def _curve_points(
    *,
    baseline: dict[str, Any],
    activities: list[dict[str, Any]],
    as_of: date,
) -> dict[str, list[dict[str, Any]]]:
    project = baseline.get("project") or {}
    start = _parse_date(project.get("planned_start_at"))
    end = _parse_date(project.get("planned_end_at"))
    if not start or not end:
        return {"planned": [], "actual": [], "forecast": []}
    calendars = {
        str(item.get("calendar_id") or ""): item
        for item in baseline.get("calendars") or []
    }
    weight_field = (
        "target_cost"
        if float((baseline.get("summary") or {}).get("target_cost") or 0) > 0
        else "target_duration_hours"
    )

    planned_points: list[dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        percentages = {
            str(activity.get("activity_internal_id") or ""): _planned_percent(
                activity, cursor, calendars
            )
            for activity in activities
        }
        planned_points.append(
            {
                "date": cursor.isoformat(),
                "percent": _weighted_percent(
                    activities, percentages, weight_field=weight_field
                ),
            }
        )
        cursor += timedelta(days=7)
    if not planned_points or planned_points[-1]["date"] != end.isoformat():
        percentages = {
            str(activity.get("activity_internal_id") or ""): _planned_percent(
                activity, end, calendars
            )
            for activity in activities
        }
        planned_points.append(
            {
                "date": end.isoformat(),
                "percent": _weighted_percent(
                    activities, percentages, weight_field=weight_field
                ),
            }
        )

    approved_evidence = list(
        schedule_evidence_collection.find(
            {
                "baseline_id": baseline["baseline_id"],
                "status": "approved",
                "approved_percent": {"$ne": None},
            },
            {"_id": 0},
        ).sort([("captured_at", 1), ("updated_at", 1)])
    )
    observed_dates = sorted(
        {
            observed
            for item in approved_evidence
            if (
                observed := _project_date(
                    item.get("captured_at") or item.get("observed_at"),
                    str(baseline.get("timezone") or "UTC"),
                )
            )
            and observed <= as_of
        }
    )
    actual_points: list[dict[str, Any]] = []
    for observed_on in observed_dates:
        actual_by_activity: dict[str, float] = {}
        for evidence in approved_evidence:
            evidence_date = _project_date(
                evidence.get("captured_at") or evidence.get("observed_at"),
                str(baseline.get("timezone") or "UTC"),
            )
            if evidence_date and evidence_date <= observed_on:
                actual_by_activity[str(evidence.get("activity_internal_id") or "")] = float(
                    evidence.get("approved_percent") or 0.0
                )
        actual_points.append(
            {
                "date": observed_on.isoformat(),
                "percent": _weighted_percent(
                    activities, actual_by_activity, weight_field=weight_field
                ),
            }
        )
    if not actual_points:
        actual_points.append({"date": as_of.isoformat(), "percent": 0.0})

    return {"planned": planned_points, "actual": actual_points, "forecast": []}


def _manpower_points(
    *, baseline: dict[str, Any], activities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    project = baseline.get("project") or {}
    start = _parse_date(project.get("planned_start_at"))
    end = _parse_date(project.get("planned_end_at"))
    if not start or not end:
        return []
    activities_by_internal_id = {
        str(activity.get("activity_internal_id") or ""): activity
        for activity in activities
    }
    calendars = {
        str(item.get("calendar_id") or ""): item
        for item in baseline.get("calendars") or []
    }
    labor_assignments = list(
        schedule_assignments_collection.find(
            {"baseline_id": baseline["baseline_id"], "resource_type": "RT_Labor"},
            {"_id": 0},
        )
    )
    points: list[dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        week_end = min(end, cursor + timedelta(days=6))
        worker_days = 0.0
        labor_hours = 0.0
        for assignment in labor_assignments:
            activity = activities_by_internal_id.get(
                str(assignment.get("activity_internal_id") or "")
            )
            if not activity:
                continue
            activity_start = _parse_date(activity.get("start_date"))
            activity_end = _parse_date(activity.get("end_date"))
            if not activity_start or not activity_end:
                continue
            overlap_start = max(cursor, activity_start)
            overlap_end = min(week_end, activity_end)
            weekdays = _calendar_weekdays(activity, calendars)
            overlap_days = _working_days(overlap_start, overlap_end, weekdays)
            if overlap_days <= 0:
                continue
            total_activity_days = max(
                _working_days(activity_start, activity_end, weekdays), 1
            )
            calendar = calendars.get(str(activity.get("calendar_id") or ""), {})
            hours_per_day = float(calendar.get("hours_per_day") or 8.0)
            total_hours = float(assignment.get("target_quantity") or 0.0)
            daily_workers = total_hours / max(total_activity_days * hours_per_day, 1.0)
            worker_days += daily_workers * overlap_days
            labor_hours += total_hours * overlap_days / total_activity_days
        configured_weekdays = {
            weekday
            for calendar in calendars.values()
            for weekday in calendar.get("working_weekdays", [])
            if isinstance(weekday, int)
        }
        working_days_in_week = max(
            _working_days(cursor, week_end, configured_weekdays or None), 1
        )
        points.append(
            {
                "date": cursor.isoformat(),
                "planned_workers": round(worker_days / working_days_in_week, 2),
                "planned_labor_hours": round(labor_hours, 2),
                "observed_workers": None,
            }
        )
        cursor += timedelta(days=7)
    return points


def build_baseline_comparison(
    project_ref: str, *, as_of: Optional[date] = None
) -> Optional[dict[str, Any]]:
    project_context = resolve_project(project_ref)
    project_id = project_context["project_id"]
    baseline = schedule_baselines_collection.find_one(
        {"project_id": project_id, "is_active": True}, sort=[("version", -1)]
    )
    if not baseline:
        baseline = schedule_baselines_collection.find_one(
            {"project_id": project_id}, sort=[("version", -1)]
        )
    if not baseline:
        return None

    observation_date = as_of or _project_today(str(baseline.get("timezone") or "UTC"))
    activities = list(
        schedule_activities_collection.find(
            {"baseline_id": baseline["baseline_id"]}, {"_id": 0}
        ).sort([("start_date", 1), ("activity_id", 1)])
    )
    actual_by_activity, evidence_by_activity, latest_observation = _evidence_by_activity(
        baseline_id=baseline["baseline_id"],
        as_of=observation_date,
        timezone_name=str(baseline.get("timezone") or "UTC"),
    )
    calendars = {
        str(item.get("calendar_id") or ""): item
        for item in baseline.get("calendars") or []
    }
    planned_by_activity = {
        str(activity.get("activity_internal_id") or ""): _planned_percent(
            activity, observation_date, calendars
        )
        for activity in activities
    }

    weighting_method = (
        "target_cost"
        if float((baseline.get("summary") or {}).get("target_cost") or 0) > 0
        else "duration"
    )
    primary_weight_field = (
        "target_cost"
        if weighting_method == "target_cost"
        else "target_duration_hours"
    )

    project_planned = _weighted_percent(
        activities, planned_by_activity, weight_field=primary_weight_field
    )
    project_actual = _weighted_percent(
        activities, actual_by_activity, weight_field=primary_weight_field
    )
    duration_planned = _weighted_percent(
        activities,
        planned_by_activity,
        weight_field="target_duration_hours",
    )
    duration_actual = _weighted_percent(
        activities,
        actual_by_activity,
        weight_field="target_duration_hours",
    )

    activity_rows: list[dict[str, Any]] = []
    delayed_count = 0
    needs_review_count = 0
    for activity in activities:
        internal_id = str(activity.get("activity_internal_id") or "")
        planned = planned_by_activity.get(internal_id, 0.0)
        actual = actual_by_activity.get(internal_id, 0.0)
        variance = round(actual - planned, 3)
        end_date = _parse_date(activity.get("end_date"))
        delay_days = 0
        if end_date and actual < 100 and observation_date > end_date:
            delay_days = (observation_date - end_date).days
            delayed_count += 1
        evidence = evidence_by_activity.get(internal_id, [])
        if any(item.get("status") == "needs_review" for item in evidence):
            needs_review_count += 1
        primary_status = "NOT STARTED"
        if actual >= 100:
            primary_status = "DONE"
        elif actual > 0:
            primary_status = "IN PROGRESS"
        elif planned > 0:
            primary_status = "DELAYED" if delay_days > 0 else "NOT STARTED"
        observed_dates = [
            _parse_date(item.get("captured_at") or item.get("observed_at"))
            for item in evidence
        ]
        observed_dates = [item for item in observed_dates if item]
        activity_rows.append(
            {
                **activity,
                "planned_percent": planned,
                "actual_percent": actual,
                "variance_percent": variance,
                "delay_days": delay_days,
                "status": primary_status,
                "primary_status": primary_status,
                "observed_start_date": min(observed_dates).isoformat()
                if observed_dates
                else "",
                "observed_end_date": max(observed_dates).isoformat()
                if observed_dates
                else "",
                "related_tour_ids": sorted(
                    {
                        str(item.get("tour_id") or "")
                        for item in evidence
                        if str(item.get("tour_id") or "")
                    }
                ),
                "evidence": evidence,
            }
        )

    forecast_by_activity = _forecast_schedule_dates(
        baseline=baseline,
        activities=activities,
        actual_by_activity=actual_by_activity,
        evidence_by_activity=evidence_by_activity,
        as_of=observation_date,
    )
    baseline_finish = _parse_date((baseline.get("project") or {}).get("planned_end_at"))
    forecast_finish = max(
        (item["finish"] for item in forecast_by_activity.values()),
        default=baseline_finish,
    )
    forecast_delay_days = max(
        0,
        (forecast_finish - baseline_finish).days
        if forecast_finish and baseline_finish
        else 0,
    )
    for activity in activity_rows:
        forecast_item = forecast_by_activity.get(
            str(activity.get("activity_internal_id") or "")
        )
        activity["forecast_start_date"] = (
            forecast_item["start"].isoformat() if forecast_item else ""
        )
        activity["forecast_end_date"] = (
            forecast_item["finish"].isoformat() if forecast_item else ""
        )
    curves = _curve_points(
        baseline=baseline,
        activities=activities,
        as_of=observation_date,
    )
    if curves["actual"] and forecast_finish:
        latest_actual = curves["actual"][-1]
        curves["forecast"] = [
            latest_actual,
            {"date": forecast_finish.isoformat(), "percent": 100.0},
        ]

    return {
        "project_id": project_id,
        "site_name": project_context["site_name"],
        "baseline": _baseline_payload(baseline),
        "summary": {
            "planned_percent": project_planned,
            "actual_percent": project_actual,
            "variance_percent": round(project_actual - project_planned, 3),
            "schedule_planned_percent": duration_planned,
            "schedule_actual_percent": duration_actual,
            "delay_days": forecast_delay_days,
            "baseline_finish_date": baseline_finish.isoformat()
            if baseline_finish
            else "",
            "forecast_finish_date": forecast_finish.isoformat()
            if forecast_finish
            else "",
            "delayed_activity_count": delayed_count,
            "critical_activity_count": sum(
                1 for activity in activities if activity.get("is_critical")
            ),
            "needs_review_count": needs_review_count,
            "data_as_of": observation_date.isoformat(),
            "last_verified_capture_date": latest_observation.isoformat()
            if latest_observation
            else "",
            "weighting_method": weighting_method,
        },
        "curves": curves,
        "manpower": {
            "is_partial": int(
                (baseline.get("summary") or {}).get("labor_loaded_activity_count")
                or 0
            )
            < len(activities),
            "points": _manpower_points(baseline=baseline, activities=activities),
        },
        "activities": activity_rows,
        "actual_percent": project_actual,
    }


def baseline_comparison_or_404(project_ref: str) -> dict[str, Any]:
    payload = build_baseline_comparison(project_ref)
    if payload is None:
        raise HTTPException(404, "No schedule baseline found")
    return payload
