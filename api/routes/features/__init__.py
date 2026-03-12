from fastapi import APIRouter

from .chatbot import router as chatbot_router
from .comments import router as comments_router

router = APIRouter()
router.include_router(comments_router)
router.include_router(chatbot_router)
