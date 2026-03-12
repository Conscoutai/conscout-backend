# Comparison service: generate change summaries and reports.
# Builds report artifacts for the API.

from typing import Dict, List


def _normalize_objects(site_objects: List[Dict]) -> List[Dict]:
    if not site_objects:
        return []

    has_source = any("source" in obj for obj in site_objects)
    if not has_source:
        return site_objects

    return [obj for obj in site_objects if obj.get("source") == "DXF"]


def _summarize(objects: List[Dict]) -> Dict:
    covered = [obj for obj in objects if obj.get("covered") is True]
    verified = [obj for obj in objects if obj.get("verified") is True]

    covered_count = len(covered)
    verified_count = len(verified)
    percentage = round((verified_count / covered_count) * 100, 2) if covered_count else 0.0

    return {
        "planned": len(objects),
        "covered": covered_count,
        "verified": verified_count,
        "percentage": percentage,
    }


def _index_by_id(objects: List[Dict]) -> Dict[str, Dict]:
    return {str(obj.get("id")): obj for obj in objects if obj.get("id") is not None}


def _shared_ids(a_map: Dict[str, Dict], b_map: Dict[str, Dict], key: str) -> List[str]:
    return [
        obj_id
        for obj_id in a_map
        if obj_id in b_map
        and a_map[obj_id].get(key) is True
        and b_map[obj_id].get(key) is True
    ]


def _delta_counts(a_summary: Dict, b_summary: Dict) -> Dict:
    return {
        "covered": b_summary["covered"] - a_summary["covered"],
        "verified": b_summary["verified"] - a_summary["verified"],
        "percentage": round(b_summary["percentage"] - a_summary["percentage"], 2),
    }


def _build_shared_details(
    a_map: Dict[str, Dict],
    b_map: Dict[str, Dict],
    shared_ids: List[str],
) -> List[Dict]:
    details = []
    for obj_id in shared_ids:
        obj_a = a_map.get(obj_id, {})
        obj_b = b_map.get(obj_id, {})
        details.append({
            "id": obj_id,
            "type": obj_a.get("type") or obj_b.get("type"),
            "label": obj_a.get("label") or obj_b.get("label"),
            "x": obj_a.get("x", obj_b.get("x")),
            "y": obj_a.get("y", obj_b.get("y")),
            "covered_a": obj_a.get("covered") is True,
            "covered_b": obj_b.get("covered") is True,
            "verified_a": obj_a.get("verified") is True,
            "verified_b": obj_b.get("verified") is True,
        })
    return details


def build_comparison_summary(tour_a: Dict, tour_b: Dict) -> Dict:
    a_objects = _normalize_objects(tour_a.get("site_objects") or [])
    b_objects = _normalize_objects(tour_b.get("site_objects") or [])

    a_summary = _summarize(a_objects)
    b_summary = _summarize(b_objects)

    a_map = _index_by_id(a_objects)
    b_map = _index_by_id(b_objects)

    shared_covered_ids = _shared_ids(a_map, b_map, "covered")
    shared_verified_ids = _shared_ids(a_map, b_map, "verified")

    verified_in_b_not_a = [
        obj_id
        for obj_id in shared_covered_ids
        if b_map.get(obj_id, {}).get("verified") is True
        and not a_map.get(obj_id, {}).get("verified")
    ]

    verified_in_a_not_b = [
        obj_id
        for obj_id in shared_covered_ids
        if a_map.get(obj_id, {}).get("verified") is True
        and not b_map.get(obj_id, {}).get("verified")
    ]

    print(
        "[compare-summary] "
        f"A covered={a_summary['covered']} verified={a_summary['verified']} | "
        f"B covered={b_summary['covered']} verified={b_summary['verified']} | "
        f"shared covered={len(shared_covered_ids)} verified={len(shared_verified_ids)}"
    )

    return {
        "tourA": {
            "id": tour_a.get("tour_id"),
            "name": tour_a.get("name"),
            "summary": a_summary,
        },
        "tourB": {
            "id": tour_b.get("tour_id"),
            "name": tour_b.get("name"),
            "summary": b_summary,
        },
        "shared": {
            "covered_count": len(shared_covered_ids),
            "verified_count": len(shared_verified_ids),
            "objects": _build_shared_details(a_map, b_map, shared_covered_ids),
        },
        "delta": _delta_counts(a_summary, b_summary),
        "newly_verified": {
            "tourB_over_A": verified_in_b_not_a,
            "tourA_over_B": verified_in_a_not_b,
        },
        "totals": {
            "covered_a": a_summary["covered"],
            "covered_b": b_summary["covered"],
            "verified_a": a_summary["verified"],
            "verified_b": b_summary["verified"],
        },
    }
