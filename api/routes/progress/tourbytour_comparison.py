# Comparison routes: change detection and report endpoints.
# Uses the comparison service to build outputs.

# api/comparison.py

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.database import tours_collection
from services.progress.comparison.tourbytour_comparison_service import build_comparison_summary

router = APIRouter(tags=["Compare"])


class CompareRequest(BaseModel):
    tourA: str
    tourB: str


@router.post("/compare-summary")
def compare_summary(data: CompareRequest):
    tour_a = tours_collection.find_one({"tour_id": data.tourA})
    tour_b = tours_collection.find_one({"tour_id": data.tourB})

    if not tour_a or not tour_b:
        raise HTTPException(404, "One or both tours not found")

    print(f"[compare-summary] request tourA={data.tourA} tourB={data.tourB}")
    return build_comparison_summary(tour_a, tour_b)
