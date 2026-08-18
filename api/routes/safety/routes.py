from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from core.auth import ensure_admin_user, require_authenticated_user
from core.auth_context import AuthenticatedUser
from services.safety.safety_service import (
    create_analysis_job,
    create_manual_weather,
    create_record,
    delete_record,
    evaluate_geofence,
    finalize_daily_report,
    generate_daily_report,
    get_analysis_job,
    get_config,
    get_weather,
    list_analysis_jobs,
    list_audit_events,
    list_records,
    review_daily_report,
    render_daily_report_pdf,
    run_analysis_job,
    save_record_attachment,
    update_config,
    update_record,
    verify_permit_record,
    build_dashboard,
)


router = APIRouter(tags=["Safety and Manpower"])


class FlexiblePayload(BaseModel):
    record_date: Optional[str] = None
    status: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    severity: Optional[str] = None
    notes: Optional[str] = None
    source: Optional[str] = None
    tour_id: Optional[str] = None
    planned_workers: Optional[int] = Field(default=None, ge=0)
    observed_workers: Optional[int] = Field(default=None, ge=0)
    trade_breakdown: Optional[list[dict[str, Any]]] = None
    point: Optional[dict[str, float]] = None
    polygon: Optional[list[dict[str, float]]] = None
    zone_id: Optional[str] = None
    floor_level: Optional[str] = None
    assigned_to: Optional[str] = None
    due_at: Optional[str] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    controls: Optional[list[dict[str, Any]]] = None
    checklist_items: Optional[list[dict[str, Any]]] = None
    attachments: Optional[list[dict[str, Any]]] = None
    activity_type: Optional[str] = None
    equipment_id: Optional[str] = None
    scaffold_id: Optional[str] = None
    reporter_name: Optional[str] = None

    class Config:
        extra = "allow"


class SafetyConfigPayload(BaseModel):
    wind_warning_kph: Optional[float] = Field(default=None, ge=0)
    wind_stop_kph: Optional[float] = Field(default=None, ge=0)
    heat_warning_c: Optional[float] = None
    heat_stop_c: Optional[float] = None
    rain_warning_mm_h: Optional[float] = Field(default=None, ge=0)
    rain_stop_mm_h: Optional[float] = Field(default=None, ge=0)
    weather_provider: Optional[str] = None
    required_ppe: Optional[list[str]] = None
    timezone: Optional[str] = None
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    daily_report_cutoff: Optional[str] = None
    auto_daily_reports: Optional[bool] = None
    report_recipients: Optional[list[str]] = None
    hazard_categories: Optional[list[str]] = None
    hazard_resolution_hours: Optional[int] = Field(default=None, ge=1)
    permit_types: Optional[list[str]] = None
    required_permit_approvals: Optional[int] = Field(default=None, ge=0)
    shift_names: Optional[list[str]] = None


class AnalysisJobPayload(BaseModel):
    tour_id: str = Field(min_length=1)


class ManualWeatherPayload(BaseModel):
    wind_kph: Optional[float] = Field(default=None, ge=0)
    apparent_temperature_c: Optional[float] = None
    precipitation_mm_h: Optional[float] = Field(default=None, ge=0)
    observed_at: Optional[str] = None
    notes: Optional[str] = None


class ReportGeneratePayload(BaseModel):
    record_date: Optional[str] = None


class GeofenceEvaluationPayload(BaseModel):
    point: dict[str, float]
    entity_type: str = "worker"
    entity_id: str = ""
    create_findings: bool = False


class PermitStartPayload(BaseModel):
    activity_type: Optional[str] = None
    zone_id: Optional[str] = None


