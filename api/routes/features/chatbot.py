# Chatbot routes: handle NLP chat requests.
# Delegates processing to the chatbot service.

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import require_authenticated_user
from core.auth_context import AuthenticatedUser
from core.database import (
    floorplans_collection,
    inspections_collection,
    notifications_collection,
    tours_collection,
)
from services.features.chatbot.chatbot_service import process_chat_message

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    project_id: Optional[str] = None
    site_name: Optional[str] = None
    tour_id: Optional[str] = None
    screen: Optional[str] = None
    project_names: list[str] = Field(default_factory=list)

    class Config:
        extra = "allow"


@router.post("/chat")
async def chat_endpoint(
    payload: ChatRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    return process_chat_message(
        message=payload.message,
        tours_collection=tours_collection,
        floorplans_collection=floorplans_collection,
        inspections_collection=inspections_collection,
        notifications_collection=notifications_collection,
        current_user=current_user,
        project_id=payload.project_id or "",
        site_name=payload.site_name or "",
        tour_id=payload.tour_id or "",
        screen=payload.screen or "",
        project_names=payload.project_names or [],
    )
