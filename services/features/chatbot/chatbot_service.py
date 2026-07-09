# Phase 1 chatbot service.
# Routes common Conscout questions to real app data before any LLM/RAG layer.

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any, Iterable, Optional

import requests

from core.auth_context import AuthenticatedUser
from services.progress.work_schedule.work_schedule_service import (
    parse_work_schedule_date,
    work_schedule_comparison,
)


TOUR_WEIGHT = 0.45
ACTIVITY_WEIGHT = 0.40
MATERIAL_WEIGHT = 0.15
OLLAMA_TIMEOUT_SECONDS = 12
SUPPORTED_LLM_INTENTS = {
    "project_list",
    "latest_updates",
    "comments",
    "open_issues",
    "progress_summary",
    "inspection_summary",
    "alerts",
    "tour_summary",
    "daily_briefing",
    "pending_items",
    "delay_risk",
    "work_activity_summary",
    "material_summary",
    "site_summary",
    "assigned_to_me",
    "report_summary",
    "unknown",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "null" else text


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", _clean(value).lower())


def _contains_any(message: str, words: Iterable[str]) -> bool:
    return any(word in message for word in words)


def _format_date(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y")
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1000000000000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp).strftime("%d %b %Y")
        except Exception:
            return ""
    text = _clean(value)
    if not text:
        return ""
    return text[:10] if len(text) > 10 else text


def _response(answer: str, *, intent: str, sources: Optional[list[str]] = None) -> dict:
    return {
        "answer": answer,
        "intent": intent,
        "sources": sources or [],
        "timestamp": _now_ms(),
    }


def _project_filter(site_name: str) -> dict:
    return {"$or": [{"site_name": site_name}, {"dxf_project_id": site_name}]}


def _project_name_from_doc(doc: dict) -> str:
    return _clean(doc.get("site_name") or doc.get("dxf_project_id") or doc.get("project_id"))


def _tour_site_name(tour: dict, floorplans_collection) -> str:
    site_name = _clean(
        tour.get("site_name")
        or tour.get("site")
        or tour.get("project_id")
        or tour.get("dxf_project_id")
    )
    if site_name:
        return site_name

    floorplan_id = _clean(tour.get("floorplan_id"))
    if floorplan_id:
        floorplan = floorplans_collection.find_one({"id": floorplan_id}, {"site_name": 1, "dxf_project_id": 1})
        if floorplan:
            return _project_name_from_doc(floorplan)
    return ""


def _list_project_names(floorplans_collection, project_names: list[str]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for name in project_names:
        cleaned = _clean(name)
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            names.append(cleaned)

    for doc in floorplans_collection.find({}, {"site_name": 1, "dxf_project_id": 1}).sort("_id", -1):
        cleaned = _project_name_from_doc(doc)
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            names.append(cleaned)
    return names


def _resolve_site_name(
    *,
    message: str,
    site_name: str,
    project_id: str,
    project_names: list[str],
    floorplans_collection,
    tours_collection,
) -> str:
    explicit = _clean(site_name or project_id)
    if explicit:
        return explicit

    all_project_names = _list_project_names(floorplans_collection, project_names)
    normalized_message = _norm(message)
    for candidate in all_project_names:
        if _norm(candidate) and _norm(candidate) in normalized_message:
            return candidate

    if len(all_project_names) == 1:
        return all_project_names[0]
    return ""


def _fetch_tours(
    *,
    tours_collection,
    floorplans_collection,
    site_name: str = "",
    tour_id: str = "",
    limit: int = 50,
) -> list[dict]:
    if _clean(tour_id):
        tour = tours_collection.find_one({"tour_id": _clean(tour_id)})
        return [tour] if tour else []

    docs = list(tours_collection.find({}).sort("created_at", -1).limit(200))
    if not _clean(site_name):
        return docs[:limit]

    target = _norm(site_name)
    matched: list[dict] = []
    for tour in docs:
        tour_site = _tour_site_name(tour, floorplans_collection)
        if _norm(tour_site) == target:
            matched.append(tour)
        if len(matched) >= limit:
            break
    return matched


def _collect_comments_from_tours(tours: list[dict]) -> list[dict]:
    comments: list[dict] = []
    for tour in tours:
        tour_id = _clean(tour.get("tour_id"))
        tour_name = _clean(tour.get("name") or tour_id or "Tour")
        top_comments = tour.get("comments")
        if isinstance(top_comments, list):
            for comment in top_comments:
                if isinstance(comment, dict):
                    comments.append({**comment, "tour_id": tour_id, "tour_name": tour_name})
        for node in tour.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            for comment in node.get("comments") or []:
                if isinstance(comment, dict):
                    comments.append(
                        {
                            **comment,
                            "tour_id": _clean(comment.get("tour_id") or tour_id),
                            "tour_name": tour_name,
                            "pano_id": _clean(comment.get("pano_id") or node.get("id")),
                        }
                    )
    comments.sort(
        key=lambda item: str(
            item.get("updated_at")
            or item.get("created_at")
            or item.get("createdAt")
            or item.get("date")
            or ""
        ),
        reverse=True,
    )
    return comments


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_percent(value: Any) -> Optional[float]:
    number = _to_float(str(value).replace("%", "").strip() if value is not None else None)
    if number is None:
        return None
    if 0 <= number <= 1:
        number *= 100
    return max(0.0, min(number, 100.0))


def _first_percent(values: Iterable[Any]) -> Optional[float]:
    for value in values:
        parsed = _to_percent(value)
        if parsed is not None:
            return parsed
    return None


def _first_positive_int(values: Iterable[Any]) -> int:
    for value in values:
        number = _to_float(value)
        if number is not None and number > 0:
            return int(number)
    return 0


def _ratio_percent(numerator: int | float, denominator: int | float) -> Optional[float]:
    if denominator <= 0:
        return None
    return max(0.0, min((float(numerator) / float(denominator)) * 100, 100.0))


def _tour_progress_details(tour: dict) -> dict:
    progress = tour.get("progress") if isinstance(tour.get("progress"), dict) else {}
    coverage = tour.get("coverage") if isinstance(tour.get("coverage"), dict) else {}
    summary = progress.get("summary") if isinstance(progress.get("summary"), dict) else {}
    nodes = tour.get("nodes") if isinstance(tour.get("nodes"), list) else []
    node_count = len(nodes)

    planned = _first_positive_int(
        [
            summary.get("planned"),
            coverage.get("planned_count"),
            coverage.get("planned"),
            coverage.get("total_count"),
            coverage.get("total_nodes"),
            coverage.get("total"),
            progress.get("planned_count"),
            progress.get("total_count"),
            tour.get("planned_count"),
            tour.get("total_count"),
            node_count,
        ]
    )
    covered_raw = _first_positive_int(
        [
            summary.get("covered"),
            coverage.get("covered_count"),
            coverage.get("covered"),
            coverage.get("capture_count"),
            coverage.get("captures"),
            coverage.get("visited_count"),
            progress.get("covered_count"),
            progress.get("capture_count"),
            tour.get("covered_count"),
            tour.get("captures"),
            node_count,
        ]
    )
    covered = min(covered_raw, planned) if planned > 0 else covered_raw

    verified_raw = _first_positive_int(
        [
            summary.get("verified"),
            progress.get("verified_count"),
            progress.get("verified"),
            progress.get("done_count"),
            progress.get("completed_count"),
            coverage.get("verified_count"),
            tour.get("verified_count"),
        ]
    )
    verified = min(verified_raw, covered) if covered > 0 else verified_raw

    tour_progress = (
        _first_percent(
            [
                summary.get("percentage"),
                progress.get("percentage"),
                progress.get("percent"),
                progress.get("progress"),
                progress.get("completion"),
                progress.get("completion_percent"),
                tour.get("progress_percent"),
            ]
        )
        or _ratio_percent(verified, covered)
        or 0.0
    )
    coverage_percent = (
        _first_percent(
            [
                coverage.get("covered_percent"),
                coverage.get("coverage_percent"),
                coverage.get("percent"),
                tour.get("coverage_percent"),
            ]
        )
        or _ratio_percent(covered, planned)
        or 0.0
    )

    return {
        "tour_id": _clean(tour.get("tour_id")),
        "tour_name": _clean(tour.get("name") or tour.get("tour_id") or "Tour"),
        "planned": planned,
        "covered": covered,
        "verified": verified,
        "node_count": node_count,
        "tour_progress": round(tour_progress, 2),
        "coverage_percent": round(coverage_percent, 2),
        "has_progress": bool(summary),
    }


def _activity_progress(site_name: str) -> Optional[float]:
    if not _clean(site_name):
        return None
    try:
        comparison = work_schedule_comparison(site_name)
    except Exception:
        return None

    activities = comparison.get("activities") or []
    if not activities:
        return None

    total_weight = 0.0
    weighted_progress = 0.0
    for activity in activities:
        planned_percent = _to_float(activity.get("planned_percent")) or 0.0
        actual_percent = _to_percent(activity.get("actual_percent")) or 0.0
        weight = max(1.0, planned_percent)
        total_weight += weight
        weighted_progress += actual_percent * weight

    if total_weight <= 0:
        return None
    return round(max(0.0, min(weighted_progress / total_weight, 100.0)), 2)


def _parse_project_date(value: Any) -> Optional[datetime]:
    raw = _clean(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return parse_work_schedule_date(raw)


def _material_progress(project: Optional[dict]) -> Optional[float]:
    if not project:
        return None
    material_setup = project.get("progress_materials") or project.get("materials_progress")
    if not isinstance(material_setup, dict):
        return None
    entries = material_setup.get("entries")
    if not isinstance(entries, list) or not entries:
        return None

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    total_score = 0.0
    counted = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        total_quantity = _to_float(entry.get("totalQuantity")) or 0.0
        quantity_used = _to_float(entry.get("quantityUsed")) or 0.0
        delivery_date = _parse_project_date(entry.get("deliveryDate"))
        if total_quantity > 0 and quantity_used >= total_quantity:
            total_score += 100.0
            counted += 1
            continue
        if delivery_date is None:
            continue
        delivery_day = delivery_date.replace(hour=0, minute=0, second=0, microsecond=0)
        delta_days = (delivery_day - today).days
        if delta_days < 0:
            total_score += 35.0
        elif delta_days <= 7:
            total_score += 75.0
        else:
            total_score += 100.0
        counted += 1

    if counted <= 0:
        return None
    return round(max(0.0, min(total_score / counted, 100.0)), 2)


def _activity_name(activity: dict) -> str:
    return _clean(
        activity.get("activity_name")
        or activity.get("name")
        or activity.get("title")
        or activity.get("work_type")
        or "Activity"
    )


def _activity_end_date(activity: dict) -> Optional[datetime]:
    return _parse_project_date(activity.get("end_date") or activity.get("endDate") or activity.get("finish_date"))


def _work_activity_summary(site_name: str) -> Optional[dict]:
    if not _clean(site_name):
        return None
    try:
        comparison = work_schedule_comparison(site_name)
    except Exception:
        return None

    activities = [item for item in comparison.get("activities") or [] if isinstance(item, dict)]
    if not activities:
        return None

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    completed: list[dict] = []
    in_progress: list[dict] = []
    not_started: list[dict] = []
    delayed: list[dict] = []
    critical: list[dict] = []

    for activity in activities:
        status = _norm(activity.get("primary_status") or activity.get("status"))
        actual_percent = _to_percent(activity.get("actual_percent")) or 0.0
        end_date = _activity_end_date(activity)
        is_done = status in {"done", "complete", "completed"} or actual_percent >= 100
        is_delayed = bool(activity.get("is_critical")) or _contains_any(
            status,
            ["critical", "delay", "delayed", "overdue", "late"],
        )
        if end_date is not None and today > end_date.replace(hour=0, minute=0, second=0, microsecond=0) and not is_done:
            is_delayed = True

        if is_done:
            completed.append(activity)
        elif "progress" in status or actual_percent > 0:
            in_progress.append(activity)
        else:
            not_started.append(activity)

        if is_delayed:
            delayed.append(activity)
        if bool(activity.get("is_critical")) or "critical" in status:
            critical.append(activity)

    progress = _activity_progress(site_name)
    return {
        "activities": activities,
        "total": len(activities),
        "completed": completed,
        "in_progress": in_progress,
        "not_started": not_started,
        "delayed": delayed,
        "critical": critical,
        "progress": progress,
    }


def _material_entries(project: Optional[dict]) -> list[dict]:
    if not project:
        return []
    material_setup = project.get("progress_materials") or project.get("materials_progress")
    if not isinstance(material_setup, dict):
        return []
    entries = material_setup.get("entries")
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _material_name(entry: dict) -> str:
    return _clean(
        entry.get("materialName")
        or entry.get("material_name")
        or entry.get("name")
        or entry.get("item")
        or entry.get("description")
        or "Material"
    )


def _overall_progress(
    *,
    tour_progress: Optional[float],
    activity_progress: Optional[float],
    material_progress: Optional[float],
) -> tuple[float, list[dict]]:
    components = [
        {
            "label": "Tour verified progress",
            "value": tour_progress,
            "weight": TOUR_WEIGHT,
        },
        {
            "label": "Work activity progress",
            "value": activity_progress,
            "weight": ACTIVITY_WEIGHT,
        },
        {
            "label": "Material readiness",
            "value": material_progress,
            "weight": MATERIAL_WEIGHT,
        },
    ]
    available = [item for item in components if item["value"] is not None]
    total_weight = sum(float(item["weight"]) for item in available)
    if total_weight <= 0:
        return 0.0, components

    score = 0.0
    resolved: list[dict] = []
    for item in components:
        value = item["value"]
        if value is None:
            resolved.append({**item, "available": False, "contribution": 0.0})
            continue
        effective_weight = float(item["weight"]) / total_weight
        contribution = max(0.0, min(float(value), 100.0)) * effective_weight
        score += contribution
        resolved.append(
            {
                **item,
                "available": True,
                "effective_weight": effective_weight,
                "contribution": contribution,
            }
        )
    return round(max(0.0, min(score, 100.0)), 2), resolved


def _project_doc(floorplans_collection, site_name: str) -> Optional[dict]:
    if not _clean(site_name):
        return None
    return floorplans_collection.find_one(_project_filter(site_name), sort=[("_id", -1)])


def _progress_value(progress: dict, coverage: dict, tour: dict) -> float:
    summary = progress.get("summary") if isinstance(progress.get("summary"), dict) else {}
    candidates = [
        summary.get("percentage"),
        progress.get("percentage"),
        progress.get("percent"),
        progress.get("progress"),
        progress.get("completion"),
        progress.get("completion_percent"),
        coverage.get("coverage_percentage"),
        coverage.get("coverage_percent"),
        tour.get("coverage_percentage"),
    ]
    for value in candidates:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if 0 <= number <= 1:
            number *= 100
        return round(number, 2)
    return 0.0


def _tour_progress_summary(tours: list[dict]) -> tuple[float, int, int]:
    if not tours:
        return 0.0, 0, 0
    values: list[float] = []
    nodes = 0
    for tour in tours:
        progress = tour.get("progress") if isinstance(tour.get("progress"), dict) else {}
        coverage = tour.get("coverage") if isinstance(tour.get("coverage"), dict) else {}
        values.append(_progress_value(progress, coverage, tour))
        nodes += len(tour.get("nodes") or [])
    average = round(sum(values) / len(values), 2) if values else 0.0
    return average, len(tours), nodes


def _line_comment(comment: dict, index: int) -> str:
    title = _clean(
        comment.get("title")
        or comment.get("issue_type")
        or comment.get("problem_description")
        or comment.get("description")
        or comment.get("message")
        or "Untitled comment"
    )
    status = _clean(comment.get("status") or "Open")
    tour_name = _clean(comment.get("tour_name"))
    suffix = f" - {tour_name}" if tour_name else ""
    return f"{index}. {title} ({status}){suffix}"


def _answer_projects(floorplans_collection, project_names: list[str]) -> dict:
    names = _list_project_names(floorplans_collection, project_names)
    if not names:
        return _response("No projects found for your account.", intent="projects")
    shown = ", ".join(names[:8])
    more = f" and {len(names) - 8} more" if len(names) > 8 else ""
    return _response(f"You have {len(names)} project(s): {shown}{more}.", intent="projects")


def _answer_tours(tours: list[dict], site_name: str) -> dict:
    if not tours:
        target = f" in {site_name}" if site_name else ""
        return _response(f"No tours found{target}.", intent="tours")
    names = [_clean(t.get("name") or t.get("tour_id") or "Unnamed tour") for t in tours[:6]]
    target = f" in {site_name}" if site_name else ""
    more = f" Showing latest {len(names)}." if len(tours) > len(names) else ""
    return _response(
        f"Found {len(tours)} tour(s){target}: {', '.join(names)}.{more}",
        intent="tours",
        sources=["tours"],
    )


def _answer_comments(comments: list[dict], site_name: str) -> dict:
    if not comments:
        target = f" in {site_name}" if site_name else ""
        return _response(f"No comments found{target}.", intent="comments", sources=["comments"])
    lines = [_line_comment(comment, index) for index, comment in enumerate(comments[:5], start=1)]
    target = f" in {site_name}" if site_name else ""
    return _response(
        f"Found {len(comments)} comment(s){target}.\n" + "\n".join(lines),
        intent="comments",
        sources=["comments", "tours"],
    )


def _answer_progress(tours: list[dict], site_name: str, floorplans_collection) -> dict:
    if not tours:
        target = f" for {site_name}" if site_name else ""
        return _response(f"No progress data found{target}.", intent="progress", sources=["tours"])

    details = [_tour_progress_details(tour) for tour in tours]
    best_coverage = max(details, key=lambda item: item["coverage_percent"])
    overview = next((item for item in details if item["has_progress"]), details[0])
    project = _project_doc(floorplans_collection, site_name)
    activity = _activity_progress(site_name)
    material = _material_progress(project)
    tour_progress = overview["tour_progress"] if overview["has_progress"] else None
    overall, components = _overall_progress(
        tour_progress=tour_progress,
        activity_progress=activity,
        material_progress=material,
    )

    component_lines = []
    for component in components:
        if component.get("available"):
            component_lines.append(
                f"- {component['label']}: {float(component['value']):.1f}%"
            )
    component_text = "\n" + "\n".join(component_lines) if component_lines else ""

    target = f" in {site_name}" if site_name else ""
    return _response(
        (
            f"Overall progress{target}: {overall:.1f}%.\n"
            f"Best coverage across {len(tours)} tour(s): {best_coverage['coverage_percent']:.1f}% "
            f"({best_coverage['tour_name']}, {best_coverage['covered']}/{best_coverage['planned']} covered).\n"
            f"Capture points checked: {sum(item['node_count'] for item in details)}."
            f"{component_text}"
        ),
        intent="progress",
        sources=["tours", "progress", "work_schedules", "materials"],
    )


def _answer_inspections(inspections_collection, site_name: str) -> dict:
    query = {"site_name": site_name} if _clean(site_name) else {}
    inspections = list(inspections_collection.find(query).sort([("updated_at", -1), ("created_at", -1)]).limit(50))
    if not inspections:
        target = f" in {site_name}" if site_name else ""
        return _response(f"No inspections found{target}.", intent="inspections", sources=["inspections"])
    open_items = [
        item
        for item in inspections
        if _norm(item.get("status")) not in {"completed", "closed", "done"}
    ]
    lines = []
    for index, item in enumerate(inspections[:5], start=1):
        title = _clean(item.get("title") or item.get("inspection_id") or "Inspection")
        status = _clean(item.get("status") or "Pending")
        due_date = _format_date(item.get("due_date"))
        due = f", due {due_date}" if due_date else ""
        lines.append(f"{index}. {title} ({status}{due})")
    target = f" in {site_name}" if site_name else ""
    return _response(
        f"Found {len(inspections)} inspection(s){target}; {len(open_items)} still open.\n" + "\n".join(lines),
        intent="inspections",
        sources=["inspections"],
    )


def _recipient_filter(current_user: Optional[AuthenticatedUser]) -> dict:
    if current_user is None:
        return {}
    return {
        "$or": [
            {"recipient_user_id": current_user.user_id},
            {"recipient_email": current_user.email.strip().lower()},
        ]
    }


def _answer_notifications(notifications_collection, current_user: Optional[AuthenticatedUser]) -> dict:
    notifications = list(
        notifications_collection.find(_recipient_filter(current_user))
        .sort("created_at", -1)
        .limit(10)
    )
    if not notifications:
        return _response("No notifications found.", intent="notifications", sources=["notifications"])
    unread = sum(1 for item in notifications if item.get("is_read") is not True and item.get("status") == "pending")
    lines = []
    for index, item in enumerate(notifications[:5], start=1):
        title = _clean(item.get("title") or item.get("type") or "Notification")
        message = _clean(item.get("message"))
        lines.append(f"{index}. {title}: {message}" if message else f"{index}. {title}")
    return _response(
        f"You have {unread} unread recent notification(s).\n" + "\n".join(lines),
        intent="notifications",
        sources=["notifications"],
    )


def _answer_latest_updates(
    *,
    tours: list[dict],
    comments: list[dict],
    inspections_collection,
    notifications_collection,
    current_user: Optional[AuthenticatedUser],
    site_name: str,
) -> dict:
    lines: list[str] = []
    if tours:
        latest = tours[0]
        name = _clean(latest.get("name") or latest.get("tour_id") or "latest tour")
        date = _format_date(latest.get("created_at"))
        lines.append(f"Latest tour: {name}{f' on {date}' if date else ''}.")
    if comments:
        lines.append(f"Latest comments: {len(comments)} found; top item: {_line_comment(comments[0], 1)[3:]}.")

    query = {"site_name": site_name} if _clean(site_name) else {}
    latest_inspection = inspections_collection.find_one(query, sort=[("updated_at", -1), ("created_at", -1)])
    if latest_inspection:
        title = _clean(latest_inspection.get("title") or "Inspection")
        status = _clean(latest_inspection.get("status") or "Pending")
        lines.append(f"Latest inspection: {title} ({status}).")

    latest_notification = notifications_collection.find_one(
        _recipient_filter(current_user),
        sort=[("created_at", -1)],
    )
    if latest_notification:
        title = _clean(latest_notification.get("title") or latest_notification.get("type"))
        if title:
            lines.append(f"Latest notification: {title}.")

    if not lines:
        target = f" for {site_name}" if site_name else ""
        return _response(f"No recent updates found{target}.", intent="latest_updates")
    target = f" for {site_name}" if site_name else ""
    return _response(
        f"Latest updates{target}:\n" + "\n".join(f"- {line}" for line in lines),
        intent="latest_updates",
        sources=["tours", "comments", "inspections", "notifications"],
    )


def _is_closed_status(value: Any) -> bool:
    status = _norm(value)
    return status in {"closed", "complete", "completed", "done", "resolved"}


def _open_comments(comments: list[dict]) -> list[dict]:
    return [
        comment
        for comment in comments
        if not _is_closed_status(comment.get("status"))
    ]


def _open_inspections(inspections_collection, site_name: str) -> list[dict]:
    query = {"site_name": site_name} if _clean(site_name) else {}
    inspections = list(
        inspections_collection.find(query)
        .sort([("updated_at", -1), ("created_at", -1)])
        .limit(50)
    )
    return [
        item
        for item in inspections
        if not _is_closed_status(item.get("status"))
    ]


def _recent_notifications(notifications_collection, current_user: Optional[AuthenticatedUser]) -> list[dict]:
    return list(
        notifications_collection.find(_recipient_filter(current_user))
        .sort("created_at", -1)
        .limit(20)
    )


def _answer_pending_items(
    *,
    comments: list[dict],
    inspections_collection,
    notifications_collection,
    current_user: Optional[AuthenticatedUser],
    site_name: str,
) -> dict:
    open_comments = _open_comments(comments)
    open_inspections = _open_inspections(inspections_collection, site_name)
    notifications = _recent_notifications(notifications_collection, current_user)
    unread_alerts = [
        item
        for item in notifications
        if item.get("is_read") is not True and _clean(item.get("status") or "pending") == "pending"
    ]

    lines = [
        f"- Open comments: {len(open_comments)}",
        f"- Open inspections: {len(open_inspections)}",
        f"- Unread alerts: {len(unread_alerts)}",
    ]
    for comment in open_comments[:2]:
        lines.append(f"- Comment: {_line_comment(comment, 1)[3:]}")
    for inspection in open_inspections[:2]:
        title = _clean(inspection.get("title") or inspection.get("inspection_id") or "Inspection")
        status = _clean(inspection.get("status") or "Pending")
        due = _format_date(inspection.get("due_date"))
        lines.append(f"- Inspection: {title} ({status}{', due ' + due if due else ''})")

    target = f" in {site_name}" if site_name else ""
    return _response(
        f"Pending items{target}:\n" + "\n".join(lines),
        intent="pending_items",
        sources=["comments", "inspections", "notifications"],
    )


def _answer_delay_risk(
    *,
    inspections_collection,
    notifications_collection,
    current_user: Optional[AuthenticatedUser],
    site_name: str,
) -> dict:
    open_inspections = _open_inspections(inspections_collection, site_name)
    risky_inspections = [
        item
        for item in open_inspections
        if _contains_any(_norm(item.get("status")), ["overdue", "delay", "late"])
    ]
    notifications = _recent_notifications(notifications_collection, current_user)
    risk_notifications = [
        item
        for item in notifications
        if _contains_any(
            _norm(f"{item.get('title')} {item.get('message')} {item.get('type')}"),
            ["delay", "overdue", "behind", "critical", "warning", "risk"],
        )
    ]
    activity = _work_activity_summary(site_name)
    delayed_activities = activity["delayed"] if activity else []

    lines = [
        f"- Risk alerts: {len(risk_notifications)}",
        f"- Overdue/delayed inspections: {len(risky_inspections)}",
        f"- Delayed/critical work activities: {len(delayed_activities)}",
    ]
    for activity_item in delayed_activities[:3]:
        name = _activity_name(activity_item)
        actual = _to_percent(activity_item.get("actual_percent"))
        progress = f", {actual:.0f}%" if actual is not None else ""
        lines.append(f"- Work activity: {name}{progress}")
    for item in risk_notifications[:4]:
        title = _clean(item.get("title") or item.get("type") or "Alert")
        message = _clean(item.get("message"))
        lines.append(f"- {title}: {message}" if message else f"- {title}")

    target = f" in {site_name}" if site_name else ""
    return _response(
        f"Delay/risk summary{target}:\n" + "\n".join(lines),
        intent="delay_risk",
        sources=["inspections", "notifications", "work_schedules"],
    )


def _answer_daily_briefing(
    *,
    tours: list[dict],
    comments: list[dict],
    inspections_collection,
    notifications_collection,
    current_user: Optional[AuthenticatedUser],
    site_name: str,
) -> dict:
    open_comments = _open_comments(comments)
    open_inspections = _open_inspections(inspections_collection, site_name)
    notifications = _recent_notifications(notifications_collection, current_user)
    unread_alerts = [
        item
        for item in notifications
        if item.get("is_read") is not True and _clean(item.get("status") or "pending") == "pending"
    ]
    latest_tour = tours[0] if tours else None

    lines = [
        f"- Open comments: {len(open_comments)}",
        f"- Open inspections: {len(open_inspections)}",
        f"- Unread alerts: {len(unread_alerts)}",
    ]
    if latest_tour:
        tour_name = _clean(latest_tour.get("name") or latest_tour.get("tour_id") or "latest tour")
        tour_date = _format_date(latest_tour.get("created_at"))
        lines.append(f"- Latest tour: {tour_name}{' on ' + tour_date if tour_date else ''}")
    if unread_alerts:
        title = _clean(unread_alerts[0].get("title") or unread_alerts[0].get("type") or "Alert")
        message = _clean(unread_alerts[0].get("message"))
        lines.append(f"- First alert: {title}{': ' + message if message else ''}")

    target = f" for {site_name}" if site_name else ""
    return _response(
        f"Today check{target}:\n" + "\n".join(lines),
        intent="daily_briefing",
        sources=["tours", "comments", "inspections", "notifications"],
    )


def _answer_work_activity(site_name: str) -> dict:
    summary = _work_activity_summary(site_name)
    target = f" for {site_name}" if site_name else ""
    if not summary:
        return _response(f"No work activity schedule found{target}.", intent="work_activity_summary", sources=["work_schedules"])

    lines = [
        f"- Total activities: {summary['total']}",
        f"- Completed: {len(summary['completed'])}",
        f"- In progress: {len(summary['in_progress'])}",
        f"- Not started: {len(summary['not_started'])}",
        f"- Delayed/critical: {len(summary['delayed'])}",
    ]
    if summary.get("progress") is not None:
        lines.insert(1, f"- Activity progress: {float(summary['progress']):.1f}%")

    top_risks = summary["delayed"][:3] or summary["in_progress"][:3] or summary["not_started"][:3]
    for activity in top_risks:
        name = _activity_name(activity)
        status = _clean(activity.get("primary_status") or activity.get("status") or "Pending")
        actual = _to_percent(activity.get("actual_percent"))
        progress = f", {actual:.0f}%" if actual is not None else ""
        lines.append(f"- {name}: {status}{progress}")

    return _response(
        f"Work activity summary{target}:\n" + "\n".join(lines),
        intent="work_activity_summary",
        sources=["work_schedules", "tours"],
    )


def _answer_material_summary(floorplans_collection, site_name: str) -> dict:
    project = _project_doc(floorplans_collection, site_name)
    entries = _material_entries(project)
    target = f" for {site_name}" if site_name else ""
    if not entries:
        return _response(f"No material plan found{target}.", intent="material_summary", sources=["materials"])

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    completed = 0
    delayed: list[dict] = []
    due_soon: list[dict] = []
    remaining: list[dict] = []

    for entry in entries:
        total_quantity = _to_float(entry.get("totalQuantity")) or 0.0
        quantity_used = _to_float(entry.get("quantityUsed")) or 0.0
        delivery_date = _parse_project_date(entry.get("deliveryDate"))
        is_complete = total_quantity > 0 and quantity_used >= total_quantity
        if is_complete:
            completed += 1
            continue
        remaining.append(entry)
        if delivery_date is None:
            continue
        delivery_day = delivery_date.replace(hour=0, minute=0, second=0, microsecond=0)
        delta_days = (delivery_day - today).days
        if delta_days < 0:
            delayed.append(entry)
        elif delta_days <= 7:
            due_soon.append(entry)

    readiness = _material_progress(project)
    lines = [
        f"- Total materials: {len(entries)}",
        f"- Completed/used: {completed}",
        f"- Remaining: {len(remaining)}",
        f"- Delayed: {len(delayed)}",
        f"- Due soon: {len(due_soon)}",
    ]
    if readiness is not None:
        lines.insert(1, f"- Readiness: {readiness:.1f}%")

    for entry in (delayed[:3] or due_soon[:3] or remaining[:3]):
        name = _material_name(entry)
        total_quantity = _to_float(entry.get("totalQuantity")) or 0.0
        quantity_used = _to_float(entry.get("quantityUsed")) or 0.0
        date = _format_date(entry.get("deliveryDate"))
        quantity = f"{quantity_used:g}/{total_quantity:g}" if total_quantity else f"{quantity_used:g} used"
        lines.append(f"- {name}: {quantity}{', delivery ' + date if date else ''}")

    return _response(
        f"Material summary{target}:\n" + "\n".join(lines),
        intent="material_summary",
        sources=["materials"],
    )


def _inspection_counts(inspections_collection, site_name: str) -> tuple[int, int]:
    query = {"site_name": site_name} if _clean(site_name) else {}
    inspections = list(inspections_collection.find(query).limit(200))
    open_count = sum(1 for item in inspections if not _is_closed_status(item.get("status")))
    return len(inspections), open_count


def _unread_notifications_count(notifications_collection, current_user: Optional[AuthenticatedUser]) -> int:
    return sum(
        1
        for item in _recent_notifications(notifications_collection, current_user)
        if item.get("is_read") is not True and _clean(item.get("status") or "pending") == "pending"
    )


def _progress_snapshot(tours: list[dict], floorplans_collection, site_name: str) -> tuple[float, float]:
    if not tours:
        return 0.0, 0.0
    details = [_tour_progress_details(tour) for tour in tours]
    best_coverage = max((item["coverage_percent"] for item in details), default=0.0)
    overview = next((item for item in details if item["has_progress"]), details[0])
    project = _project_doc(floorplans_collection, site_name)
    activity = _activity_progress(site_name)
    material = _material_progress(project)
    tour_progress = overview["tour_progress"] if overview["has_progress"] else None
    overall, _ = _overall_progress(
        tour_progress=tour_progress,
        activity_progress=activity,
        material_progress=material,
    )
    return overall, best_coverage


def _answer_site_summary(
    *,
    tours: list[dict],
    comments: list[dict],
    floorplans_collection,
    inspections_collection,
    notifications_collection,
    current_user: Optional[AuthenticatedUser],
    site_name: str,
) -> dict:
    target = f" for {site_name}" if site_name else ""
    if not site_name and not tours and not comments:
        return _response("Please open or mention a project so I can summarize the site.", intent="site_summary")

    overall, best_coverage = _progress_snapshot(tours, floorplans_collection, site_name)
    total_inspections, open_inspections = _inspection_counts(inspections_collection, site_name)
    open_comments = _open_comments(comments)
    unread_alerts = _unread_notifications_count(notifications_collection, current_user)
    activity = _work_activity_summary(site_name)
    project = _project_doc(floorplans_collection, site_name)
    material = _material_progress(project)

    lines = [
        f"- Overall progress: {overall:.1f}%",
        f"- Best tour coverage: {best_coverage:.1f}%",
        f"- Tours: {len(tours)}",
        f"- Open comments: {len(open_comments)}",
        f"- Open inspections: {open_inspections}/{total_inspections}",
        f"- Unread alerts: {unread_alerts}",
    ]
    if activity:
        lines.append(f"- Work activities delayed/critical: {len(activity['delayed'])}")
    if material is not None:
        lines.append(f"- Material readiness: {material:.1f}%")

    return _response(
        f"Site summary{target}:\n" + "\n".join(lines),
        intent="site_summary",
        sources=["tours", "comments", "inspections", "notifications", "work_schedules", "materials"],
    )


def _matches_current_user(value: Any, current_user: Optional[AuthenticatedUser]) -> bool:
    if current_user is None:
        return False
    target = _norm(value)
    if not target:
        return False
    email = _norm(current_user.email)
    name = _norm(current_user.name)
    email_prefix = email.split("@", 1)[0] if email else ""
    user_id = _norm(current_user.user_id)
    candidates = {item for item in [email, name, email_prefix, user_id] if item}
    return target in candidates


def _answer_assigned_to_me(
    *,
    comments: list[dict],
    inspections_collection,
    notifications_collection,
    current_user: Optional[AuthenticatedUser],
    site_name: str,
) -> dict:
    if current_user is None:
        return _response("Please sign in to see items assigned to you.", intent="assigned_to_me")

    my_comments = [
        comment
        for comment in _open_comments(comments)
        if _matches_current_user(
            comment.get("assigned_to")
            or comment.get("assignedTo")
            or comment.get("assigned_to_detail"),
            current_user,
        )
    ]
    query = {"site_name": site_name} if _clean(site_name) else {}
    inspections = list(inspections_collection.find(query).sort([("updated_at", -1), ("created_at", -1)]).limit(100))
    my_inspections = [
        item
        for item in inspections
        if not _is_closed_status(item.get("status")) and _matches_current_user(item.get("assigned_to"), current_user)
    ]
    my_alerts = [
        item
        for item in _recent_notifications(notifications_collection, current_user)
        if item.get("is_read") is not True and _clean(item.get("status") or "pending") == "pending"
    ]

    lines = [
        f"- Comments assigned to you: {len(my_comments)}",
        f"- Inspections assigned to you: {len(my_inspections)}",
        f"- Unread alerts for you: {len(my_alerts)}",
    ]
    for comment in my_comments[:2]:
        lines.append(f"- Comment: {_line_comment(comment, 1)[3:]}")
    for inspection in my_inspections[:2]:
        title = _clean(inspection.get("title") or inspection.get("inspection_id") or "Inspection")
        due = _format_date(inspection.get("due_date"))
        lines.append(f"- Inspection: {title}{', due ' + due if due else ''}")

    target = f" in {site_name}" if site_name else ""
    return _response(
        f"Assigned to you{target}:\n" + "\n".join(lines),
        intent="assigned_to_me",
        sources=["comments", "inspections", "notifications"],
    )


def _answer_report_summary(
    *,
    tours: list[dict],
    comments: list[dict],
    floorplans_collection,
    inspections_collection,
    notifications_collection,
    current_user: Optional[AuthenticatedUser],
    site_name: str,
) -> dict:
    overall, best_coverage = _progress_snapshot(tours, floorplans_collection, site_name)
    total_inspections, open_inspections = _inspection_counts(inspections_collection, site_name)
    open_comments = _open_comments(comments)
    unread_alerts = _unread_notifications_count(notifications_collection, current_user)
    activity = _work_activity_summary(site_name)
    project = _project_doc(floorplans_collection, site_name)
    material = _material_progress(project)
    target = f" for {site_name}" if site_name else ""

    lines = [
        f"- Overall progress: {overall:.1f}%",
        f"- Best coverage: {best_coverage:.1f}%",
        f"- Tours completed: {len(tours)}",
        f"- Open comments/issues: {len(open_comments)}",
        f"- Open inspections: {open_inspections}/{total_inspections}",
        f"- Unread alerts: {unread_alerts}",
    ]
    if activity:
        lines.append(
            f"- Work activities: {len(activity['completed'])} done, "
            f"{len(activity['in_progress'])} in progress, {len(activity['delayed'])} delayed"
        )
    if material is not None:
        lines.append(f"- Material readiness: {material:.1f}%")

    if open_comments or open_inspections or (activity and activity["delayed"]):
        lines.append("- Next action: close open issues and delayed activities first.")
    else:
        lines.append("- Next action: continue scheduled capture and verification.")

    return _response(
        f"Project report summary{target}:\n" + "\n".join(lines),
        intent="report_summary",
        sources=["tours", "comments", "inspections", "notifications", "work_schedules", "materials"],
    )


def _ollama_enabled() -> bool:
    return os.getenv("CHAT_INTENT_PROVIDER", "").strip().lower() == "ollama"


def _classify_intent_with_ollama(
    *,
    message: str,
    site_name: str,
    project_names: list[str],
) -> str:
    if not _ollama_enabled():
        return "unknown"

    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip().rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "llama3.2:3b").strip() or "llama3.2:3b"
    prompt = (
        "You classify Conscout construction app chatbot messages.\n"
        "Return only JSON with one field: intent.\n"
        "Allowed intents: project_list, latest_updates, comments, open_issues, "
        "progress_summary, inspection_summary, alerts, tour_summary, daily_briefing, "
        "pending_items, delay_risk, work_activity_summary, material_summary, "
        "site_summary, assigned_to_me, report_summary, unknown.\n"
        "Rules:\n"
        "- pending_items: pending/open/remaining/action items.\n"
        "- daily_briefing: what should I check today, daily summary, today's priorities.\n"
        "- delay_risk: delayed, overdue, behind, risk, critical, warning.\n"
        "- work_activity_summary: work activities, schedule activities, activity progress/status.\n"
        "- material_summary: materials, material readiness, quantity used, delivery, shortage.\n"
        "- site_summary: full site/project health or site overview.\n"
        "- assigned_to_me: my tasks, assigned to me, my action items.\n"
        "- report_summary: client report, project report, management summary.\n"
        "- alerts: alerts, notifications, reminders.\n"
        "- inspection_summary: checklist or inspection questions.\n"
        "- comments/open_issues: comments, issues, snags.\n"
        "- progress_summary: progress, coverage, completion percent.\n"
        "- latest_updates: recent/latest/today updates.\n"
        f"Current site: {site_name or 'unknown'}.\n"
        f"Projects: {', '.join(project_names[:20]) or 'unknown'}.\n"
        f"Message: {message}\n"
    )

    try:
        response = requests.post(
            f"{base_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "options": {
                    "temperature": 0,
                    "num_predict": 80,
                },
            },
            timeout=OLLAMA_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        raw = _clean(payload.get("response"))
        parsed = json.loads(raw)
        intent = _clean(parsed.get("intent")).lower()
        return intent if intent in SUPPORTED_LLM_INTENTS else "unknown"
    except Exception:
        return "unknown"


def _route_intent(
    *,
    intent: str,
    tours: list[dict],
    comments: list[dict],
    floorplans_collection,
    inspections_collection,
    notifications_collection,
    current_user: Optional[AuthenticatedUser],
    site_name: str,
    project_names: list[str],
) -> Optional[dict]:
    if intent == "project_list":
        return _answer_projects(floorplans_collection, project_names)
    if intent == "latest_updates":
        return _answer_latest_updates(
            tours=tours,
            comments=comments,
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=site_name,
        )
    if intent in {"comments", "open_issues"}:
        return _answer_comments(comments, site_name)
    if intent == "progress_summary":
        return _answer_progress(tours, site_name, floorplans_collection)
    if intent == "inspection_summary":
        return _answer_inspections(inspections_collection, site_name)
    if intent == "alerts":
        return _answer_notifications(notifications_collection, current_user)
    if intent == "tour_summary":
        return _answer_tours(tours, site_name)
    if intent == "pending_items":
        return _answer_pending_items(
            comments=comments,
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=site_name,
        )
    if intent == "delay_risk":
        return _answer_delay_risk(
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=site_name,
        )
    if intent == "daily_briefing":
        return _answer_daily_briefing(
            tours=tours,
            comments=comments,
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=site_name,
        )
    if intent == "work_activity_summary":
        return _answer_work_activity(site_name)
    if intent == "material_summary":
        return _answer_material_summary(floorplans_collection, site_name)
    if intent == "site_summary":
        return _answer_site_summary(
            tours=tours,
            comments=comments,
            floorplans_collection=floorplans_collection,
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=site_name,
        )
    if intent == "assigned_to_me":
        return _answer_assigned_to_me(
            comments=comments,
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=site_name,
        )
    if intent == "report_summary":
        return _answer_report_summary(
            tours=tours,
            comments=comments,
            floorplans_collection=floorplans_collection,
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=site_name,
        )
    return None


def process_chat_message(
    *,
    message: str,
    tours_collection,
    floorplans_collection,
    inspections_collection,
    notifications_collection,
    current_user: Optional[AuthenticatedUser] = None,
    project_id: str = "",
    site_name: str = "",
    tour_id: str = "",
    screen: str = "",
    project_names: Optional[list[str]] = None,
) -> dict:
    raw_message = _clean(message)
    normalized = _norm(raw_message)
    project_names = project_names or []

    if normalized in {"hi", "hello", "hey", "hai"}:
        return _response(
            "Hi. Ask me about projects, progress, tours, comments, inspections, alerts, work activities, materials, or site reports.",
            intent="greeting",
        )

    if _contains_any(normalized, ["help", "what can you do", "how to use"]):
        return _response(
            "I can answer Conscout questions using live data: projects, tours, progress, comments, inspections, alerts, work activities, materials, assigned tasks, and reports.",
            intent="help",
        )

    resolved_site = _resolve_site_name(
        message=raw_message,
        site_name=site_name,
        project_id=project_id,
        project_names=project_names,
        floorplans_collection=floorplans_collection,
        tours_collection=tours_collection,
    )
    tours = _fetch_tours(
        tours_collection=tours_collection,
        floorplans_collection=floorplans_collection,
        site_name=resolved_site,
        tour_id=tour_id,
    )
    comments = _collect_comments_from_tours(tours)

    if _contains_any(normalized, ["what should i check", "check today", "today priority", "today priorities", "daily briefing"]):
        return _answer_daily_briefing(
            tours=tours,
            comments=comments,
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=resolved_site,
        )

    if _contains_any(normalized, ["pending", "open item", "open items", "remaining", "need attention", "action item", "action items"]):
        return _answer_pending_items(
            comments=comments,
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=resolved_site,
        )

    if _contains_any(normalized, ["delay", "delayed", "overdue", "behind", "risk", "critical", "warning"]):
        return _answer_delay_risk(
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=resolved_site,
        )

    if _contains_any(normalized, ["assigned to me", "my task", "my tasks", "my action", "for me"]):
        return _answer_assigned_to_me(
            comments=comments,
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=resolved_site,
        )

    if _contains_any(normalized, ["work activity", "work activities", "activity status", "activity update", "schedule activity", "schedule activities"]):
        return _answer_work_activity(resolved_site)

    if _contains_any(normalized, ["material", "materials", "quantity", "delivery", "shortage", "readiness"]):
        return _answer_material_summary(floorplans_collection, resolved_site)

    if _contains_any(normalized, ["site summary", "site overview", "project summary", "project overview", "site health", "project health"]):
        return _answer_site_summary(
            tours=tours,
            comments=comments,
            floorplans_collection=floorplans_collection,
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=resolved_site,
        )

    if _contains_any(normalized, ["report", "client summary", "management summary", "status summary"]):
        return _answer_report_summary(
            tours=tours,
            comments=comments,
            floorplans_collection=floorplans_collection,
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=resolved_site,
        )

    if _contains_any(normalized, ["project", "site"]) and _contains_any(normalized, ["list", "show", "my", "all", "how many"]):
        return _answer_projects(floorplans_collection, project_names)

    if _contains_any(normalized, ["latest", "update", "recent", "today", "this week", "happened"]):
        return _answer_latest_updates(
            tours=tours,
            comments=comments,
            inspections_collection=inspections_collection,
            notifications_collection=notifications_collection,
            current_user=current_user,
            site_name=resolved_site,
        )

    if _contains_any(normalized, ["comment", "issue", "snag", "remark"]):
        return _answer_comments(comments, resolved_site)

    if _contains_any(normalized, ["inspection", "inspect", "checklist"]):
        return _answer_inspections(inspections_collection, resolved_site)

    if _contains_any(normalized, ["progress", "coverage", "complete", "completion", "percent", "%"]):
        return _answer_progress(tours, resolved_site, floorplans_collection)

    if _contains_any(normalized, ["tour", "capture", "panorama", "pano"]):
        return _answer_tours(tours, resolved_site)

    if _contains_any(normalized, ["notification", "alert", "unread", "reminder"]):
        return _answer_notifications(notifications_collection, current_user)

    llm_intent = _classify_intent_with_ollama(
        message=raw_message,
        site_name=resolved_site,
        project_names=project_names,
    )
    llm_response = _route_intent(
        intent=llm_intent,
        tours=tours,
        comments=comments,
        floorplans_collection=floorplans_collection,
        inspections_collection=inspections_collection,
        notifications_collection=notifications_collection,
        current_user=current_user,
        site_name=resolved_site,
        project_names=project_names,
    )
    if llm_response is not None:
        llm_response["intent_source"] = "ollama"
        return llm_response

    return _response(
        "I can help with projects, latest updates, progress, tours, comments, inspections, notifications, work activities, materials, site summaries, assigned tasks, and reports.",
        intent="fallback",
    )
