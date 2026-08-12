from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any

from .xer_parser import normalize_work_category


class PdfScheduleParseError(ValueError):
    pass


_DATE_PATTERN = re.compile(
    r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}-[A-Za-z]{3}-\d{2,4})\b"
)


def _parse_date(raw: str) -> str:
    for date_format in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%d-%b-%Y",
        "%d-%b-%y",
    ):
        try:
            return datetime.strptime(raw, date_format).date().isoformat()
        except ValueError:
            continue
    return ""


def parse_pdf_schedule(raw_bytes: bytes, *, filename: str = "baseline.pdf") -> dict[str, Any]:
    if not raw_bytes:
        raise PdfScheduleParseError("The PDF file is empty")
    try:
        from pypdf import PdfReader
    except ImportError as error:
        try:
            # PyPDF2 is retained as a compatibility fallback for older Conscout
            # deployments; new installations use the maintained pypdf package.
            from PyPDF2 import PdfReader
        except ImportError:
            raise PdfScheduleParseError(
                "PDF schedule extraction requires the pypdf dependency"
            ) from error

    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
        page_text = [(page.extract_text() or "") for page in reader.pages[:250]]
    except Exception as error:
        raise PdfScheduleParseError("The uploaded PDF could not be read") from error

    text = "\n".join(page_text)
    if not text.strip():
        raise PdfScheduleParseError(
            "No selectable text was found. A scanned schedule requires OCR and manual review."
        )

    activities: list[dict[str, Any]] = []
    seen_activity_ids: set[str] = set()
    for line in text.splitlines():
        compact = " ".join(line.split())
        dates = list(_DATE_PATTERN.finditer(compact))
        if len(dates) < 2:
            continue
        prefix = compact[: dates[0].start()].strip()
        tokens = prefix.split(maxsplit=1)
        if len(tokens) < 2:
            continue
        activity_id, activity_name = tokens[0].strip(), tokens[1].strip(" -:")
        if not re.search(r"\d", activity_id) or len(activity_name) < 3:
            continue
        start_date = _parse_date(dates[0].group(0))
        end_date = _parse_date(dates[1].group(0))
        if not start_date or not end_date or activity_id in seen_activity_ids:
            continue
        seen_activity_ids.add(activity_id)
        category = normalize_work_category(activity_name)
        activities.append(
            {
                "activity_internal_id": activity_id,
                "activity_id": activity_id,
                "activity_name": activity_name,
                "wbs_id": "",
                "wbs_path": [],
                "zone": "",
                "work_category": category,
                "photo_trackable": False,
                "task_type": "PDF_ROW",
                "status_code": "UNKNOWN",
                "completion_type": "",
                "physical_complete_percent": 0.0,
                "start_date": start_date,
                "end_date": end_date,
                "target_start_at": f"{start_date}T00:00:00",
                "target_end_at": f"{end_date}T00:00:00",
                "actual_start_at": "",
                "actual_end_at": "",
                "target_duration_hours": 0.0,
                "remaining_duration_hours": 0.0,
                "total_float_hours": 0.0,
                "free_float_hours": 0.0,
                "is_critical": False,
                "calendar_id": "",
                "target_cost": 0.0,
                "target_labor_hours": 0.0,
                "target_material_quantity": 0.0,
                "planned_quantity": 0.0,
                "quantity_unit": "",
                "weight_source": "duration",
                "weight": 1.0,
                "mapping_status": "needs_review",
            }
        )

    if not activities:
        raise PdfScheduleParseError(
            "No reliable activity rows were detected. Upload an XER or review the PDF manually."
        )

    starts = [item["start_date"] for item in activities]
    finishes = [item["end_date"] for item in activities]
    return {
        "source_type": "pdf",
        "project": {
            "xer_project_id": "",
            "name": filename,
            "short_name": filename,
            "planned_start_at": f"{min(starts)}T00:00:00",
            "planned_end_at": f"{max(finishes)}T00:00:00",
            "last_scheduled_at": "",
            "data_date": "",
            "critical_path_type": "",
            "default_percent_complete_type": "",
        },
        "summary": {
            "activity_count": len(activities),
            "wbs_count": 0,
            "relationship_count": 0,
            "assignment_count": 0,
            "cost_loaded_activity_count": 0,
            "activities_without_cost_count": len(activities),
            "labor_assignment_count": 0,
            "labor_loaded_activity_count": 0,
            "target_cost": 0.0,
            "target_labor_hours": 0.0,
            "time_phased_assignment_count": 0,
            "zones": [],
            "status_counts": {"UNKNOWN": len(activities)},
        },
        "warnings": [
            {
                "code": "pdf_requires_review",
                "severity": "warning",
                "message": "PDF extraction is best effort. Confirm every activity, date, zone, weight and relationship before activation.",
                "count": len(activities),
            },
            {
                "code": "pdf_missing_schedule_logic",
                "severity": "warning",
                "message": "Relationships, calendars, cost and manpower were not available from the PDF. An XER remains the preferred source.",
                "count": 0,
            },
        ],
        "calendars": [],
        "activities": activities,
        "relationships": [],
        "assignments": [],
    }
