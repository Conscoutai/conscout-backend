from fastapi import APIRouter

from .tourbytour_comparison import router as comparison_router
from .work_shedule import router as work_schedule_router
from .materials import router as materials_router
from .budget import router as budget_router

router = APIRouter()
router.include_router(work_schedule_router)
router.include_router(comparison_router)
router.include_router(materials_router)
router.include_router(budget_router)
