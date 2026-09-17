"""Keep progress provenance across explicitly activated baseline revisions."""

from collections import Counter
from copy import deepcopy

from core.database import schedule_baselines_collection, schedule_activities_collection


def carry_plan(source, old_activities, new_activities):
    old_counts = Counter(a.get("activity_id") for a in old_activities)
    new_counts = Counter(a.get("activity_id") for a in new_activities)
    old = {a.get("activity_id"): a for a in old_activities}
    matched, renamed = [], []
    for row in new_activities:
        aid = row.get("activity_id")
        previous = old.get(aid)
        if (
            not aid
            or old_counts[aid] != 1
            or new_counts[aid] != 1
            or previous.get("task_type", "") != row.get("task_type", "")
        ):
            continue
        matched.append(aid)
        if previous.get("activity_name") != row.get("activity_name"):
            renamed.append(aid)
    return {
        "baseline_id": source["baseline_id"],
        "activity_ids": sorted(matched),
        "matched_count": len(matched),
        "new_or_changed_count": len(new_activities) - len(matched),
        "removed_count": len(set(old) - set(new_counts)),
        "renamed_activity_ids": sorted(renamed),
    }


def inherited_sources(baseline):
    """Yield ancestor baselines and the IDs allowed through every revision.

    Links are immutable once a baseline is first activated. Never match by P6
    internal IDs (which may be reused), cross projects, or traverse a cycle.
    """
    if not baseline:
        return
    project_id = baseline.get("project_id")
    seen = {baseline.get("baseline_id")}
    allowed = None
    current = baseline
    while current.get("progress_parent"):
        link = current["progress_parent"]
        source_id = link.get("baseline_id")
        ids = set(link.get("activity_ids") or [])
        allowed = ids if allowed is None else allowed & ids
        if not source_id or source_id in seen or not allowed:
            return
        source = schedule_baselines_collection.find_one(
            {
                "baseline_id": source_id,
                "project_id": project_id,
            }
        )
        if not source or source.get("project_id") != project_id:
            return
        seen.add(source_id)
        yield source, allowed.copy()
        current = source


def inherited_history(baseline, activities, evidence_collection, updates_collection):
    target = {a["activity_id"]: a["activity_internal_id"] for a in activities}
    evidence, updates = [], []
    for source, allowed in inherited_sources(baseline):
        source_id = source["baseline_id"]
        rows = list(
            schedule_activities_collection.find({"baseline_id": source_id}, {"_id": 0})
        )
        remap = {
            a["activity_internal_id"]: target[a["activity_id"]]
            for a in rows
            if a.get("activity_id") in allowed and a["activity_id"] in target
        }
        for raw in evidence_collection.find({"baseline_id": source_id}, {"_id": 0}):
            if raw.get("activity_internal_id") not in remap:
                continue
            row = deepcopy(raw)
            row["activity_internal_id"] = remap[raw["activity_internal_id"]]
            row["source_baseline_id"] = source_id
            evidence.append(row)
        for raw in updates_collection.find(
            {"baseline_id": source_id, "status": "accepted"}, {"_id": 0}
        ):
            update = deepcopy(raw)
            update["activities"] = [
                a
                for a in update.get("activities", [])
                if a.get("activity_id") in allowed and a.get("activity_id") in target
            ]
            updates.append(update)
    return evidence, updates
