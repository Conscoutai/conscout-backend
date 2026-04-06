from .graph_service import build_streetview_graph
from .ingest_service import upload_streetview_image
from .lifecycle_service import (
    delete_tour,
    get_latest_tour_id,
    list_all_tours,
    rename_tour,
)


def upload_street_capture_image(*, tour_id, image, tour_name, floorplan_id):
    return upload_streetview_image(
        tour_id=tour_id,
        image=image,
        tour_name=tour_name,
        floorplan_id=floorplan_id,
    )


def build_street_capture_graph(tour_id: str):
    return build_streetview_graph(tour_id)


def get_latest_street_capture_tour_id():
    return get_latest_tour_id()


def list_all_street_capture_tours():
    return list_all_tours()


def delete_street_capture_tour(tour_id: str):
    return delete_tour(tour_id)


def rename_street_capture_tour(tour_id: str, new_name: str):
    return rename_tour(tour_id, new_name)