def _payload(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


def _ensure_manager(user: AuthenticatedUser) -> None:
    ensure_admin_user(user)


@router.get("/projects/{project_id}/safety/config")
def read_safety_config(project_id: str):
    return get_config(project_id)


@router.put("/projects/{project_id}/safety/config")
def save_safety_config(
    project_id: str,
    payload: SafetyConfigPayload,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _ensure_manager(current_user)
    return update_config(project_id, payload=_payload(payload), user=current_user)


@router.get("/projects/{project_id}/safety/dashboard")
def safety_dashboard(
    project_id: str,
    record_date: str = "",
    shift: str = "",
    tour_id: str = "",
    zone_id: str = "",
):
    return build_dashboard(
        project_id,
        record_date=record_date,
        shift=shift,
        tour_id=tour_id,
        zone_id=zone_id,
    )


def _list(project_id: str, record_type: str, status: str, record_date: str, limit: int):
    return list_records(
        project_id,
        record_type,
        status=status,
        record_date=record_date,
        limit=limit,
    )


@router.get("/projects/{project_id}/safety/workforce-plans")
def workforce_plans(project_id: str, record_date: str = "", limit: int = Query(250, ge=1, le=1000)):
    return _list(project_id, "workforce_plan", "", record_date, limit)


@router.post("/projects/{project_id}/safety/workforce-plans", status_code=201)
def create_workforce_plan(
    project_id: str,
    payload: FlexiblePayload,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _ensure_manager(current_user)
    return create_record(project_id, record_type="workforce_plan", payload=_payload(payload), user=current_user)


@router.put("/projects/{project_id}/safety/workforce-plans/{record_id}")
def update_workforce_plan(
    project_id: str,
    record_id: str,
    payload: FlexiblePayload,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _ensure_manager(current_user)
    return update_record(project_id, record_type="workforce_plan", record_id=record_id, payload=_payload(payload), user=current_user)


@router.get("/projects/{project_id}/safety/workforce-observations")
def workforce_observations(project_id: str, record_date: str = "", limit: int = Query(250, ge=1, le=1000)):
    return _list(project_id, "workforce_observation", "", record_date, limit)


@router.post("/projects/{project_id}/safety/workforce-observations", status_code=201)
def create_workforce_observation(
    project_id: str,
    payload: FlexiblePayload,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _ensure_manager(current_user)
    values = _payload(payload)
    values.setdefault("source", "manual")
    values.setdefault("requires_review", False)
    return create_record(project_id, record_type="workforce_observation", payload=values, user=current_user)


@router.put("/projects/{project_id}/safety/workforce-observations/{record_id}")
def update_workforce_observation(
    project_id: str,
    record_id: str,
    payload: FlexiblePayload,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _ensure_manager(current_user)
    return update_record(project_id, record_type="workforce_observation", record_id=record_id, payload=_payload(payload), user=current_user)


@router.post("/projects/{project_id}/safety/analysis-jobs", status_code=202)
def queue_analysis_job(
    project_id: str,
    payload: AnalysisJobPayload,
    background_tasks: BackgroundTasks,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _ensure_manager(current_user)
    job, created = create_analysis_job(project_id, tour_id=payload.tour_id, user=current_user)
    if created:
        background_tasks.add_task(run_analysis_job, job["job_id"])
    return {"created": created, "job": job}


@router.get("/projects/{project_id}/safety/analysis-jobs")
def analysis_jobs(project_id: str, limit: int = Query(100, ge=1, le=500)):
    return list_analysis_jobs(project_id, limit=limit)


@router.get("/projects/{project_id}/safety/analysis-jobs/{job_id}")
def analysis_job(project_id: str, job_id: str):
    return get_analysis_job(project_id, job_id)


@router.post("/projects/{project_id}/safety/analysis-jobs/{job_id}/retry", status_code=202)
def retry_analysis_job(
    project_id: str,
    job_id: str,
    background_tasks: BackgroundTasks,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _ensure_manager(current_user)
    job = get_analysis_job(project_id, job_id)
    if job.get("status") not in {"failed", "partially_completed"}:
        return {"created": False, "job": job, "detail": "Only failed or partial jobs can be retried"}
    background_tasks.add_task(run_analysis_job, job_id)
    return {"created": True, "job": job}


@router.get("/projects/{project_id}/safety/findings")
def safety_findings(project_id: str, status: str = "", limit: int = Query(250, ge=1, le=1000)):
    return _list(project_id, "safety_finding", status, "", limit)


@router.post("/projects/{project_id}/safety/findings", status_code=201)
def create_safety_finding(
    project_id: str,
    payload: FlexiblePayload,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _ensure_manager(current_user)
    values = _payload(payload)
    values.setdefault("source", "manual")
    values.setdefault("requires_review", True)
    return create_record(project_id, record_type="safety_finding", payload=values, user=current_user)


@router.put("/projects/{project_id}/safety/findings/{record_id}")
def review_safety_finding(
    project_id: str,
    record_id: str,
    payload: FlexiblePayload,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _ensure_manager(current_user)
    values = _payload(payload)
    values["reviewed_by"] = {"user_id": current_user.user_id, "email": current_user.email, "name": current_user.name}
    return update_record(project_id, record_type="safety_finding", record_id=record_id, payload=values, user=current_user)


@router.post("/projects/{project_id}/safety/findings/{record_id}/attachments", status_code=201)
async def add_finding_attachment(
    project_id: str,
    record_id: str,
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    content = await file.read()
    return save_record_attachment(
        project_id,
        record_type="safety_finding",
        record_id=record_id,
        filename=file.filename or "attachment",
        content_type=file.content_type or "application/octet-stream",
        content=content,
        user=current_user,
    )


@router.get("/projects/{project_id}/safety/weather")
def current_weather(project_id: str, refresh: bool = False):
    return get_weather(project_id, refresh=refresh)


@router.post("/projects/{project_id}/safety/weather-observations", status_code=201)
def manual_weather_observation(
    project_id: str,
    payload: ManualWeatherPayload,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _ensure_manager(current_user)
    return create_manual_weather(project_id, payload=_payload(payload), user=current_user)


@router.get("/projects/{project_id}/safety/weather/events")
def weather_events(project_id: str, limit: int = Query(250, ge=1, le=1000)):
    return _list(project_id, "weather_observation", "", "", limit)


@router.put("/projects/{project_id}/safety/weather/events/{record_id}")
def update_weather_event(
    project_id: str,
    record_id: str,
    payload: FlexiblePayload,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _ensure_manager(current_user)
    values = _payload(payload)
    values["acknowledged_by"] = {
        "user_id": current_user.user_id,
        "email": current_user.email,
        "name": current_user.name,
    }
    return update_record(
        project_id,
        record_type="weather_observation",
        record_id=record_id,
        payload=values,
        user=current_user,
    )


@router.post("/projects/{project_id}/safety/geofence/evaluate")
def geofence_evaluation(
    project_id: str,
    payload: GeofenceEvaluationPayload,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    if payload.create_findings:
        _ensure_manager(current_user)
    return evaluate_geofence(
        project_id,
        point=payload.point,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        create_findings=payload.create_findings,
        user=current_user,
    )


def _crud_list(record_type: str):
    def endpoint(project_id: str, status: str = "", limit: int = Query(250, ge=1, le=1000)):
        return _list(project_id, record_type, status, "", limit)
    return endpoint


router.add_api_route("/projects/{project_id}/safety/zones", _crud_list("safety_zone"), methods=["GET"], name="list_safety_zones")
router.add_api_route("/projects/{project_id}/safety/permits", _crud_list("permit"), methods=["GET"], name="list_safety_permits")
router.add_api_route("/projects/{project_id}/safety/check-templates", _crud_list("check_template"), methods=["GET"], name="list_safety_check_templates")
router.add_api_route("/projects/{project_id}/safety/check-runs", _crud_list("check_run"), methods=["GET"], name="list_safety_check_runs")
router.add_api_route("/projects/{project_id}/safety/hazards", _crud_list("hazard"), methods=["GET"], name="list_safety_hazards")
router.add_api_route("/projects/{project_id}/safety/daily-reports", _crud_list("daily_report"), methods=["GET"], name="list_safety_daily_reports")


def _create_managed(project_id: str, record_type: str, payload: FlexiblePayload, user: AuthenticatedUser):
    _ensure_manager(user)
    return create_record(project_id, record_type=record_type, payload=_payload(payload), user=user)


@router.post("/projects/{project_id}/safety/zones", status_code=201)
def create_zone(project_id: str, payload: FlexiblePayload, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    return _create_managed(project_id, "safety_zone", payload, current_user)


@router.put("/projects/{project_id}/safety/zones/{record_id}")
def update_zone(project_id: str, record_id: str, payload: FlexiblePayload, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    _ensure_manager(current_user)
    return update_record(project_id, record_type="safety_zone", record_id=record_id, payload=_payload(payload), user=current_user)


@router.delete("/projects/{project_id}/safety/zones/{record_id}")
def delete_zone(project_id: str, record_id: str, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    _ensure_manager(current_user)
    return delete_record(project_id, record_type="safety_zone", record_id=record_id, user=current_user)


@router.post("/projects/{project_id}/safety/permits", status_code=201)
def create_permit(project_id: str, payload: FlexiblePayload, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    return _create_managed(project_id, "permit", payload, current_user)


@router.put("/projects/{project_id}/safety/permits/{record_id}")
def update_permit(project_id: str, record_id: str, payload: FlexiblePayload, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    _ensure_manager(current_user)
    return update_record(project_id, record_type="permit", record_id=record_id, payload=_payload(payload), user=current_user)


@router.post("/projects/{project_id}/safety/permits/{record_id}/attachments", status_code=201)
async def add_permit_attachment(
    project_id: str,
    record_id: str,
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    content = await file.read()
    return save_record_attachment(
        project_id,
        record_type="permit",
        record_id=record_id,
        filename=file.filename or "attachment",
        content_type=file.content_type or "application/octet-stream",
        content=content,
        user=current_user,
    )


@router.post("/projects/{project_id}/safety/permits/{record_id}/verify-start")
def verify_permit(
    project_id: str,
    record_id: str,
    payload: Optional[PermitStartPayload] = None,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    values = _payload(payload) if payload else {}
    return verify_permit_record(
        project_id,
        record_id,
        user=current_user,
        expected_activity_type=str(values.get("activity_type") or ""),
        expected_zone_id=str(values.get("zone_id") or ""),
    )


@router.post("/projects/{project_id}/safety/check-templates", status_code=201)
def create_check_template(project_id: str, payload: FlexiblePayload, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    return _create_managed(project_id, "check_template", payload, current_user)


@router.put("/projects/{project_id}/safety/check-templates/{record_id}")
def update_check_template(project_id: str, record_id: str, payload: FlexiblePayload, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    _ensure_manager(current_user)
    return update_record(project_id, record_type="check_template", record_id=record_id, payload=_payload(payload), user=current_user)


@router.post("/projects/{project_id}/safety/check-runs", status_code=201)
def create_check_run(project_id: str, payload: FlexiblePayload, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    return create_record(project_id, record_type="check_run", payload=_payload(payload), user=current_user)


@router.put("/projects/{project_id}/safety/check-runs/{record_id}")
def update_check_run(project_id: str, record_id: str, payload: FlexiblePayload, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    return update_record(project_id, record_type="check_run", record_id=record_id, payload=_payload(payload), user=current_user)


@router.post("/projects/{project_id}/safety/check-runs/{record_id}/attachments", status_code=201)
async def add_check_attachment(
    project_id: str,
    record_id: str,
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    content = await file.read()
    return save_record_attachment(
        project_id,
        record_type="check_run",
        record_id=record_id,
        filename=file.filename or "attachment",
        content_type=file.content_type or "application/octet-stream",
        content=content,
        user=current_user,
    )


@router.post("/projects/{project_id}/safety/hazards", status_code=201)
def report_hazard(project_id: str, payload: FlexiblePayload, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    values = _payload(payload)
    values.setdefault("status", "open")
    values.setdefault("source", "mobile")
    return create_record(project_id, record_type="hazard", payload=values, user=current_user)


@router.put("/projects/{project_id}/safety/hazards/{record_id}")
def update_hazard(project_id: str, record_id: str, payload: FlexiblePayload, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    return update_record(project_id, record_type="hazard", record_id=record_id, payload=_payload(payload), user=current_user)


@router.post("/projects/{project_id}/safety/hazards/{record_id}/attachments", status_code=201)
async def add_hazard_attachment(
    project_id: str,
    record_id: str,
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    content = await file.read()
    return save_record_attachment(
        project_id,
        record_type="hazard",
        record_id=record_id,
        filename=file.filename or "attachment",
        content_type=file.content_type or "application/octet-stream",
        content=content,
        user=current_user,
    )


@router.post("/projects/{project_id}/safety/daily-reports/generate", status_code=201)
def generate_report(project_id: str, payload: ReportGeneratePayload, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    _ensure_manager(current_user)
    return generate_daily_report(project_id, record_date=payload.record_date or "", user=current_user)


@router.put("/projects/{project_id}/safety/daily-reports/{record_id}/review")
def review_report(project_id: str, record_id: str, payload: FlexiblePayload, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    _ensure_manager(current_user)
    values = _payload(payload)
    return review_daily_report(
        project_id,
        record_id,
        notes=str(values.get("notes") or values.get("review_notes") or ""),
        user=current_user,
    )


@router.post("/projects/{project_id}/safety/daily-reports/{record_id}/finalize")
def finalize_report(project_id: str, record_id: str, current_user: AuthenticatedUser = Depends(require_authenticated_user)):
    _ensure_manager(current_user)
    return finalize_daily_report(project_id, record_id, user=current_user)


@router.get("/projects/{project_id}/safety/daily-reports/{record_id}/pdf")
def download_report_pdf(project_id: str, record_id: str):
    content, filename = render_daily_report_pdf(project_id, record_id)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/projects/{project_id}/safety/audit-events")
def safety_audit_events(project_id: str, limit: int = Query(100, ge=1, le=500)):
    return list_audit_events(project_id, limit=limit)
