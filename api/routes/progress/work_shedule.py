from typing import List, Literal, Optional

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
from services.progress.work_schedule.work_schedule_notification_service import (
    sync_schedule_delay_notifications as sync_schedule_delay_notifications_service,
)
from services.progress.prediction_notification_service import (
    sync_prediction_notifications as sync_prediction_notifications_service,
)

router = APIRouter(tags=["WorkSchedule"])


def _best_effort_schedule_notification_sync(
    project_id: str,
    current_user: Optional[AuthenticatedUser] = None,
) -> dict:
    try:
        result = sync_schedule_delay_notifications_service(
            project_id=project_id,
            current_user=current_user,
        )
        return {
            "status": "synced",
            "created_count": int(result.get("created_count") or 0),
            "updated_count": int(result.get("updated_count") or 0),
            "resolved_count": int(result.get("resolved_count") or 0),
        }
    except Exception as error:
        return {
            "status": "skipped",
            "detail": str(error),
        }


def _best_effort_prediction_notification_sync(
    project_id: str,
    current_user: Optional[AuthenticatedUser] = None,
) -> dict:
    try:
        result = sync_prediction_notifications_service(
            project_id=project_id,
            current_user=current_user,
        )
        return {
            "status": "synced",
            "created_count": int(result.get("created_count") or 0),
            "updated_count": int(result.get("updated_count") or 0),
            "resolved_count": int(result.get("resolved_count") or 0),
        }
    except Exception as error:
        return {
            "status": "skipped",
            "detail": str(error),
        }


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


class WorkScheduleNotificationSyncRequest(BaseModel):
    project_id: str

    @validator("project_id")
    def _project_required(cls, value: str):
        if not value or not value.strip():
            raise ValueError("project_id is required")
        return value.strip()


# Saves a work schedule (manual/csv source with activities).
@router.post("/work-schedules")
def save_work_schedule(
    payload: WorkScheduleRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    save_result = save_work_schedule_service(
        project_id=payload.project_id,
        source=payload.source,
        activities=[activity.dict() for activity in payload.activities],
    )
    return {
        **save_result,
        "notification_sync": _best_effort_schedule_notification_sync(
            payload.project_id,
            current_user=current_user,
        ),
        "prediction_notification_sync": _best_effort_prediction_notification_sync(
            payload.project_id,
            current_user=current_user,
        ),
    }


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
    comparison = work_schedule_comparison_service(project_id)
    comparison["notification_sync"] = _best_effort_schedule_notification_sync(
        project_id,
    )
    comparison["prediction_notification_sync"] = _best_effort_prediction_notification_sync(
        project_id,
    )
    return comparison


@router.post("/work-schedules/notifications/sync")
def sync_work_schedule_notifications(
    payload: WorkScheduleNotificationSyncRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return sync_schedule_delay_notifications_service(
        project_id=payload.project_id,
        current_user=current_user,
    )
