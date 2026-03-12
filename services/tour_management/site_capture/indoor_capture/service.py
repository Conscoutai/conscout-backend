from fastapi import UploadFile

from services.tour_management.site_capture.indoor_capture.panorama_service import (
    save_indoor_tour_metadata,
    stitch_indoor_panoramas,
    upload_indoor_video,
)

def upload_indoor_capture_video(tour_id: str, file: UploadFile, node_index: int):
    return upload_indoor_video(tour_id=tour_id, file=file, node_index=node_index)


def stitch_indoor_capture_panoramas(tour_id: str, node_count: int):
    return stitch_indoor_panoramas(tour_id=tour_id, node_count=node_count)


def save_indoor_capture_tour_metadata(tour_id: str, payload: dict):
    return save_indoor_tour_metadata(tour_id=tour_id, payload=payload)

