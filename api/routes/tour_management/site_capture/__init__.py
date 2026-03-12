from fastapi import APIRouter

from .street_capture import router as street_capture_router
from .indoor_capture import router as indoor_capture_router

router = APIRouter()
router.include_router(street_capture_router)
router.include_router(indoor_capture_router)

