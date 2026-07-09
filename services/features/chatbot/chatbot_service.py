# Phase 1 chatbot service.
# Routes common Conscout questions to real app data before any LLM/RAG layer.

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Iterable, Optional

from core.auth_context import AuthenticatedUser
from services.progress.work_schedule.work_schedule_service import (
    parse_work_schedule_date,
    work_schedule_comparison,
)


TOUR_WEIGHT = 0.45
ACTIVITY_WEIGHT = 0.40
MATERIAL_WEIGHT = 0.15


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
            "Hi. Ask me about projects, latest updates, progress, tours, comments, inspections, or notifications.",
            intent="greeting",
        )

    if _contains_any(normalized, ["help", "what can you do", "how to use"]):
        return _response(
            "I can answer Phase 1 Conscout questions using your live data: projects, tours, progress, comments, inspections, notifications, and latest updates.",
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

    return _response(
        "I can help with projects, latest updates, progress, tours, comments, inspections, and notifications. Try: 'latest updates', 'list comments', or 'progress summary'.",
        intent="fallback",
    )
