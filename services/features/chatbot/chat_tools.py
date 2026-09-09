"""Read-only, permission-scoped data tools. Models never supply Mongo queries."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.auth_context import AuthenticatedUser


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListProjects(ToolInput):
    pass


class ProjectInput(ToolInput):
    project: str = Field(min_length=1, max_length=160)


class ProgressInput(ProjectInput):
    query: str = Field(default="", max_length=120)
    status: str = Field(default="", max_length=80)
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=10, ge=1, le=20)


class ReadRecords(ProjectInput):
    dataset: Literal["tours", "comments", "inspections", "notifications", "materials"]
    query: str = Field(default="", max_length=120)
    status: str = Field(default="", max_length=80)
    tour_id: str = Field(default="", max_length=240)
    date_field: Literal["created_at", "updated_at", "due_date"] = "updated_at"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    offset: int = Field(default=0, ge=0, le=200)
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_date(cls, value):
        if value is not None:
            datetime.strptime(value, "%Y-%m-%d")
        return value


TOOL_MODELS = {
    "list_projects": ListProjects,
    "read_project_records": ReadRecords,
    "get_project_progress": ProgressInput,
    "get_project_details": ProjectInput,
}
TOOL_DESCRIPTIONS = {
    "list_projects": "List projects the authenticated user can access. Client project names grant no access.",
    "read_project_records": (
        "Read live project records and exact matching count. Choose tours, comments/issues (including panorama comments), "
        "inspections, the user's notifications, or material ledger. query is a literal phrase in text fields; status is "
        "an exact case-insensitive value. Dates are UTC dates, start inclusive and end exclusive. Results are newest "
        "first. Omit tour_id for project-wide questions; use it only for an explicitly requested tour. Paginate with "
        "offset. Limited results are not all records. Use separate calls to combine datasets or projects."
    ),
    "get_project_progress": (
        "Get the project's server-calculated schedule comparison: planned/actual progress, activities, dates and "
        "delays. Filter activity names/descriptions with query or exact status, and "
        "paginate using offset/limit. Project summary remains unfiltered. Capture coverage is not physical construction completion."
    ),
    "get_project_details": "Read project description, location, dates and team members. Use with records to investigate a member's work.",
}
TOOLS = [
    {"type": "function", "function": {"name": name, "description": TOOL_DESCRIPTIONS[name],
     "parameters": model.model_json_schema()}}
    for name, model in TOOL_MODELS.items()
]

# Only business fields are included. Never send entire documents, image payloads,
# signed file URLs, credentials, or account/session records to a model.
RECORD_FIELDS = (
    "id", "_id", "tour_id", "inspection_id", "notification_id", "material_id",
    "name", "title", "description", "message", "text", "body", "content",
    "status", "priority", "category", "type", "created_at", "createdAt", "updated_at",
    "due_date", "date", "assigned_to", "assignedTo", "responsible_party",
    "created_by", "closed_by", "completed_by", "completion_by", "pano_id", "tour_name",
    "is_read", "unit", "quantity", "planned_quantity", "delivered_quantity", "used_quantity",
    "boq_quantity", "approved_quantity", "remaining_quantity", "item_code",
    "planned_qty", "ordered_qty", "delivered_qty", "accepted_qty", "rejected_qty",
    "pending_delivery_qty", "pending_inspection_qty", "over_delivery_qty", "expected_delivery_date",
    "is_overdue", "recalculated_at", "source_document_ids",
)
# A nested assignee must not accidentally include an entire embedded user object.
NESTED_FIELDS = set(RECORD_FIELDS) | {
    "email", "role", "user_id", "summary", "percentage", "percent", "planned",
    "covered", "verified", "actual_percent", "planned_percent", "variance_percent",
    "activity_id", "activity_name", "start_date", "end_date", "primary_status",
    "is_critical", "related_tour_ids", "baseline_id", "version", "data_date",
    "total_activities", "completed_activities", "delayed_activities", "progress",
    "coverage", "coverage_percent", "coverage_percentage", "completion_percent",
    "progress_percent", "total_count", "covered_count", "verified_count",
    "actual", "planned_finish", "forecast_finish", "unit_of_measure",
    "delay_days", "baseline_finish_date", "forecast_finish_date", "delayed_activity_count",
    "critical_activity_count", "needs_review_count", "data_as_of", "last_verified_capture_date",
    "weighting_method", "schedule_planned_percent", "schedule_actual_percent",
    "team_members", "teamMembers", "stakeholders", "stakeholder_emails", "owner_email",
    "location", "address", "site_name", "project_id", "dxf_project_id",
}


def clean_value(value, depth=0):
    if depth > 5:
        return "[nested data omitted]"
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key not in NESTED_FIELDS:
                continue
            if key in {"created_at", "createdAt", "updated_at", "recalculated_at", "due_date"} and isinstance(item, (int, float)) and not isinstance(item, bool):
                try:
                    item = datetime.fromtimestamp(item / 1000 if abs(item) >= 100000000000 else item, timezone.utc)
                except (ValueError, OverflowError, OSError):
                    item = None
            result[key] = clean_value(item, depth + 1)
        return result
    if isinstance(value, list):
        items = [clean_value(item, depth + 1) for item in value[:30]]
        return items + (["[additional items omitted]"] if len(value) > 30 else [])
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text[:1200] + (" [text truncated]" if len(text) > 1200 else "")


class ProjectDataTools:
    def __init__(self, *, user: AuthenticatedUser, floorplans, tours, inspections,
                 notifications, materials=None, progress_reader=None):
        if not user or not user.user_id:
            raise PermissionError("Authentication required")
        self.user = user
        self.floorplans = floorplans
        self.collections = {"tours": tours, "comments": tours, "inspections": inspections,
                            "notifications": notifications, "materials": materials}
        self.progress_reader = progress_reader
        clauses = [{"owner_user_id": user.user_id}]
        if user.email.strip():
            clauses.append({"owner_email": user.email.strip().lower()})
        if user.accessible_floorplan_ids:
            ids = list(user.accessible_floorplan_ids)
            clauses.extend([{"id": {"$in": ids}}, {"floorplan_id": {"$in": ids}}])
        self.owner_filter = {"$or": clauses}
        self._projects = None

    def projects(self):
        if self._projects is None:
            projection = {key: 1 for key in ("id", "site_name", "dxf_project_id", "project_id")}
            docs = list(self.floorplans.find(self.owner_filter, projection, max_time_ms=3000).limit(501))
            if len(docs) > 500:
                raise ValueError("Project directory exceeds the supported size; narrow account access.")
            grouped = {}
            for doc in docs:
                name = str(doc.get("site_name") or doc.get("dxf_project_id") or doc.get("project_id") or "").strip()
                if not name:
                    continue
                entry = grouped.setdefault(name.casefold(), {"name": name, "aliases": set(), "floorplan_ids": set()})
                entry["aliases"].update(str(doc[k]) for k in projection if doc.get(k))
                if doc.get("id"):
                    entry["floorplan_ids"].add(str(doc["id"]))
            self._projects = list(grouped.values())
        return self._projects

    def resolve(self, value):
        key = value.strip().casefold()
        matches = [p for p in self.projects() if key in {v.casefold() for v in p["aliases"]}]
        if len(matches) != 1:
            raise ValueError("Project is unavailable or ambiguous. Select a project from list_projects.")
        return matches[0]

    def execute(self, name, arguments):
        if name not in TOOL_MODELS:
            raise ValueError("Unknown read-only tool")
        arguments = dict(arguments)
        # Some tool-capable models serialize numeric arguments as strings. Parse
        # only integer paging values, then retain normal validation and row caps.
        for field in ("offset", "limit"):
            value = arguments.get(field)
            if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 6:
                arguments[field] = int(value)
        if type(arguments.get("limit")) is int:
            arguments["limit"] = min(arguments["limit"], 20)
        args = TOOL_MODELS[name].model_validate(arguments)
        if name == "list_projects":
            return {"projects": [{"name": p["name"]} for p in self.projects()]}
        project = self.resolve(args.project)
        if name == "get_project_progress":
            return self.progress(project, args)
        if name == "get_project_details":
            return self.details(project)
        return self.records(project, args)

    def details(self, project):
        fields = ("id", "site_name", "description", "location", "address", "start_date", "end_date",
                  "team_members", "teamMembers", "stakeholders", "stakeholder_emails", "owner_email")
        docs = list(self.floorplans.find({"$and": [self.owner_filter,
            {"id": {"$in": sorted(project["floorplan_ids"])}}]}, {key: 1 for key in fields}, max_time_ms=3000).limit(20))
        return {"project": project["name"], "dataset": "project_details", "records": [clean_value(doc) for doc in docs],
                "total_matching": len(project["floorplan_ids"]), "offset": 0, "returned": len(docs),
                "has_more": len(project["floorplan_ids"]) > len(docs)}

    def _scope(self, project, dataset):
        aliases = sorted(project["aliases"])
        project_filter = {"$or": [{field: {"$in": aliases}} for field in
                                  ("site_name", "site", "project_id", "dxf_project_id")] +
                          [{"floorplan_id": {"$in": sorted(project["floorplan_ids"])}}]}
        access_filter = self.owner_filter
        if dataset == "notifications":
            recipients = [{"recipient_user_id": self.user.user_id}]
            if self.user.email.strip():
                recipients.append({"recipient_email": self.user.email.strip().lower()})
            access_filter = {"$or": recipients}
        return {"$and": [access_filter, project_filter]}

    def records(self, project, args):
        collection = self.collections[args.dataset]
        if collection is None:
            raise ValueError("This dataset is not configured")
        scope = self._scope(project, args.dataset)
        if args.tour_id:
            scope = {"$and": [scope, {"tour_id": args.tour_id}]}
        pipeline = [{"$match": scope}]
        if args.dataset == "comments":
            # Flatten both storage locations in Mongo, before counting/pagination.
            pipeline.extend([
                {"$project": {"tour_id": 1, "name": 1, "all_comments": {"$concatArrays": [
                    {"$ifNull": ["$comments", []]},
                    {"$reduce": {"input": {"$ifNull": ["$nodes", []]}, "initialValue": [],
                                 "in": {"$concatArrays": ["$$value", {"$ifNull": ["$$this.comments", []]}]}}},
                ]}}},
                {"$unwind": "$all_comments"},
                {"$replaceRoot": {"newRoot": {"$mergeObjects": ["$all_comments",
                    {"tour_id": "$tour_id", "tour_name": "$name"}]}}},
            ])
        filters = []
        if args.query:
            filters.append({"$or": [{field: {"$regex": re.escape(args.query), "$options": "i"}}
                                    for field in ("name", "title", "description", "message", "body", "text", "content")]})
        if args.status:
            filters.append({"status": {"$regex": "^" + re.escape(args.status) + "$", "$options": "i"}})
        if filters:
            pipeline.append({"$match": {"$and": filters}})
        date_input = "$" + args.date_field
        if args.date_field == "updated_at":
            date_input = {"$ifNull": ["$updated_at", {"$ifNull": ["$recalculated_at", {"$ifNull": ["$created_at", "$createdAt"]}]}]}
        pipeline.append({"$set": {"_chat_date": {"$convert": {
            "input": {"$let": {"vars": {"value": date_input}, "in": {"$cond": [
                {"$isNumber": "$$value"}, {"$cond": [{"$lt": [{"$abs": "$$value"}, 100000000000]},
                    {"$multiply": ["$$value", 1000]}, "$$value"]}, "$$value"]}}},
            "to": "date", "onError": None, "onNull": None}}}})
        bounds = {}
        for key, value in (("$gte", args.start_date), ("$lt", args.end_date)):
            if value:
                bounds[key] = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if args.start_date and args.end_date and args.start_date >= args.end_date:
            raise ValueError("end_date must be after start_date")
        if bounds:
            pipeline.append({"$match": {"_chat_date": bounds}})
        projection = {key: 1 for key in RECORD_FIELDS}
        if args.dataset == "tours":
            projection.update({"progress.summary": 1, "progress.percentage": 1, "coverage": 1,
                               "progress_percent": 1})
        pipeline.append({"$facet": {
            "count": [{"$count": "value"}],
            "records": [{"$sort": {"_chat_date": -1, "_id": -1}}, {"$skip": args.offset},
                        {"$limit": args.limit}, {"$project": projection}],
        }})
        result = next(iter(collection.aggregate(pipeline, maxTimeMS=3000)), {})
        total = (result.get("count") or [{"value": 0}])[0]["value"]
        rows = [clean_value(row) for row in result.get("records", [])]
        if args.dataset == "tours":
            for row in rows:
                summary = (row.get("progress") or {}).get("summary") or {}
                counts = {key: summary.get(key) for key in ("planned", "covered", "verified")}
                if all(type(value) in (int, float) and value >= 0 for value in counts.values()):
                    planned, covered, verified = (counts[key] for key in ("planned", "covered", "verified"))
                    row["tour_metrics"] = {"planned_units": planned, "covered_units": covered, "verified_units": verified,
                        "coverage_percent": round(100 * covered / planned, 2) if planned else None,
                        "verification_percent_of_covered": round(100 * verified / covered, 2) if covered else None,
                        "scope": "Tour coverage and verification; not overall project completion."}
                    row.pop("progress", None)
                    row.pop("coverage", None)
        return {"project": project["name"], "dataset": args.dataset, "total_matching": total,
                "offset": args.offset, "returned": len(rows), "has_more": args.offset + len(rows) < total,
                "filters": {"date_field": args.date_field, **args.model_dump(
                    exclude={"project", "dataset", "offset", "limit"}, exclude_defaults=True)},
                "records": rows}

    def progress(self, project, args):
        if self.progress_reader is None:
            raise ValueError("Progress calculations are not configured")
        # This service uses the same authenticated ContextVar as the rest of the API.
        # Lookup by an authorized floorplan ID avoids ambiguous name-only resolution.
        project_id = next(iter(sorted(project["floorplan_ids"])), project["name"])
        try:
            comparison = self.progress_reader(project_id)
            schedule_available = True
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            comparison = {}
            schedule_available = False
        activities = comparison.get("activities") or []
        matched = [row for row in activities if
                   (not args.query or args.query.casefold() in str(row.get("activity_name") or row.get("name") or row.get("description") or "").casefold()) and
                   (not args.status or args.status.casefold() == str(row.get("primary_status") or row.get("status") or "").casefold())]
        result = {"project": project["name"], "dataset": "schedule_progress",
                  "schedule_available": schedule_available,
                  "summary": clean_value(comparison.get("summary") or {}),
                  "actual_percent": comparison.get("actual_percent"),
                  "activity_count": len(activities),
                  "total_matching_activities": len(matched), "activity_offset": args.offset,
                  "activities": [clean_value(row) for row in matched[args.offset:args.offset + args.limit]],
                  "activities_truncated": args.offset + args.limit < len(matched),
                  "note": "Only recorded or server-calculated values; missing percentages are unknown, not zero."}
        # The caller may retrieve more tour history separately; never force a stale UI tour ID here.
        if not schedule_available:
            result["latest_tour"] = self.records(project, ReadRecords(project=project["name"], dataset="tours", date_field="created_at", limit=1))
        return result


def bounded_result(result, max_chars=10000):
    """Trim whole rows, preserving valid JSON and explicitly signalling truncation."""
    result = dict(result)
    for field in ("records", "activities"):
        if field in result:
            result[field] = list(result[field])
            while len(json.dumps(result, default=str)) > max_chars and result[field]:
                result[field].pop()
                result["context_truncated"] = True
            if field == "records":
                result["returned"] = len(result[field])
                result["has_more"] = result["offset"] + len(result[field]) < result["total_matching"]
    if len(json.dumps(result, default=str)) > max_chars:
        return {"error": "Result is too large; request a smaller page or a narrower query."}
    return result
