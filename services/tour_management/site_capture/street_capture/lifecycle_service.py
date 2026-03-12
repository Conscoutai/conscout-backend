import os
import shutil

from core.database import floorplans_collection, tours_collection
from core.config import TOURS_DIR
from services.tour_management.site_capture.shared.node_path_mapper import normalize_node_paths
from services.tour_management.site_capture.shared.storage_service import resolve_storage_key_for_tour


def get_latest_tour_id():
    doc = tours_collection.find_one(sort=[("_id", -1)])
    return {"tour_id": doc["tour_id"] if doc else None}


def list_all_tours():
    tours = []
    for doc in tours_collection.find():
        floorplan = None
        if doc.get("floorplan_id"):
            floorplan = floorplans_collection.find_one({"id": doc.get("floorplan_id")})
        site_name = None
        if floorplan:
            site_name = floorplan.get("site_name") or floorplan.get("dxf_project_id")

        storage_key = resolve_storage_key_for_tour(doc.get("tour_id"), doc)
        nodes = [normalize_node_paths(doc.get("tour_id"), n.copy(), storage_key=storage_key) for n in doc.get("nodes", [])]
        tours.append({
            "tour_id": doc.get("tour_id"),
            "name": doc.get("name"),
            "storage_key": storage_key,
            "floorplan_id": doc.get("floorplan_id"),
            "site_name": site_name,
            "created_at": doc.get("created_at"),
            "nodes": nodes,
            "progress": doc.get("progress"),
            "coverage": doc.get("coverage"),
            "site_objects": doc.get("site_objects", []),
        })
    return {"tours": tours}


def delete_tour(tour_id: str):
    tour_doc = tours_collection.find_one({"tour_id": tour_id})
    result = tours_collection.delete_one({"tour_id": tour_id})

    storage_key = resolve_storage_key_for_tour(tour_id, tour_doc)
    sv_path = os.path.join(TOURS_DIR, storage_key)

    if os.path.exists(sv_path):
        shutil.rmtree(sv_path)

    return {
        "status": "deleted",
        "mongo_deleted": result.deleted_count,
    }

