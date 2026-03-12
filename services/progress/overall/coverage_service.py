# Coverage service: compute tour coverage geometry.
# Used by streetview endpoints.

import math
import time
from typing import Dict, List, Optional

# ==================================
# CAMERA CONFIG - BACKEND TRUTH
# ==================================

# Fallback radius (pixels) when scale is unavailable.
CAMERA_COVERAGE_RADIUS_PX = 80
DENSIFY_STEP_PX = 10
CIRCLE_SEGMENTS = 32

# Camera coverage radius presets (meters).
DEFAULT_CAMERA_MODEL = "insta360_x5"
CAMERA_RADIUS_METERS = {
    "insta360_x5": 12.0,
}


# ==================================
# GEOMETRY
# ==================================

def build_circle(center, radius, segments=CIRCLE_SEGMENTS):
    cx, cy = center["x"], center["y"]
    return [
        {
            "x": cx + math.cos(2 * math.pi * i / segments) * radius,
            "y": cy + math.sin(2 * math.pi * i / segments) * radius,
        }
        for i in range(segments)
    ]


def cross(o, a, b):
    return (a["x"] - o["x"]) * (b["y"] - o["y"]) - (a["y"] - o["y"]) * (b["x"] - o["x"])


def convex_hull(points):
    if len(points) <= 1:
        return points

    points = sorted(points, key=lambda p: (p["x"], p["y"]))

    lower = []
    for p in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper = []
    for p in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


def densify_path(path, step_px=DENSIFY_STEP_PX):
    dense = []

    for i in range(len(path) - 1):
        p0, p1 = path[i], path[i + 1]
        dx = p1["x"] - p0["x"]
        dy = p1["y"] - p0["y"]
        dist = math.hypot(dx, dy)

        steps = max(int(dist // step_px), 1)

        for s in range(steps):
            t = s / steps
            dense.append({
                "x": p0["x"] + dx * t,
                "y": p0["y"] + dy * t,
            })

    dense.append(path[-1])
    return dense


# ==================================
# COVERAGE - SINGLE SOURCE OF TRUTH
# ==================================

def build_coverage_payload(
    path: List[Dict[str, float]],
    camera_model: str = DEFAULT_CAMERA_MODEL,
    floorplan_scale: Optional[float] = None,
    radius_m_override: Optional[float] = None,
) -> Dict:
    """
    Builds final camera coverage polygon.
    Frontend sends ONLY path.
    """

    if not path or len(path) < 2:
        return {
            "type": "AREA",
            "radius_px": CAMERA_COVERAGE_RADIUS_PX,
            "radius_m": radius_m_override,
            "camera_model": camera_model,
            "polygon": [],
            "generated_at": int(time.time() * 1000),
        }

    dense_path = densify_path(path)
    radius_m = radius_m_override or CAMERA_RADIUS_METERS.get(
        camera_model,
        CAMERA_RADIUS_METERS[DEFAULT_CAMERA_MODEL],
    )
    if floorplan_scale and floorplan_scale > 0:
        radius_px = radius_m / floorplan_scale
    else:
        radius_px = CAMERA_COVERAGE_RADIUS_PX

    all_points = []

    for p in dense_path:
        all_points.extend(
            build_circle(p, radius_px)
        )

    area_polygon = convex_hull(all_points)

    # Debug logs.
    print("[COVERAGE AREA]")
    print(" raw nodes      :", len(path))
    print(" dense nodes    :", len(dense_path))
    # print(" radius_px      :", radius_px)
    # print(" polygon points :", len(area_polygon))

    return {
        "type": "AREA",
        "radius_px": radius_px,
        "radius_m": radius_m,
        "camera_model": camera_model,
        "scale_m_per_px": floorplan_scale,
        "polygon": area_polygon,
        "path": path,
        "generated_at": int(time.time() * 1000),
    }
