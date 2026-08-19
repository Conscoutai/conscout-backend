from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

from services.progress.materials.material_service import (
    _extract_pdf_pages,
    _validated_confirmed_lines,
    build_material_ledger,
    classify_material_document,
    extract_structured_lines,
    validate_linked_material_line,
)


CLIENT_BOQ = Path(
    r"C:\Users\safwa\Downloads\safwan-20260819T062840Z-1-001\safwan\BOQ - Abdullatif Alfozan Street Rev.B 10-01-2024 REV-07.pdf"
)


def _document(document_id: str, document_type: str, lines: list[dict]) -> dict:
    return {
        "document_id": document_id,
        "document_type": document_type,
        "status": "confirmed",
        "confirmed_header": {},
        "confirmed_lines": lines,
    }


def test_boq_text_creates_reviewable_project_material_baseline_line():
    lines, warnings = extract_structured_lines(
        ["15 Flush curb concrete 100x300x500 14,538 LM 75 1,090,350"],
        document_type="boq",
    )

    assert len(lines) == 1
    assert lines[0]["item_number"] == "15"
    assert lines[0]["planned_qty"] == 14538
    assert lines[0]["unit"] == "LM"
    assert lines[0]["contract_unit_rate"] == 75
    assert any(item["code"] == "best_effort_extraction" for item in warnings)


def test_real_client_boq_extracts_the_two_curb_baseline_candidates():
    if not CLIENT_BOQ.exists():
        import pytest

        pytest.skip("Real client BOQ is not available on this machine")
    pages, method, _ = _extract_pdf_pages(CLIENT_BOQ.read_bytes())
    lines, _ = extract_structured_lines(pages, document_type="boq")
    curbs = {
        line["item_number"]: line
        for line in lines
        if line["source_page"] == 8 and line["item_number"] in {"14", "15"}
    }

    assert method == "native_text"
    assert curbs["14"]["planned_qty"] == 11467
    assert curbs["15"]["planned_qty"] == 14538
    assert curbs["15"]["unit"] == "LM"
    assert curbs["15"]["contract_unit_rate"] == 75


def test_document_classifier_handles_the_supplied_material_document_types():
    assert classify_material_document("BILL OF QUANTITIES", "contract.pdf") == "boq"
    assert classify_material_document("WEEKLY REPORT #38", "report.pdf") == "weekly_report"
    assert classify_material_document("CUSTOMER SHIPMENT Pack/Delivery ID", "ship.pdf") == "customer_shipment"
    assert classify_material_document("MATERIAL INSPECTION REQUEST", "mir.pdf") == "mir_grn"


def test_ledger_uses_boq_as_target_and_keeps_contract_value_labelled():
    documents = [
        _document(
            "boq-1",
            "boq",
            [
                {
                    "line_id": "b1",
                    "material_id": "mat-curb",
                    "item_number": "15",
                    "description": "Flush curb concrete 100x300x500",
                    "unit": "LM",
                    "planned_qty": 14538,
                    "contract_unit_rate": 75,
                    "line_amount": 1090350,
                }
            ],
        ),
        _document(
            "weekly-38",
            "weekly_report",
            [
                {
                    "line_id": "w1",
                    "linked_material_id": "mat-curb",
                    "description": "Curb Concrete 100x300x500",
                    "unit": "LM",
                    "approval_status": "delivered",
                    "expected_delivery_date": "2024-11-10",
                    "actual_delivery_date": "2024-11-08",
                }
            ],
        ),
        _document(
            "dn-1",
            "delivery_note",
            [
                {
                    "line_id": "d1",
                    "linked_material_id": "mat-curb",
                    "description": "Kerbstone 50x30x10cm",
                    "unit": "LM",
                    "delivered_qty": 378,
                }
            ],
        ),
        _document(
            "mir-1",
            "mir_grn",
            [
                {
                    "line_id": "m1",
                    "linked_material_id": "mat-curb",
                    "description": "Kerbstone",
                    "unit": "LM",
                    "inspected_qty": 378,
                    "accepted_qty": 370,
                    "rejected_qty": 8,
                    "inspection_result": "partially_accepted",
                }
            ],
        ),
    ]

    ledger, warnings = build_material_ledger(documents)

    assert warnings == []
    assert len(ledger) == 1
    item = ledger[0]
    assert item["delivery_target_qty"] == 14538
    assert item["delivered_qty"] == 378
    assert item["accepted_qty"] == 370
    assert item["rejected_qty"] == 8
    assert item["pending_delivery_qty"] == 14160
    assert item["pending_inspection_qty"] == 0
    assert item["value_basis"] == "boq_contract_rate"
    assert item["delivered_reference_value"] == 28350
    assert item["approval_status"] == "delivered"


def test_incompatible_units_do_not_change_ledger_quantity():
    documents = [
        _document(
            "boq-1",
            "boq",
            [
                {
                    "line_id": "b1",
                    "material_id": "mat-1",
                    "description": "Curb",
                    "unit": "LM",
                    "planned_qty": 100,
                }
            ],
        ),
        _document(
            "dn-1",
            "delivery_note",
            [
                {
                    "line_id": "d1",
                    "linked_material_id": "mat-1",
                    "description": "Curb",
                    "unit": "PCS",
                    "delivered_qty": 20,
                }
            ],
        ),
    ]

    ledger, warnings = build_material_ledger(documents)

    assert ledger[0]["delivered_qty"] == 0
    assert warnings[0]["code"] == "unit_mismatch"


def test_pending_mir_cannot_be_confirmed_as_acceptance_evidence():
    document = {
        "document_type": "mir_grn",
        "reviewed_lines": [
            {
                "description": "UPVC pipe",
                "unit": "PCS",
                "linked_material_id": "mat-upvc",
                "inspected_qty": 200,
                "inspection_result": "pending",
            }
        ],
    }

    try:
        _validated_confirmed_lines(document)
    except HTTPException as error:
        assert error.status_code == 422
        assert "no final inspection result" in str(error.detail)
    else:
        raise AssertionError("A pending MIR must remain in Needs Review")


def test_linked_documents_block_unit_mismatch_and_excess_inspection():
    try:
        validate_linked_material_line(
            document_type="delivery_note",
            line={"unit": "PCS", "delivered_qty": 20},
            material={"unit": "LM", "delivered_qty": 0},
            line_number=1,
        )
    except HTTPException as error:
        assert error.status_code == 422
        assert "does not match baseline unit" in str(error.detail)
    else:
        raise AssertionError("Incompatible quantities must not post")

    try:
        validate_linked_material_line(
            document_type="mir_grn",
            line={"unit": "PCS", "accepted_qty": 200, "rejected_qty": 0},
            material={
                "unit": "PCS",
                "delivered_qty": 199,
                "accepted_qty": 0,
                "rejected_qty": 0,
            },
            line_number=1,
        )
    except HTTPException as error:
        assert error.status_code == 422
        assert "only 199 delivered quantity" in str(error.detail)
    else:
        raise AssertionError("Inspection decisions cannot exceed delivery")
