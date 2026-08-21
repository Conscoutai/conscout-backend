from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field

from core.auth import ensure_admin_user, require_authenticated_user
from core.auth_context import AuthenticatedUser
from services.progress.budget.budget_service import (
    activate_boq,
    create_variation,
    decide_invoice,
    get_boq,
    get_budget_workspace,
    get_invoice,
    list_boqs,
    list_invoices,
    list_variations,
    record_payment,
    review_boq,
    review_invoice,
    upload_boq,
    upload_invoice,
    verify_invoice,
)


router = APIRouter(tags=["Budget"])


class BudgetDocumentReviewRequest(BaseModel):
    header: dict[str, Any] = Field(default_factory=dict)
    lines: list[dict[str, Any]] = Field(default_factory=list)
    note: str = Field("", max_length=2000)


class BudgetVariationRequest(BaseModel):
    boq_item_id: str = ""
    reference: str = Field("", max_length=200)
    description: str = Field("", max_length=1000)
    quantity_delta: float = 0
    amount_delta: float = 0
    rate_override: float = 0
    effective_date: str = ""
    approval_date: str = ""
    status: Literal["pending", "approved", "rejected", "voided"] = "approved"
    note: str = Field("", max_length=2000)


class InvoiceDecisionRequest(BaseModel):
    action: Literal["certify", "hold", "request_correction", "reject"]
    note: str = Field("", max_length=2000)
    certified_amount: Optional[float] = Field(None, ge=0)


class InvoicePaymentRequest(BaseModel):
    amount: float = Field(..., gt=0)
    payment_date: str
    reference: str = Field("", max_length=300)
    note: str = Field("", max_length=2000)


@router.get("/projects/{project_id}/budget")
def project_budget_workspace(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    del current_user
    return get_budget_workspace(project_id)


@router.get("/projects/{project_id}/budget/boqs")
def project_budget_boqs(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    del current_user
    return list_boqs(project_id)


@router.post("/projects/{project_id}/budget/boqs")
async def upload_project_budget_boq(
    project_id: str,
    file: UploadFile = File(...),
    revision: str = Form(""),
    currency: str = Form(""),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return upload_boq(
        project_ref=project_id,
        filename=file.filename or "priced-boq.pdf",
        raw_bytes=await file.read(),
        revision=revision,
        currency=currency,
        user=current_user,
    )


@router.get("/projects/{project_id}/budget/boqs/{boq_id}")
def project_budget_boq_detail(
    project_id: str,
    boq_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    del current_user
    return get_boq(project_id, boq_id)


@router.patch("/projects/{project_id}/budget/boqs/{boq_id}")
def review_project_budget_boq(
    project_id: str,
    boq_id: str,
    payload: BudgetDocumentReviewRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return review_boq(
        project_ref=project_id,
        boq_id=boq_id,
        header=payload.header,
        lines=payload.lines,
        note=payload.note,
        user=current_user,
    )


@router.post("/projects/{project_id}/budget/boqs/{boq_id}/activate")
def activate_project_budget_boq(
    project_id: str,
    boq_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return activate_boq(project_ref=project_id, boq_id=boq_id, user=current_user)


@router.get("/projects/{project_id}/budget/variations")
def project_budget_variations(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    del current_user
    return list_variations(project_id)


@router.post("/projects/{project_id}/budget/variations")
def create_project_budget_variation(
    project_id: str,
    payload: BudgetVariationRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return create_variation(
        project_ref=project_id,
        payload=payload.dict(),
        user=current_user,
    )


@router.get("/projects/{project_id}/budget/invoices")
def project_budget_invoices(
    project_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    del current_user
    return list_invoices(project_id)


@router.post("/projects/{project_id}/budget/invoices")
async def upload_project_budget_invoice(
    project_id: str,
    file: UploadFile = File(...),
    invoice_number: str = Form(""),
    invoice_date: str = Form(""),
    billing_start_date: str = Form(""),
    billing_end_date: str = Form(""),
    billing_cutoff_date: str = Form(""),
    currency: str = Form(""),
    retention_percent: float = Form(0),
    advance_recovery_percent: float = Form(0),
    vat_percent: float = Form(0),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return upload_invoice(
        project_ref=project_id,
        filename=file.filename or "payment-application.pdf",
        raw_bytes=await file.read(),
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        billing_start_date=billing_start_date,
        billing_end_date=billing_end_date,
        billing_cutoff_date=billing_cutoff_date,
        currency=currency,
        retention_percent=retention_percent,
        advance_recovery_percent=advance_recovery_percent,
        vat_percent=vat_percent,
        user=current_user,
    )


@router.get("/projects/{project_id}/budget/invoices/{invoice_id}")
def project_budget_invoice_detail(
    project_id: str,
    invoice_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    del current_user
    return get_invoice(project_id, invoice_id)


@router.patch("/projects/{project_id}/budget/invoices/{invoice_id}")
def review_project_budget_invoice(
    project_id: str,
    invoice_id: str,
    payload: BudgetDocumentReviewRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return review_invoice(
        project_ref=project_id,
        invoice_id=invoice_id,
        header=payload.header,
        lines=payload.lines,
        note=payload.note,
        user=current_user,
    )


@router.post("/projects/{project_id}/budget/invoices/{invoice_id}/verify")
def verify_project_budget_invoice(
    project_id: str,
    invoice_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return verify_invoice(
        project_ref=project_id, invoice_id=invoice_id, user=current_user
    )


@router.post("/projects/{project_id}/budget/invoices/{invoice_id}/decision")
def decide_project_budget_invoice(
    project_id: str,
    invoice_id: str,
    payload: InvoiceDecisionRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return decide_invoice(
        project_ref=project_id,
        invoice_id=invoice_id,
        action=payload.action,
        note=payload.note,
        certified_amount=payload.certified_amount,
        user=current_user,
    )


@router.post("/projects/{project_id}/budget/invoices/{invoice_id}/payments")
def record_project_budget_invoice_payment(
    project_id: str,
    invoice_id: str,
    payload: InvoicePaymentRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ensure_admin_user(current_user)
    return record_payment(
        project_ref=project_id,
        invoice_id=invoice_id,
        amount=payload.amount,
        payment_date=payload.payment_date,
        reference=payload.reference,
        note=payload.note,
        user=current_user,
    )
