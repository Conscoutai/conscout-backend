"""One approved progress timeline, retaining the provenance of every observation."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
import math
from typing import Any

from fastapi import HTTPException
from core.database import schedule_updates_collection, schedule_evidence_collection


def resolve_progress(
    activities: list[dict],
    evidence: list[dict],
    updates: list[dict],
    as_of: date,
    timezone_name: str = "UTC",
) -> dict[str, Any]:
    # Local import avoids an analytics/service import cycle.
    from .analytics_service import _project_date

    ids = {str(a["activity_id"]): str(a["activity_internal_id"]) for a in activities}
    events = []
    for raw in evidence:
        if str(raw.get("activity_internal_id")) not in ids.values():
            continue
        # Explicit allowlist: never expose owner fields through public history.
        fields = (
            "evidence_id tour_id tour_name site_name captured_at uploaded_at node_id "
            "node_index total_nodes work_type work_category zone image_url confidence "
            "status suggested_percent approved_percent previous_approved_percent "
            "verified_quantity quantity_unit review_note review_source reviewed_at source_baseline_id "
            "reviewed_by_email rationale activity_internal_id progress_conflict_confirmed"
        )
        item = {key: raw.get(key) for key in fields.split()}
        item["observed_at"] = raw.get("captured_at") or raw.get("observed_at")
        item["_order"] = (
            raw.get("reviewed_at") or raw.get("updated_at") or raw.get("uploaded_at")
        )
        events.append(item)
    for update in updates:
        if update.get("status") != "accepted":
            continue
        reviews = {r["activity_id"]: r for r in update.get("progress_reviews", [])}
        for row in update.get("activities", []):
            key = ids.get(str(row.get("activity_id")))
            if key is None or row.get("matched") is False:
                continue
            review = reviews.get(row["activity_id"], {})
            pct = row.get("reported_percent")
            events.append(
                {
                    "evidence_id": f"scheduleupdate:{update['update_id']}:{row['activity_id']}",
                    "activity_internal_id": key,
                    "tour_id": "",
                    "tour_name": update.get("source_filename", "Client schedule"),
                    "review_source": "client_schedule",
                    "source_baseline_id": update.get("baseline_id", ""),
                    "observed_at": update.get("data_date"),
                    "status": review.get("decision", "approved"),
                    "suggested_percent": pct,
                    "approved_percent": (
                        None if review.get("decision") == "rejected" else pct
                    ),
                    "schedule_status": row.get("reported_status", ""),
                    "reviewed_at": review.get("reviewed_at")
                    or update.get("accepted_at"),
                    "reviewed_by_email": review.get("reviewed_by_email")
                    or update.get("accepted_by_email"),
                    "review_note": review.get(
                        "review_note", "Accepted client schedule update"
                    ),
                    "progress_conflict_confirmed": review.get("decision") == "approved",
                    "_order": review.get("reviewed_at") or update.get("accepted_at"),
                }
            )
    dated = []
    for item in events:
        observed = _project_date(item.get("observed_at"), timezone_name)
        if not observed or observed > as_of:
            continue
        item["observed_at"] = observed.isoformat()
        # Normalize approval timestamps so Mongo datetime and ISO strings sort alike.
        order = item.pop("_order", None)
        if isinstance(order, datetime):
            order = order.replace(tzinfo=order.tzinfo or timezone.utc).timestamp()
        else:
            try:
                stamp = datetime.fromisoformat(str(order).replace("Z", "+00:00"))
                order = stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp()
            except (ValueError, TypeError):
                order = 0
        dated.append((observed, order, str(item.get("evidence_id") or ""), item))
    selected, values, history, timeline = {}, {}, defaultdict(list), {}
    # Reconcile by progress date, even when an old XER is accepted today.
    # Otherwise a previously saved manual reduction escapes comparison with
    # the earlier completed schedule observation.
    for observed, _, _, item in sorted(dated, key=lambda e: e[:3]):
        key = str(item["activity_internal_id"])
        previous = selected.get(key)
        pct = item.get("approved_percent")
        valid = isinstance(pct, (int, float)) and math.isfinite(pct) and 0 <= pct <= 100
        candidate = item.get("status") == "approved" and valid
        older = previous is not None and item["observed_at"] < previous["observed_at"]
        conflict = (
            candidate
            and not older
            and previous is not None
            and (
                pct < previous["approved_percent"]
                or (
                    item["observed_at"] == previous["observed_at"]
                    and pct != previous["approved_percent"]
                )
            )
            and not item.get("progress_conflict_confirmed")
        )
        item["progress_conflict"] = bool(conflict)
        if conflict:
            item["status"] = "needs_review"
            item["suggested_percent"] = pct
            item["approved_percent"] = None
            item["previous_approved_percent"] = previous["approved_percent"]
            item["rationale"] = (
                "This update lowers progress or gives a different value for the same date. Check it before changing the current progress."
            )
        elif candidate and not older:
            selected[key] = item
            values[key] = float(pct)
        elif item.get("status") == "approved" and not valid:
            item["status"] = "needs_review"
            item["approved_percent"] = None
            item["rationale"] = (
                "This schedule update has no usable progress percentage."
            )
        history[key].append(item)
    for key, items in history.items():
        current = selected.get(key)
        for item in items:
            item["is_current_progress"] = item is current
            if (
                item.get("progress_conflict")
                and current
                and item["observed_at"] < current["observed_at"]
            ):
                item["status"] = "superseded"
                item["progress_conflict"] = False
        items.sort(key=lambda item: item["observed_at"], reverse=True)
    # The curve uses the same reconciled history as the current value.
    # Unconfirmed reductions never appear as completed progress in either.
    historical_values = {}
    for observed, _, _, item in sorted(dated, key=lambda e: e[:3]):
        if item.get("status") != "approved" or item.get("approved_percent") is None:
            continue
        key = str(item["activity_internal_id"])
        if key:
            historical_values[key] = float(item["approved_percent"])
            timeline[observed.isoformat()] = dict(historical_values)
    for items in history.values():
        for item in items:
            item.pop("activity_internal_id", None)
    return {
        "values": values,
        "selected": selected,
        "history": dict(history),
        "timeline": timeline,
        "latest_date": max((i["observed_at"] for i in selected.values()), default=""),
    }


def load_progress(
    baseline_id: str,
    activities: list[dict],
    as_of: date,
    timezone_name: str,
    baseline: dict | None = None,
) -> dict:
    from .baseline_progress_service import inherited_history

    inherited_evidence, inherited_updates = inherited_history(
        baseline, activities, schedule_evidence_collection, schedule_updates_collection
    )
    return resolve_progress(
        activities,
        inherited_evidence
        + list(
            schedule_evidence_collection.find({"baseline_id": baseline_id}, {"_id": 0})
        ),
        inherited_updates
        + list(
            schedule_updates_collection.find(
                {"baseline_id": baseline_id, "status": "accepted", "removed_at": None},
                {"_id": 0},
            )
        ),
        as_of,
        timezone_name,
    )


def review_schedule_observation(
    evidence_id: str,
    decision: str,
    approved_percent: float | None,
    review_note: str,
    reviewer_email: str,
) -> dict:
    from core.database import schedule_activities_collection

    parts = evidence_id.split(":", 2)
    if len(parts) != 3:
        raise HTTPException(404, "Schedule observation not found")
    _, update_id, activity_id = parts
    update = schedule_updates_collection.find_one(
        {"update_id": update_id, "status": "accepted", "removed_at": None}
    )
    if not update or not schedule_activities_collection.find_one(
        {
            "baseline_id": update["baseline_id"],
            "activity_id": activity_id,
            "removed_at": None,
        }
    ):
        raise HTTPException(404, "Schedule observation not found")
    row = next(
        (
            a
            for a in update.get("activities", [])
            if a.get("activity_id") == activity_id and a.get("matched")
        ),
        None,
    )
    if row is None:
        raise HTTPException(404, "Schedule observation not found")
    if decision == "approved" and (
        row.get("reported_percent") is None
        or approved_percent != row["reported_percent"]
    ):
        raise HTTPException(
            422,
            "Approve the schedule percentage as supplied, or record a separate manual correction.",
        )
    review = {
        "activity_id": activity_id,
        "decision": decision,
        "review_note": review_note,
        "reviewed_by_email": reviewer_email,
        "reviewed_at": datetime.now(timezone.utc),
    }
    schedule_updates_collection.update_one(
        {"update_id": update_id}, {"$push": {"progress_reviews": review}}
    )
    return {"status": decision, "evidence": {"evidence_id": evidence_id, **review}}
