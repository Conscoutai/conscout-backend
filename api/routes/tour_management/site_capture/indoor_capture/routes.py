from fastapi import APIRouter, File, HTTPException, UploadFile

from services.tour_management.site_capture.indoor_capture import (
    save_indoor_capture_tour_metadata,
    stitch_indoor_capture_panoramas,
    upload_indoor_capture_video,
)

router = APIRouter(tags=["IndoorCapture"])


@router.post("/site-capture/indoor-capture/upload-video/{tour_id}")
@router.post("/site-capture/indoor-capture/upload-video/{tour_id}/node_index={node_index}")
async def upload_indoor_capture_video_file(
    tour_id: str,
    file: UploadFile = File(...),
    node_index: int = 0,
):
    if node_index < 0:
        raise HTTPException(status_code=400, detail="node_index must be >= 0")
    return upload_indoor_capture_video(tour_id=tour_id, file=file, node_index=node_index)


@router.post("/site-capture/indoor-capture/stitch-panoramas/{tour_id}")
async def stitch_indoor_capture_multi(tour_id: str, payload: dict):
    node_count = payload.get("node_count", 1)
    try:
        parsed_count = int(node_count)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="node_count must be an integer")
    return stitch_indoor_capture_panoramas(tour_id=tour_id, node_count=parsed_count)


@router.post("/site-capture/indoor-capture/save/{tour_id}")
@router.post("/save-indoor-tour/{tour_id}")
async def save_indoor_capture_tour(tour_id: str, payload: dict):
    return save_indoor_capture_tour_metadata(tour_id=tour_id, payload=payload)

