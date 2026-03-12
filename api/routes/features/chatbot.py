# Chatbot routes: handle NLP chat requests.
# Delegates processing to the chatbot service.

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.database import tours_collection
from services.features.chatbot.chatbot_service import process_chat_message

router = APIRouter()


class ChatRequest(BaseModel):
    message: str


@router.post("/chat")
async def chat_endpoint(payload: ChatRequest):
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    return process_chat_message(
        message=payload.message,
        tours_collection=tours_collection
    )
