from typing import List, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, validator

from core.auth import ensure_admin_user, require_authenticated_user
from core.auth_context import AuthenticatedUser
from services.progress.work_schedule.work_schedule_service import (
    latest_work_schedule as latest_work_schedule_service,
    list_work_schedules as list_work_schedules_service,
    save_work_schedule as save_work_schedule_service,
    work_schedule_comparison as work_schedule_comparison_service,
)

router = APIRouter(tags=["WorkSchedule"])


class WorkScheduleActivity(BaseModel):
    activity_id: str
    activity_name: str
    zone: str
    start_date: str
    end_date: str
    planned_percent: float = Field(..., ge=0, le=100)

    @validator("activity_id", "activity_name", "zone", "start_date", "end_date")
    def _required(cls, value: str):
        if not value or not value.strip():
            raise ValueError("Field is required")
        return value.strip()


class WorkScheduleRequest(BaseModel):
    project_id: str
    source: Literal["manual", "csv"]
    activities: List[WorkScheduleActivity]

    @validator("project_id")
    def _project_required(cls, value: str):
        if not value or not value.strip():
            raise ValueError("project_id is required")
        return value.strip()

    @validator("activities")
    def _activities_required(cls, value: List[WorkScheduleActivity]):
        if not value:
            raise ValueError("activities is required")
        return value


# Saves a work schedule (manual/csv source with activities).
@router.post("/work-schedules")
def save_work_schedule(
    payload: WorkScheduleRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return save_work_schedule_service(
        project_id=payload.project_id,
        source=payload.source,
        activities=[activity.dict() for activity in payload.activities],
    )


# Lists schedules for a project.
@router.get("/work-schedules")
def list_work_schedules(project_id: str):
    return list_work_schedules_service(project_id)


# Returns latest schedule for a project
@router.get("/work-schedules/latest")
def latest_work_schedule(project_id: str):
    return latest_work_schedule_service(project_id)


# Returns comparison output for schedules of a project.
@router.get("/work-schedules/comparison")
def work_schedule_comparison(project_id: str):
    return work_schedule_comparison_service(project_id)
