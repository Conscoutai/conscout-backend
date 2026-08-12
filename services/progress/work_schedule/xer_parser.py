from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Iterable, Optional


_XER_DATE_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d-%b-%y",
    "%d-%b-%Y",
    "%d/%m/%Y",
)

_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("site_clearance", ("site clearance", "clearing")),
    ("site_preparation", ("site preparation",)),
    ("excavation", ("excavat", "trenching")),
    ("backfilling", ("backfill",)),
    ("base_layer", ("base layer", "road base", "subbase", "sub-base")),
    ("paving_installation", ("interlock", "paving", "pavement")),
    ("asphalt", ("asphalt",)),
    ("curb_installation", ("curb", "kerb")),
    ("painting", ("thermoplastic", "road marking", "paint")),
    ("street_lighting", ("street light", "lighting pole", "light pole")),
    ("planting", ("planting", "landscape", "tree", "shrub", "palm")),
    ("irrigation", ("irrigation", "upvc", "valve", "pe pipe")),
    ("furniture", ("furniture", "bench", "wheel stopper", "shade")),
    ("concrete", ("concrete", "rcc")),
    ("electrical", ("electrical", "cable", "distribution board", "lv system")),
)

_NON_PHYSICAL_KEYWORDS = (
    "submittal",
    "approval",
    "pre-qualification",
    "prequalification",
    "issuing po",
    "purchase order",
    "shop drawing",
    "milestone",
    "testing and commissioning",
    "handover",
    "handing over",
)


class XerParseError(ValueError):
    pass


