# World coordinate utilities: mapping helpers.
# Used by visualization and coordinate transforms.

import math

def detection_to_world_xy(node, det, distance_m=10.0):
    """
    Approximate world XY for a detection using pano yaw.
    distance_m: average distance to palm (can be tuned)
    """

    base_yaw = node.get("camera_yaw")
    if base_yaw is None:
        base_yaw = 0.0
    base_yaw = float(base_yaw)

    det_yaw = det.get("yaw")
    if det_yaw is None:
        det_yaw = 0.0
    det_yaw = float(det_yaw)

    x = node.get("x")
    y = node.get("y")
    if x is None or y is None:
        return None

    yaw_rad = math.radians((base_yaw + det_yaw) % 360)

    dx = distance_m * math.sin(yaw_rad)
    dy = distance_m * math.cos(yaw_rad)

    return {
        "world_x": float(x) + dx,
        "world_y": float(y) + dy,
    }
