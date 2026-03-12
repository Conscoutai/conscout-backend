from fastapi import APIRouter

from .site_capture import router as site_capture_router


router = APIRouter()
router.include_router(site_capture_router)