def _decode_xer(raw_bytes: bytes) -> str:
    if not raw_bytes:
        raise XerParseError("The XER file is empty")
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _parse_tables(text: str) -> dict[str, list[dict[str, str]]]:
    tables: dict[str, list[dict[str, str]]] = defaultdict(list)
    headers: dict[str, list[str]] = {}
    current_table = ""

    for raw_line in text.splitlines():
        parts = raw_line.rstrip("\r\n").split("\t")
        if not parts:
            continue
        marker = parts[0]
        if marker == "%T" and len(parts) > 1:
            current_table = parts[1].strip().upper()
        elif marker == "%F" and current_table:
            headers[current_table] = parts[1:]
        elif marker == "%R" and current_table in headers:
            row_values = parts[1:]
            row = {
                field_name: row_values[index] if index < len(row_values) else ""
                for index, field_name in enumerate(headers[current_table])
            }
            tables[current_table].append(row)
    return dict(tables)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        raw = str(value or "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    return int(round(_number(value, float(default))))


def parse_xer_datetime(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for date_format in _XER_DATE_FORMATS:
        try:
            return datetime.strptime(raw, date_format)
        except ValueError:
            continue
    return None


def _iso_datetime(value: Any) -> str:
    parsed = parse_xer_datetime(value)
    return parsed.isoformat() if parsed else ""


def _iso_date(value: Any) -> str:
    parsed = parse_xer_datetime(value)
    return parsed.date().isoformat() if parsed else ""


def normalize_work_category(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    if not normalized:
        return ""
    for category, needles in _CATEGORY_RULES:
        if any(needle in normalized for needle in needles):
            return category
    return normalized.replace(" ", "_")


def _photo_trackable(task_code: str, task_name: str, work_category: str) -> bool:
    normalized_name = task_name.lower()
    normalized_code = task_code.upper()
    if any(keyword in normalized_name for keyword in _NON_PHYSICAL_KEYWORDS):
        return False
    if ".ENG." in normalized_code or ".MAT." in normalized_code:
        return False
    return work_category in {
        "site_clearance",
        "site_preparation",
        "excavation",
        "backfilling",
        "base_layer",
        "paving_installation",
        "asphalt",
        "curb_installation",
        "painting",
        "street_lighting",
        "planting",
        "irrigation",
        "furniture",
        "concrete",
        "electrical",
    }


def _zone_from_wbs_path(path: Iterable[str]) -> str:
    for label in reversed(list(path)):
        match = re.search(r"\bzone\s+([a-z0-9_-]+)\b", label, re.IGNORECASE)
        if match:
            return f"Zone {match.group(1).upper()}"
    return ""


def _calendar_working_weekdays(calendar_data: Any) -> list[int]:
    """Return Python weekday numbers from P6's Sunday=1 weekday sections."""
    raw = str(calendar_data or "")
    if not raw:
        return [0, 1, 2, 3, 4]
    python_weekday_for_p6 = {1: 6, 2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5}
    weekdays: list[int] = []
    for p6_day in range(1, 8):
        marker = f"(0||{p6_day}()"
        start_index = raw.find(marker)
        if start_index < 0:
            continue
        following_indices = [
            index
            for next_day in range(p6_day + 1, 8)
            if (index := raw.find(f"(0||{next_day}()", start_index + len(marker)))
            >= 0
        ]
        end_index = min(following_indices) if following_indices else raw.find("VIEW(", start_index)
        section = raw[start_index : end_index if end_index >= 0 else len(raw)]
        if "s|" in section and "|f|" in section:
            weekdays.append(python_weekday_for_p6[p6_day])
    return sorted(weekdays) or [0, 1, 2, 3, 4]


def parse_xer(raw_bytes: bytes, *, filename: str = "baseline.xer") -> dict[str, Any]:
    text = _decode_xer(raw_bytes)
    if not text.lstrip().startswith("ERMHDR"):
        raise XerParseError("The uploaded file does not have a valid XER header")

    tables = _parse_tables(text)
    if not tables.get("PROJECT") or not tables.get("TASK"):
        raise XerParseError("The XER file does not contain PROJECT and TASK tables")

    project = tables["PROJECT"][0]
    project_xer_id = str(project.get("proj_id") or "").strip()

    def project_rows(table_name: str) -> list[dict[str, str]]:
        rows = tables.get(table_name, [])
        if not project_xer_id or not rows or "proj_id" not in rows[0]:
            return rows
        return [row for row in rows if str(row.get("proj_id") or "") == project_xer_id]

    wbs_rows = project_rows("PROJWBS")
    task_rows = project_rows("TASK")
    relationship_rows = project_rows("TASKPRED")
    assignment_rows = project_rows("TASKRSRC")
    resources = {str(row.get("rsrc_id") or ""): row for row in tables.get("RSRC", [])}
    calendars = {str(row.get("clndr_id") or ""): row for row in tables.get("CALENDAR", [])}
    wbs_by_id = {str(row.get("wbs_id") or ""): row for row in wbs_rows}

    path_cache: dict[str, list[str]] = {}

    def wbs_path(wbs_id: str) -> list[str]:
        if wbs_id in path_cache:
            return path_cache[wbs_id]
        labels: list[str] = []
        seen: set[str] = set()
        current_id = wbs_id
        while current_id and current_id not in seen:
            seen.add(current_id)
            current = wbs_by_id.get(current_id)
            if not current:
                break
            label = str(current.get("wbs_name") or current.get("wbs_short_name") or "").strip()
            if label:
                labels.append(label)
            current_id = str(current.get("parent_wbs_id") or "").strip()
        path_cache[wbs_id] = list(reversed(labels))
        return path_cache[wbs_id]

    assignments_by_task: dict[str, list[dict[str, str]]] = defaultdict(list)
    for assignment in assignment_rows:
        assignments_by_task[str(assignment.get("task_id") or "")].append(assignment)

    parsed_assignments: list[dict[str, Any]] = []
    activity_assignment_totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"target_cost": 0.0, "labor_hours": 0.0, "material_quantity": 0.0}
    )
    labor_loaded_activity_ids: set[str] = set()
    cost_loaded_activity_ids: set[str] = set()

    for row in assignment_rows:
        task_id = str(row.get("task_id") or "").strip()
        resource_id = str(row.get("rsrc_id") or "").strip()
        resource = resources.get(resource_id, {})
        resource_type = str(row.get("rsrc_type") or resource.get("rsrc_type") or "").strip()
        target_cost = _number(row.get("target_cost"))
        target_quantity = _number(row.get("target_qty"))
        if target_cost > 0:
            activity_assignment_totals[task_id]["target_cost"] += target_cost
            cost_loaded_activity_ids.add(task_id)
        if resource_type == "RT_Labor":
            activity_assignment_totals[task_id]["labor_hours"] += target_quantity
            labor_loaded_activity_ids.add(task_id)
        elif resource_type == "RT_Mat":
            activity_assignment_totals[task_id]["material_quantity"] += target_quantity
        parsed_assignments.append(
            {
                "assignment_id": str(row.get("taskrsrc_id") or "").strip(),
                "activity_internal_id": task_id,
                "resource_id": resource_id,
                "resource_name": str(resource.get("rsrc_name") or "").strip(),
                "resource_type": resource_type,
                "target_quantity": target_quantity,
                "remaining_quantity": _number(row.get("remain_qty")),
                "target_cost": target_cost,
                "remaining_cost": _number(row.get("remain_cost")),
                "actual_cost": _number(row.get("act_reg_cost"))
                + _number(row.get("act_ot_cost")),
                "target_start_at": _iso_datetime(row.get("target_start_date")),
                "target_end_at": _iso_datetime(row.get("target_end_date")),
                "curve_id": str(row.get("curv_id") or "").strip(),
                "has_time_phased_curve": bool(
                    str(row.get("curv_id") or row.get("target_crv") or "").strip()
                ),
            }
        )

    activities: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    zones: Counter[str] = Counter()
    for row in task_rows:
        internal_id = str(row.get("task_id") or "").strip()
        activity_id = str(row.get("task_code") or internal_id).strip()
        activity_name = str(row.get("task_name") or activity_id).strip()
        current_wbs_path = wbs_path(str(row.get("wbs_id") or "").strip())
        zone = _zone_from_wbs_path(current_wbs_path)
        if zone:
            zones[zone] += 1
        category = normalize_work_category(activity_name)
        totals = activity_assignment_totals[internal_id]
        total_float_hours = _number(row.get("total_float_hr_cnt"))
        target_duration_hours = _number(row.get("target_drtn_hr_cnt"))
        status_code = str(row.get("status_code") or "").strip()
        status_counts[status_code or "UNKNOWN"] += 1
        activities.append(
            {
                "activity_internal_id": internal_id,
                "activity_id": activity_id,
                "activity_name": activity_name,
                "wbs_id": str(row.get("wbs_id") or "").strip(),
                "wbs_path": current_wbs_path,
                "zone": zone,
                "work_category": category,
                "photo_trackable": _photo_trackable(activity_id, activity_name, category),
                "task_type": str(row.get("task_type") or "").strip(),
                "status_code": status_code,
                "completion_type": str(row.get("complete_pct_type") or "").strip(),
                "physical_complete_percent": _number(row.get("phys_complete_pct")),
                "start_date": _iso_date(row.get("target_start_date")),
                "end_date": _iso_date(row.get("target_end_date")),
                "target_start_at": _iso_datetime(row.get("target_start_date")),
                "target_end_at": _iso_datetime(row.get("target_end_date")),
                "actual_start_at": _iso_datetime(row.get("act_start_date")),
                "actual_end_at": _iso_datetime(row.get("act_end_date")),
                "target_duration_hours": target_duration_hours,
                "remaining_duration_hours": _number(row.get("remain_drtn_hr_cnt")),
                "total_float_hours": total_float_hours,
                "free_float_hours": _number(row.get("free_float_hr_cnt")),
                "is_critical": total_float_hours <= 0,
                "calendar_id": str(row.get("clndr_id") or "").strip(),
                "target_cost": round(totals["target_cost"], 6),
                "target_labor_hours": round(totals["labor_hours"], 6),
                "target_material_quantity": round(totals["material_quantity"], 6),
                "planned_quantity": _number(row.get("target_work_qty")),
                "quantity_unit": "",
                "weight_source": "target_cost" if totals["target_cost"] > 0 else "duration",
                "weight": totals["target_cost"] if totals["target_cost"] > 0 else max(target_duration_hours, 1.0),
                "mapping_status": "suggested" if zone and category else "needs_review",
            }
        )

    parsed_relationships = [
        {
            "relationship_id": str(row.get("task_pred_id") or "").strip(),
            "activity_internal_id": str(row.get("task_id") or "").strip(),
            "predecessor_internal_id": str(row.get("pred_task_id") or "").strip(),
            "relationship_type": str(row.get("pred_type") or "").strip(),
            "lag_hours": _number(row.get("lag_hr_cnt")),
        }
        for row in relationship_rows
    ]

    calendar_payload = [
        {
            "calendar_id": calendar_id,
            "name": str(row.get("clndr_name") or "").strip(),
            "type": str(row.get("clndr_type") or "").strip(),
            "hours_per_day": _number(row.get("day_hr_cnt"), 8.0),
            "hours_per_week": _number(row.get("week_hr_cnt"), 40.0),
            "working_weekdays": _calendar_working_weekdays(row.get("clndr_data")),
            "calendar_data": str(row.get("clndr_data") or ""),
        }
        for calendar_id, row in calendars.items()
    ]

    total_target_cost = round(sum(item["target_cost"] for item in parsed_assignments), 6)
    total_labor_hours = round(
        sum(
            item["target_quantity"]
            for item in parsed_assignments
            if item["resource_type"] == "RT_Labor"
        ),
        6,
    )
    curve_assignment_count = sum(
        1 for item in parsed_assignments if item["has_time_phased_curve"]
    )
    tasks_without_cost = len(activities) - len(cost_loaded_activity_ids)

    warnings: list[dict[str, Any]] = []
    if tasks_without_cost:
        warnings.append(
            {
                "code": "activities_without_cost",
                "severity": "warning",
                "message": f"{tasks_without_cost} activities have no target cost and are excluded from the cost-weighted S-curve.",
                "count": tasks_without_cost,
            }
        )
    if len(labor_loaded_activity_ids) < len(activities):
        warnings.append(
            {
                "code": "partial_labor_loading",
                "severity": "warning",
                "message": f"Labor assignments cover {len(labor_loaded_activity_ids)} of {len(activities)} activities; manpower is partial.",
                "count": len(labor_loaded_activity_ids),
            }
        )
    if parsed_assignments and curve_assignment_count == 0:
        warnings.append(
            {
                "code": "missing_resource_curves",
                "severity": "info",
                "message": "No time-phased resource curves were found. Linear distribution across working dates will be used.",
                "count": 0,
            }
        )
    if any(item["actual_start_at"] or item["actual_end_at"] for item in activities):
        warnings.append(
            {
                "code": "xer_contains_actuals",
                "severity": "info",
                "message": "The XER contains actual dates. Imported actuals remain separate from photo-verified progress.",
                "count": sum(
                    1 for item in activities if item["actual_start_at"] or item["actual_end_at"]
                ),
            }
        )

    project_name = ""
    for row in wbs_rows:
        if str(row.get("proj_node_flag") or "").upper() == "Y":
            project_name = str(row.get("wbs_name") or "").strip()
            break
    project_name = project_name or str(project.get("proj_short_name") or filename).strip()

    return {
        "source_type": "xer",
        "project": {
            "xer_project_id": project_xer_id,
            "name": project_name,
            "short_name": str(project.get("proj_short_name") or "").strip(),
            "planned_start_at": _iso_datetime(project.get("plan_start_date")),
            "planned_end_at": _iso_datetime(
                project.get("scd_end_date") or project.get("plan_end_date")
            ),
            "last_scheduled_at": _iso_datetime(project.get("last_schedule_date")),
            "data_date": _iso_datetime(
                project.get("next_data_date") or project.get("last_schedule_date")
            ),
            "critical_path_type": str(project.get("critical_path_type") or "").strip(),
            "default_percent_complete_type": str(
                project.get("def_complete_pct_type") or ""
            ).strip(),
        },
        "summary": {
            "activity_count": len(activities),
            "wbs_count": len(wbs_rows),
            "relationship_count": len(parsed_relationships),
            "assignment_count": len(parsed_assignments),
            "cost_loaded_activity_count": len(cost_loaded_activity_ids),
            "activities_without_cost_count": tasks_without_cost,
            "labor_assignment_count": sum(
                1 for item in parsed_assignments if item["resource_type"] == "RT_Labor"
            ),
            "labor_loaded_activity_count": len(labor_loaded_activity_ids),
            "target_cost": total_target_cost,
            "target_labor_hours": total_labor_hours,
            "time_phased_assignment_count": curve_assignment_count,
            "zones": [
                {"name": zone_name, "activity_count": count}
                for zone_name, count in sorted(zones.items())
            ],
            "status_counts": dict(status_counts),
        },
        "warnings": warnings,
        "calendars": calendar_payload,
        "activities": activities,
        "relationships": parsed_relationships,
        "assignments": parsed_assignments,
    }
