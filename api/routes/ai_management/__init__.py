from fastapi import APIRouter

from .ai_proxy import router as ai_proxy_router

router = APIRouter()
router.include_router(ai_proxy_router)
