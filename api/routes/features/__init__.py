from fastapi import APIRouter

from .comments import router as comments_router

router = APIRouter()
router.include_router(comments_router)
