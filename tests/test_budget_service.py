from __future__ import annotations

import io
import os

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

from services.progress.budget.budget_service import (
    _excel_rows,
    calculate_invoice_line,
    calculate_payment_state,
    material_boq_to_budget_payload,
    material_documents_as_of,
    normalize_boq_lines,
    normalize_invoice_lines,
)


def _boq_item() -> dict:
    return {
        "boq_item_id": "boq-1",
        "item_number": "1.1",
        "description": "Supply and install kerbstone",
        "unit": "LM",
        "contract_qty": 100,
        "contract_unit_rate": 10,
        "contract_amount": 1000,
    }


def _invoice_line(**overrides) -> dict:
    value = {
        "line_id": "line-1",
        "item_number": "1.1",
        "description": "Supply and install kerbstone",
        "unit": "LM",
        "claimed_unit_rate": 10,
        "current_claimed_qty": 25,
        "current_claimed_amount": 250,
    }
    value.update(overrides)
    return value


def _calculate(line: dict, *, previous_amount: float = 200, previous_qty: float = 20, activity_percent: float = 50):
    return calculate_invoice_line(
        invoice_line=line,
        boq_item=_boq_item(),
        variations=[],
        previous={"quantity": previous_qty, "amount": previous_amount},
        activity={
            "physical_progress_percent": activity_percent,
            "approved_evidence_count": 1,
            "tour_evidence_count": 1,
        },
        material=None,
        match_confidence=1,
        match_method="item_number",
    )


def test_verified_line_uses_previous_current_and_supported_cumulative_values():
    result = _calculate(_invoice_line())

    assert result["verification_status"] == "verified"
    assert result["previous_certified_amount"] == 200
    assert result["current_claimed_amount"] == 250
    assert result["cumulative_claimed_amount"] == 450
    assert result["verified_cumulative_amount"] == 500
    assert result["recommended_current_amount"] == 250


def test_overbilling_is_flagged_and_recommendation_is_capped():
    result = _calculate(
        _invoice_line(current_claimed_qty=90, current_claimed_amount=900),
        activity_percent=100,
    )

    assert result["verification_status"] == "overbilled"
    assert result["cumulative_claimed_amount"] == 1100
    assert result["recommended_current_amount"] == 800


def test_previously_certified_supported_progress_is_detected_as_duplicate():
    result = _calculate(
        _invoice_line(current_claimed_qty=10, current_claimed_amount=100),
        previous_qty=50,
        previous_amount=500,
        activity_percent=50,
    )

    assert result["verification_status"] == "duplicate"
    assert result["recommended_current_amount"] == 0


def test_pending_material_inspection_forces_review_without_proving_installation():
    result = calculate_invoice_line(
        invoice_line=_invoice_line(),
        boq_item={**_boq_item(), "material_id": "material-1"},
        variations=[],
        previous={"quantity": 20, "amount": 200},
        activity={"physical_progress_percent": 50, "approved_evidence_count": 1},
        material={
            "material_id": "material-1",
            "unit": "LM",
            "accepted_qty": 50,
            "pending_inspection_qty": 10,
        },
        match_confidence=1,
        match_method="item_number",
    )

    assert result["verification_status"] == "needs_review"
    assert any("pending inspection" in reason.lower() for reason in result["verification_reasons"])


def test_source_cumulative_invoice_value_derives_current_claim_after_previous():
    result = _calculate(
        _invoice_line(
            current_claimed_qty=0,
            current_claimed_amount=0,
            source_cumulative_qty=45,
            source_cumulative_amount=450,
        )
    )

    assert result["current_claimed_qty"] == 25
    assert result["current_claimed_amount"] == 250
    assert result["cumulative_claimed_amount"] == 450


def test_excel_boq_parser_extracts_priced_lines():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Hardscape"
    sheet.append(["S.No", "Description", "Quantity", "Unit", "Rate", "Amount"])
    sheet.append(["1.1", "Supply kerbstone", 100, "LM", 12.5, 1250])
    sheet.append(["1.2", "Install pavers", 200, "M2", 20, 4000])
    buffer = io.BytesIO()
    workbook.save(buffer)

    lines, header, warnings = _excel_rows(buffer.getvalue(), document_type="boq")

    assert header["currency"] == ""
    assert warnings == []
    assert len(lines) == 2
    assert lines[0]["category"] == "Hardscape"
    assert lines[0]["contract_qty"] == 100
    assert lines[0]["contract_unit_rate"] == 12.5
    assert lines[0]["contract_amount"] == 1250


