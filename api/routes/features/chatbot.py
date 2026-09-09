# Chatbot routes: handle NLP chat requests.
# Delegates processing to the chatbot service.

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from core.auth import require_authenticated_user
from core.auth_context import AuthenticatedUser
from core.database import (
    chat_conversations_collection,
    floorplans_collection,
    inspections_collection,
    notifications_collection,
    project_materials_collection,
    tours_collection,
)
from services.features.chatbot.chatbot_service import process_chat_message
from services.features.chatbot.chat_agent import ChatAgentError, HistoryMessage
from services.features.chatbot.chat_conversations import (
    ConversationConflict, ConversationNotFound, ConversationStore,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    message: str = Field(min_length=1, max_length=2000)
    project_id: Optional[str] = Field(default=None, max_length=160)
    site_name: Optional[str] = Field(default=None, max_length=160)
    tour_id: Optional[str] = Field(default=None, max_length=240)
    screen: Optional[str] = Field(default=None, max_length=40)
    project_names: list[str] = Field(default_factory=list, max_length=50)
    conversation_id: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    history: list[HistoryMessage] = Field(default_factory=list, max_length=12)

@router.post("/chat")
def chat_endpoint(
    payload: ChatRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # FastAPI runs this synchronous endpoint in its worker pool, carrying the
    # authentication ContextVar without blocking the event loop on Mongo/Ollama.
    if payload.conversation_id and payload.history:
        raise HTTPException(400, "Use conversation_id or history, not both.")
    store = ConversationStore(chat_conversations_collection, current_user)
    try:
        previous = store.load(payload.conversation_id)
    except ConversationNotFound:
        raise HTTPException(404, "Conversation not found or expired.")
    except Exception as exc:
        logger.warning("chat_history_load_failed type=%s", type(exc).__name__)
        raise HTTPException(503, "Conversation history is temporarily unavailable.") from exc
    history = previous["messages"] if previous else [item.model_dump() for item in payload.history]
    try:
        result = process_chat_message(
            message=payload.message.strip(), tours_collection=tours_collection,
            floorplans_collection=floorplans_collection, inspections_collection=inspections_collection,
            notifications_collection=notifications_collection, materials_collection=project_materials_collection,
            current_user=current_user, project_id=payload.project_id or "",
            site_name=payload.site_name or "", tour_id=payload.tour_id or "",
            screen=payload.screen or "", project_names=payload.project_names or [], history=history,
        )
    except ChatAgentError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    try:
        result["conversation_id"] = store.save(payload.conversation_id, previous, payload.message.strip(), result["answer"], initial_history=history)
        result["conversation_saved"] = True
    except ConversationConflict:
        raise HTTPException(409, "This conversation changed during the request. Please resend your question.")
    except Exception as exc:
        logger.warning("chat_history_save_failed type=%s", type(exc).__name__)
        result["conversation_saved"] = False
    return result
