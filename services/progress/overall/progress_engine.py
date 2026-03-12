# Progress engine: compute progress metrics for tours.
# Used by tour progress endpoints.

import time
from collections import Counter
from typing import Dict, List


# =========================================================
# GEOMETRY HELPERS (READ-ONLY)
# =========================================================

def point_in_polygon(x: float, y: float, poly: List[Dict]) -> bool:
    """
    Ray casting algorithm
    """
    inside = False
    j = len(poly) - 1

    for i in range(len(poly)):
        xi, yi = poly[i]["x"], poly[i]["y"]
        xj, yj = poly[j]["x"], poly[j]["y"]

        intersect = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi + 1e-9) + xi
        )
        if intersect:
            inside = not inside
        j = i

    return inside


# =========================================================
# CORE PROGRESS ENGINE (COVERAGE-DRIVEN MVP)
# =========================================================

def calculate_progress(tour: Dict) -> Dict:
    print("\n================ PROGRESS ENGINE START ================")

    nodes = tour.get("nodes", [])
    site_objects = tour.get("site_objects", [])
    coverage = tour.get("coverage", {})

    corridor = coverage.get("polygon", [])
    radius_px = coverage.get("radius_px")

    print(f"[INFO] Nodes count        : {len(nodes)}")
    print(f"[INFO] Site objects total : {len(site_objects)}")
    print(f"[INFO] Coverage polygon  : {len(corridor)} points")

    if not corridor or not site_objects:
        print("[WARN] Missing coverage polygon or site objects")
        return _empty_progress(site_objects, nodes)

    # -----------------------------------------------------
    # FILTER DXF OBJECTS (PLANNED TRUTH)
    # -----------------------------------------------------
    dxf_objects = [
        {**o, "covered": False, "verified": False}
        for o in site_objects
        if o.get("source") == "DXF"
    ]

    print(f"\n[DXF] Planned objects: {len(dxf_objects)}")

    # -----------------------------------------------------
    # STEP 1: COVERAGE CHECK (POLYGON-BASED)
    # -----------------------------------------------------
    covered_objects = []

    for obj in dxf_objects:
        inside = point_in_polygon(obj["x"], obj["y"], corridor)
        obj["covered"] = inside

        if inside:
            covered_objects.append(obj)

    print(f"\n[COVERAGE]")
    print(f"  Objects inside coverage corridor: {len(covered_objects)}")

    # Coverage summary by type (important for debugging)
    covered_type_summary = Counter(o["type"] for o in covered_objects)
    print("\n[COVERAGE BREAKDOWN BY TYPE]")
    for t, c in covered_type_summary.items():
        print(f"  - {t}: {c}")

    # -----------------------------------------------------
    # STEP 2: AI VERIFICATION (COUNT-BASED)
    # -----------------------------------------------------
    verified_count = 0

    print("\n[AI VERIFICATION – COUNT BASED]")
    print("  Rule: detected count = verified planned objects (inside coverage only)")

    for node in nodes:
        node_id = node.get("id", "unknown")
        detections = node.get("detections", [])

        if not detections:
            continue

        print(f"\n[NODE] {node_id}")
        print(f"  Total detections: {len(detections)}")

        # Count detections per class
        det_counts: Dict[str, int] = {}
        for det in detections:
            cls = det.get("class")
            if cls:
                det_counts[cls] = det_counts.get(cls, 0) + 1

        for cls, count in det_counts.items():
            candidates = [
                o
                for o in dxf_objects
                if o["type"] == cls and o["covered"] and not o["verified"]
            ]

            print(
                f"  → {cls}: detected={count}, "
                f"eligible_planned={len(candidates)}"
            )

            # Verify only up to detected count
            for obj in candidates[:count]:
                obj["verified"] = True
                verified_count += 1
                print(f"      ✅ VERIFIED {obj['id']}")

    print(f"\n[INFO] Total verified by AI: {verified_count}")

    # -----------------------------------------------------
    # WRITE BACK FLAGS (SINGLE SOURCE OF TRUTH)
    # -----------------------------------------------------
    dxf_map = {o["id"]: o for o in dxf_objects if "id" in o}

    for obj in site_objects:
        if obj.get("id") in dxf_map:
            obj["covered"] = dxf_map[obj["id"]]["covered"]
            obj["verified"] = dxf_map[obj["id"]]["verified"]

    # -----------------------------------------------------
    # SUMMARY
    # -----------------------------------------------------
    planned = len(dxf_objects)
    covered = sum(o["covered"] for o in dxf_objects)
    verified = sum(o["verified"] for o in dxf_objects)
    percentage = round((verified / covered) * 100, 2) if covered else 0.0

    print("\n[SUMMARY]")
    print(f"  Planned objects : {planned}")
    print(f"  Covered objects : {covered}")
    print(f"  Verified objects: {verified}")
    print(f"  Progress        : {percentage}%")

    print("================ PROGRESS ENGINE END =================\n")

    return {
        "progress": {
            "mode": "DXF_COVERAGE",
            "summary": {
                "planned": planned,
                "covered": covered,
                "verified": verified,
                "percentage": percentage,
            },
            "coverage_radius_px": radius_px,
            "calculated_at": int(time.time() * 1000),
        },
        "nodes": nodes,
        "site_objects": site_objects,
    }


# ---------------------------------------------------------
# EMPTY FALLBACK
# ---------------------------------------------------------
def _empty_progress(site_objects, nodes):
    return {
        "progress": {
            "mode": "DXF_COVERAGE",
            "summary": {
                "planned": len(site_objects),
                "covered": 0,
                "verified": 0,
                "percentage": 0.0,
            },
            "calculated_at": int(time.time() * 1000),
        },
        "nodes": nodes,
        "site_objects": site_objects,
    }