def test_normalizers_recalculate_missing_amounts_and_preserve_manual_progress():
    boq = normalize_boq_lines(
        [{"description": "Concrete", "contract_qty": 3, "contract_unit_rate": 20}]
    )[0]
    invoice = normalize_invoice_lines(
        [
            {
                "description": "Concrete",
                "current_claimed_qty": 2,
                "claimed_unit_rate": 20,
                "manual_verified_percent": 35,
            }
        ]
    )[0]

    assert boq["contract_amount"] == 60
    assert invoice["current_claimed_amount"] == 40
    assert invoice["manual_verified_percent"] == 35


def test_confirmed_materials_boq_converts_to_linked_budget_lines():
    header, lines, warnings = material_boq_to_budget_payload(
        {
            "confirmed_header": {"currency": "SAR", "revision": "Rev 2"},
            "confirmed_lines": [
                {
                    "line_id": "material-line-1",
                    "material_id": "material-1",
                    "item_number": "L-01",
                    "description": "Landscape kerbstone",
                    "category": "Hardscape",
                    "unit": "LM",
                    "planned_qty": 100,
                    "contract_unit_rate": 12.5,
                    "line_amount": 1250,
                }
            ],
        }
    )

    assert header == {"currency": "SAR", "revision": "Rev 2"}
    assert lines[0]["line_id"] == "material-line-1"
    assert lines[0]["material_id"] == "material-1"
    assert lines[0]["contract_qty"] == 100
    assert lines[0]["contract_unit_rate"] == 12.5
    assert lines[0]["contract_amount"] == 1250
    assert warnings[0]["code"] == "imported_from_materials"


def test_materials_boq_import_warns_about_unpriced_lines():
    _, lines, warnings = material_boq_to_budget_payload(
        {
            "confirmed_lines": [
                {
                    "description": "Unpriced planting allowance",
                    "unit": "EA",
                    "planned_qty": 10,
                }
            ]
        }
    )

    assert lines[0]["contract_amount"] == 0
    assert any(item["code"] == "missing_priced_lines" for item in warnings)


def test_payment_state_supports_partial_and_full_payment_without_overpayment():
    assert calculate_payment_state(
        amount=250, existing_paid=100, certified_payable=500
    ) == (350, "certified")
    assert calculate_payment_state(
        amount=400, existing_paid=100, certified_payable=500
    ) == (500, "paid")

    with pytest.raises(HTTPException, match="cannot exceed") as error:
        calculate_payment_state(
            amount=401, existing_paid=100, certified_payable=500
        )
    assert error.value.status_code == 422


def test_payment_state_rejects_an_already_paid_invoice():
    with pytest.raises(HTTPException, match="already been paid") as error:
        calculate_payment_state(
            amount=1, existing_paid=500, certified_payable=500
        )
    assert error.value.status_code == 409


def test_material_evidence_uses_source_dates_not_upload_dates_for_cutoff():
    documents = [
        {
            "document_id": "boq-doc",
            "document_type": "boq",
            "status": "confirmed",
            "confirmed_lines": [],
        },
        {
            "document_id": "delivery-before",
            "document_type": "delivery_note",
            "status": "confirmed",
            "uploaded_at": "2026-08-20T10:00:00Z",
            "confirmed_header": {"document_date": "2026-07-30"},
            "confirmed_lines": [{"linked_material_id": "material-1"}],
        },
        {
            "document_id": "delivery-after",
            "document_type": "delivery_note",
            "status": "confirmed",
            "uploaded_at": "2026-07-01T10:00:00Z",
            "confirmed_header": {"document_date": "2026-08-02"},
            "confirmed_lines": [{"linked_material_id": "material-1"}],
        },
        {
            "document_id": "mir-undated",
            "document_type": "mir_grn",
            "status": "confirmed",
            "confirmed_lines": [{"linked_material_id": "material-1"}],
        },
    ]

    eligible, undated = material_documents_as_of(documents, "2026-07-31")

    assert [item["document_id"] for item in eligible] == [
        "boq-doc",
        "delivery-before",
    ]
    assert undated == {"material-1": ["mir-undated"]}
