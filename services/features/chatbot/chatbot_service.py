"""ConScoutAI entrypoint: LLM-selected data tools and evidence-based answers."""
from __future__ import annotations

from typing import Optional

from core.auth_context import AuthenticatedUser, reset_current_user, set_current_user
from services.progress.work_schedule.work_schedule_service import work_schedule_comparison

from .chat_agent import answer_question
from .chat_tools import ProjectDataTools


def process_chat_message(
    *, message: str, tours_collection, floorplans_collection, inspections_collection,
    notifications_collection, current_user: Optional[AuthenticatedUser] = None,
    project_id: str = "", site_name: str = "", tour_id: str = "", screen: str = "",
    project_names: Optional[list[str]] = None, materials_collection=None, history=None,
) -> dict:
    """Keep the client contract while interpreting questions through the model.

    screen/project_names remain accepted for existing clients. They are not
    access grants; the tools resolve projects from authenticated server data.
    """
    if current_user is None:
        raise PermissionError("Authentication required")
    context_token = set_current_user(current_user)
    try:
        data_tools = ProjectDataTools(
            user=current_user, floorplans=floorplans_collection, tours=tours_collection,
            inspections=inspections_collection, notifications=notifications_collection,
            materials=materials_collection, progress_reader=work_schedule_comparison,
        )
        return answer_question(
            message=message, data_tools=data_tools, site_name=site_name or project_id,
            tour_id=tour_id, history=history,
        )
    finally:
        reset_current_user(context_token)
