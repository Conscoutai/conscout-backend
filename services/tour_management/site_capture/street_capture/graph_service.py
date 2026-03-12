from fastapi import HTTPException

from core.database import floorplans_collection, tours_collection
from utils.geo import haversine
from services.tour_management.site_capture.shared.node_path_mapper import normalize_node_paths
from services.tour_management.site_capture.shared.storage_service import resolve_storage_key_for_tour


def build_streetview_graph(tour_id: str):
    tour = tours_collection.find_one({"tour_id": tour_id})
    if not tour or "nodes" not in tour:
        return {"nodes": [], "edges": []}

    nodes = tour["nodes"]

    fp = None
    if tour.get("floorplan_id"):
        fp = floorplans_collection.find_one({"id": tour.get("floorplan_id")})
    if not fp:
        raise HTTPException(400, "Tour has no valid floorplan")
    width, height = fp["bounds"]["width"], fp["bounds"]["height"]

    located_nodes = [
        n for n in nodes
        if isinstance(n.get("x"), (int, float)) and isinstance(n.get("y"), (int, float))
    ]

    if located_nodes:
        xs = [n["x"] for n in located_nodes]
        ys = [n["y"] for n in located_nodes]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
    else:
        min_x = min_y = 0
        max_x = width
        max_y = height

    range_x = max(max_x - min_x, 1)
    range_y = max(max_y - min_y, 1)

    normalized = []
    for n in nodes:
        has_xy = isinstance(n.get("x"), (int, float)) and isinstance(n.get("y"), (int, float))
        normalized.append({
            **n,
            "px": ((n["x"] - min_x) / range_x * width) if has_xy else None,
            "py": ((n["y"] - min_y) / range_y * height) if has_xy else None,
        })

    edges = []
    for i in range(len(normalized)):
        for j in range(i + 1, len(normalized)):
            try:
                if not all(
                    isinstance(normalized[idx].get(key), (int, float))
                    for idx in (i, j)
                    for key in ("lat", "lon")
                ):
                    continue
                d = haversine(
                    normalized[i]["lat"], normalized[i]["lon"],
                    normalized[j]["lat"], normalized[j]["lon"],
                )
                if d < 8:
                    edges.append([normalized[i]["id"], normalized[j]["id"]])
            except Exception:
                pass

    storage_key = resolve_storage_key_for_tour(tour_id, tour)
    normalized = [normalize_node_paths(tour_id, n.copy(), storage_key=storage_key) for n in normalized]

    return {
        "name": tour.get("name", "Street"),
        "storage_key": storage_key,
        "nodes": normalized,
        "edges": edges,
    }

