from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, Field

from core.auth import ensure_admin_user, require_authenticated_user
from core.auth_context import AuthenticatedUser
from services.progress.materials.material_service import (
    confirm_material_document,
    discard_material_document,
    get_material_document,
    get_material_ledger,
    get_material_summary,
    list_material_documents,
    reprocess_material_document,
    reset_material_documents,
    restore_material_document,
    update_material_document_review,
    upload_material_document,
    void_material_document,
)


router = APIRouter(tags=["Materials"])

MaterialDocumentType = Literal[
    "auto",
    "boq",
    "weekly_report",
    "purchase_order",
    "delivery_note",
    "customer_shipment",
    "mir_grn",
    "progress_invoice",
]


class MaterialDocumentReviewRequest(BaseModel):
    header: dict[str, Any] = Field(default_factory=dict)
    lines: list[dict[str, Any]] = Field(default_factory=list)
    note: str = Field("", max_length=1000)


class MaterialDocumentVoidRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


class MaterialDocumentControlRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


class MaterialResetRequest(BaseModel):
    scope: Literal["pending", "transactions", "all"]
    reason: str = Field(..., min_length=3, max_length=1000)
    confirmation: str = Field(..., min_length=3, max_length=100)


@router.post("/projects/{project_id}/material-documents")
async def upload_project_material_document(
    project_id: str,
    file: UploadFile = File(...),
    document_type: MaterialDocumentType = Form("auto"),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return upload_material_document(
        project_ref=project_id,
        filename=file.filename or "material-document.pdf",
        raw_bytes=await file.read(),
        document_type=document_type,
        user=current_user,
    )


@router.get("/projects/{project_id}/material-documents")
def project_material_documents(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    del current_user
    return list_material_documents(project_id)


@router.post("/projects/{project_id}/material-documents/reset")
def reset_project_material_documents(
    project_id: str,
    payload: MaterialResetRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return reset_material_documents(
        project_ref=project_id,
        scope=payload.scope,
        reason=payload.reason,
        confirmation=payload.confirmation,
        user=current_user,
    )


@router.get("/projects/{project_id}/material-documents/{document_id}")
def project_material_document_detail(
    project_id: str,
    document_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    del current_user
    return get_material_document(project_id, document_id)


@router.patch("/projects/{project_id}/material-documents/{document_id}")
def review_project_material_document(
    project_id: str,
    document_id: str,
    payload: MaterialDocumentReviewRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return update_material_document_review(
        project_ref=project_id,
        document_id=document_id,
        header=payload.header,
        lines=payload.lines,
        review_note=payload.note,
        user=current_user,
    )


@router.delete("/projects/{project_id}/material-documents/{document_id}")
def discard_project_material_document(
    project_id: str,
    document_id: str,
    payload: MaterialDocumentControlRequest | None = Body(default=None),
    reason: str | None = Query(default=None, min_length=3, max_length=1000),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return discard_material_document(
        project_ref=project_id,
        document_id=document_id,
        reason=(payload.reason if payload else reason)
        or "Discarded pending material upload",
        user=current_user,
    )


@router.post("/projects/{project_id}/material-documents/{document_id}/confirm")
def confirm_project_material_document(
    project_id: str,
    document_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return confirm_material_document(
        project_ref=project_id,
        document_id=document_id,
        user=current_user,
    )


@router.post("/projects/{project_id}/material-documents/{document_id}/void")
def void_project_material_document(
    project_id: str,
    document_id: str,
    payload: MaterialDocumentVoidRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return void_material_document(
        project_ref=project_id,
        document_id=document_id,
        reason=payload.reason,
        user=current_user,
    )


@router.post("/projects/{project_id}/material-documents/{document_id}/reprocess")
def reprocess_project_material_document(
    project_id: str,
    document_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return reprocess_material_document(
        project_ref=project_id,
        document_id=document_id,
        user=current_user,
    )


@router.post("/projects/{project_id}/material-documents/{document_id}/restore")
def restore_project_material_document(
    project_id: str,
    document_id: str,
    payload: MaterialDocumentControlRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return restore_material_document(
        project_ref=project_id,
        document_id=document_id,
        reason=payload.reason,
        user=current_user,
    )


@router.get("/projects/{project_id}/material-ledger")
def project_material_ledger(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    del current_user
    return get_material_ledger(project_id)


@router.get("/projects/{project_id}/material-summary")
def project_material_summary(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    del current_user
    return get_material_summary(project_id)
