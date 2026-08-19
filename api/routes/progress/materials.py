from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field

from core.auth import ensure_admin_user, require_authenticated_user
from core.auth_context import AuthenticatedUser
from services.progress.materials.material_service import (
    confirm_material_document,
    get_material_document,
    get_material_ledger,
    get_material_summary,
    list_material_documents,
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
