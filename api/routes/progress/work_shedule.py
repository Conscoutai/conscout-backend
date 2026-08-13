from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
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
from services.progress.work_schedule.analytics_service import (
    baseline_comparison_or_404,
)
from services.progress.work_schedule.baseline_service import (
    activate_schedule_baseline,
    align_schedule_zones,
    get_schedule_baseline,
    import_schedule_baseline,
    import_schedule_zone_plan,
    list_schedule_baselines,
    confirm_schedule_zone_plan,
    get_schedule_zones,
    update_schedule_zones,
    update_activity_mapping,
)
from services.progress.work_schedule.evidence_service import (
    analyze_tour_schedule,
    review_schedule_evidence,
    record_manual_activity_progress,
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


class ScheduleActivityMappingRequest(BaseModel):
    zone: Optional[str] = None
    work_category: Optional[str] = None
    photo_trackable: Optional[bool] = None
    planned_quantity: Optional[float] = Field(None, ge=0)
    quantity_unit: Optional[str] = None
    weight: Optional[float] = Field(None, ge=0)
    weight_source: Optional[str] = None
    mapping_status: Optional[str] = None


class ScheduleEvidenceReviewRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    approved_percent: Optional[float] = Field(None, ge=0, le=100)
    verified_quantity: Optional[float] = Field(None, ge=0)
    note: str = ""


class ScheduleZonePoint(BaseModel):
    x: float
    y: float


class ScheduleZonePayload(BaseModel):
    name: str
    points: List[ScheduleZonePoint]


class ScheduleZonesRequest(BaseModel):
    zones: List[ScheduleZonePayload]


class ScheduleZoneConfirmationRequest(BaseModel):
    zone_plan_id: str = ""
    version: Optional[int] = Field(None, ge=1)
    floorplan_loaded: bool = False
    note: str = Field("", max_length=500)


class ScheduleZoneAlignmentRequest(BaseModel):
    zone_plan_id: str = ""
    version: Optional[int] = Field(None, ge=1)
    source_points: List[ScheduleZonePoint] = Field(..., min_items=3, max_items=3)
    floorplan_points: List[ScheduleZonePoint] = Field(..., min_items=3, max_items=3)


class ManualActivityProgressRequest(BaseModel):
    observed_at: str
    approved_percent: Optional[float] = Field(None, ge=0, le=100)
    verified_quantity: Optional[float] = Field(None, ge=0)
    note: str = ""


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
    comparison["prediction_notification_sync"] = (
        _best_effort_prediction_notification_sync(
            project_id,
        )
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


@router.post("/projects/{project_id}/schedule-baselines")
async def upload_schedule_baseline(
    project_id: str,
    file: UploadFile = File(...),
    timezone_name: str = Form("UTC", alias="timezone"),
    activate: bool = Form(False),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    filename = file.filename or "baseline.xer"
    return import_schedule_baseline(
        project_ref=project_id,
        filename=filename,
        raw_bytes=await file.read(),
        timezone_name=timezone_name,
        activate=activate,
    )


@router.get("/projects/{project_id}/schedule-baselines")
def project_schedule_baselines(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    return list_schedule_baselines(project_id)


@router.get("/schedule-baselines/{baseline_id}")
def schedule_baseline_detail(
    baseline_id: str,
    include_activities: bool = True,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    return get_schedule_baseline(
        baseline_id=baseline_id, include_activities=include_activities
    )


@router.post("/projects/{project_id}/schedule-baselines/{baseline_id}/activate")
def activate_project_schedule_baseline(
    project_id: str,
    baseline_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return activate_schedule_baseline(project_ref=project_id, baseline_id=baseline_id)


@router.patch("/schedule-baselines/{baseline_id}/activities/{activity_id}/mapping")
def patch_schedule_activity_mapping(
    baseline_id: str,
    activity_id: str,
    payload: ScheduleActivityMappingRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return update_activity_mapping(
        baseline_id=baseline_id,
        activity_id=activity_id,
        updates=payload.dict(exclude_none=True),
    )


@router.get("/projects/{project_id}/schedule-analytics")
def project_schedule_analytics(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    return baseline_comparison_or_404(project_id)


@router.post("/tours/{tour_id}/schedule-analysis")
def analyze_tour_against_schedule(
    tour_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return analyze_tour_schedule(tour_id)


@router.patch("/schedule-evidence/{evidence_id}")
def review_activity_evidence(
    evidence_id: str,
    payload: ScheduleEvidenceReviewRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return review_schedule_evidence(
        evidence_id=evidence_id,
        decision=payload.decision,
        approved_percent=payload.approved_percent,
        verified_quantity=payload.verified_quantity,
        review_note=payload.note,
        reviewer_user_id=current_user.user_id,
        reviewer_email=current_user.email,
    )


@router.get("/projects/{project_id}/schedule-zones")
def project_schedule_zones(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    return get_schedule_zones(project_id)


@router.post("/projects/{project_id}/schedule-zones/import")
async def import_project_schedule_zones(
    project_id: str,
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    filename = file.filename or "zone-plan.pdf"
    return import_schedule_zone_plan(
        project_ref=project_id,
        filename=filename,
        raw_bytes=await file.read(),
    )


@router.put("/projects/{project_id}/schedule-zones")
def put_project_schedule_zones(
    project_id: str,
    payload: ScheduleZonesRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return update_schedule_zones(
        project_ref=project_id,
        zones=[zone.dict() for zone in payload.zones],
    )


@router.post("/projects/{project_id}/schedule-zones/confirm")
def confirm_project_schedule_zones(
    project_id: str,
    payload: ScheduleZoneConfirmationRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return confirm_schedule_zone_plan(
        project_ref=project_id,
        reviewer_user_id=current_user.user_id,
        reviewer_email=current_user.email,
        expected_zone_plan_id=payload.zone_plan_id,
        expected_version=payload.version,
        floorplan_loaded=payload.floorplan_loaded,
        note=payload.note,
    )


@router.post("/projects/{project_id}/schedule-zones/align")
def align_project_schedule_zones(
    project_id: str,
    payload: ScheduleZoneAlignmentRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return align_schedule_zones(
        project_ref=project_id,
        expected_zone_plan_id=payload.zone_plan_id,
        expected_version=payload.version,
        source_points=[point.dict() for point in payload.source_points],
        floorplan_points=[point.dict() for point in payload.floorplan_points],
    )


@router.post("/schedule-baselines/{baseline_id}/activities/{activity_id}/progress")
def post_manual_activity_progress(
    baseline_id: str,
    activity_id: str,
    payload: ManualActivityProgressRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return record_manual_activity_progress(
        baseline_id=baseline_id,
        activity_id=activity_id,
        observed_at=payload.observed_at,
        approved_percent=payload.approved_percent,
        verified_quantity=payload.verified_quantity,
        note=payload.note,
        reviewer_user_id=current_user.user_id,
        reviewer_email=current_user.email,
    )
