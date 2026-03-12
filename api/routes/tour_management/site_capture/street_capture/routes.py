import time
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from core.database import floorplans_collection, tours_collection
from services.progress.overall.coverage_service import build_coverage_payload
from services.progress.overall.progress_engine import calculate_progress
from services.tour_management.site_capture.street_capture import (
    build_street_capture_graph,
    delete_street_capture_tour,
    get_latest_street_capture_tour_id,
    list_all_street_capture_tours,
    upload_street_capture_image,
)

router = APIRouter(tags=["StreetCapture"])


@router.post("/site-capture/street-capture/upload/{tour_id}")
@router.post("/upload-streetview/{tour_id}")
async def upload_street_capture(
    tour_id: str,
    image: UploadFile = File(...),
    tour_name: str = Form(""),
    floorplan_id: Optional[str] = Form(None),
):
    return upload_street_capture_image(
        tour_id=tour_id,
        image=image,
        tour_name=tour_name,
        floorplan_id=floorplan_id,
    )


@router.get("/site-capture/street-capture/graph")
@router.get("/streetview-graph")
def street_capture_graph(tour_id: str):
    return build_street_capture_graph(tour_id)


@router.get("/site-capture/street-capture/latest-tour")
@router.get("/latest-tour")
def latest_street_capture_tour():
    return get_latest_street_capture_tour_id()


@router.get("/site-capture/street-capture/all-tours")
@router.get("/all-streetview-tours")
def all_street_capture_tours():
    return list_all_street_capture_tours()


@router.delete("/site-capture/street-capture/delete/{tour_id}")
@router.delete("/delete-streetview-tour/{tour_id}")
def delete_street_capture(tour_id: str):
    return delete_street_capture_tour(tour_id)


@router.post("/site-capture/street-capture/save/{tour_id}")
@router.post("/save-tour/{tour_id}")
async def save_street_capture_tour(tour_id: str, payload: dict):
    tour = tours_collection.find_one({"tour_id": tour_id})
    if not tour:
        raise HTTPException(404, "Tour not found")

    if "coverage" not in tour:
        raise HTTPException(400, "Coverage not generated for this tour")

    floorplan_id = tour.get("floorplan_id")
    floorplan = None
    if floorplan_id:
        floorplan = floorplans_collection.find_one({"id": floorplan_id})

    tour_name = payload.get("tour_name")

    floorplan_site_objects = floorplan.get("site_objects", []) if floorplan else []
    tour_site_objects = tour.get("site_objects", [])
    site_objects_for_calc = floorplan_site_objects or tour_site_objects
    tour_for_calc = {**tour, "site_objects": site_objects_for_calc}
    result = calculate_progress(tour_for_calc)

    tours_collection.update_one(
        {"tour_id": tour_id},
        {"$set": {
            "name": tour_name or tour.get("name") or "Street Capture Tour",
            "progress": result["progress"],
            "coverage": tour["coverage"],
            "site_objects": result["site_objects"],
            "status": "completed",
            "updated_at": time.time(),
        }}
    )

    return {
        "message": "Tour finalized",
        "progress": result["progress"],
    }


@router.post("/site-capture/street-capture/coverage/{tour_id}")
@router.post("/tour-coverage/{tour_id}")
async def save_street_capture_coverage(tour_id: str, payload: dict):
    tour = tours_collection.find_one({"tour_id": tour_id})
    if not tour:
        raise HTTPException(status_code=404, detail="Tour not found")

    coverage = payload.get("coverage")
    if not coverage or "points" not in coverage:
        raise HTTPException(status_code=400, detail="coverage.points missing")

    path = coverage["points"]
    if not isinstance(path, list) or len(path) < 2:
        raise HTTPException(status_code=400, detail="Invalid camera path")

    floorplan = None
    if tour.get("floorplan_id"):
        floorplan = floorplans_collection.find_one({"id": tour.get("floorplan_id")})
    if not floorplan:
        raise HTTPException(status_code=400, detail="Tour has no valid floorplan")

    scale = floorplan.get("scale")
    camera_model = payload.get("camera_model", "insta360_x5")
    radius_m = payload.get("camera_radius_m")

    coverage_data = build_coverage_payload(
        path=path,
        camera_model=camera_model,
        floorplan_scale=scale,
        radius_m_override=radius_m,
    )

    tours_collection.update_one(
        {"tour_id": tour_id},
        {"$set": {"coverage": coverage_data}}
    )

    return {"coverage": coverage_data}

