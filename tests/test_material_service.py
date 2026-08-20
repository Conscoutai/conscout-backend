from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

from services.progress.materials.material_service import (
    _extract_pdf_pages,
    _validated_confirmed_lines,
    build_material_ledger,
    classify_material_document,
    document_matches_material_reset_scope,
    discard_material_document,
    enrich_delivery_lines_with_baseline,
    enrich_mir_lines_with_baseline,
    extract_structured_lines,
    preserve_matching_boq_material_ids,
    resolve_material_document_type,
    validate_linked_material_line,
)


CLIENT_BOQ = Path(
    r"C:\Users\safwa\Downloads\safwan-20260819T062840Z-1-001\safwan\BOQ - Abdullatif Alfozan Street Rev.B 10-01-2024 REV-07.pdf"
)
CLIENT_MATERIALS = Path(r"D:\Cosysta\conscout\Sites\fozan Street\materials")
CLIENT_WEEKLY_REPORT = Path(
    r"D:\Cosysta\conscout\Sites\fozan Street\PROJECT SETUP\material steup\02 - Weekly Report\Weekly Report #38 (18-11-2024).pdf"
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
    assert (
        classify_material_document("WEEKLY REPORT #38", "report.pdf") == "weekly_report"
    )
    assert (
        classify_material_document("CUSTOMER SHIPMENT Pack/Delivery ID", "ship.pdf")
        == "customer_shipment"
    )
    assert (
        classify_material_document("MATERIAL INSPECTION REQUEST", "mir.pdf")
        == "mir_grn"
    )


def test_strong_mir_classification_overrides_wrong_delivery_note_selection():
    resolved, corrected = resolve_material_document_type("delivery_note", "mir_grn")

    assert resolved == "mir_grn"
    assert corrected is True

    # delivery_note is the weak fallback classification, so it must not
    # override a deliberate MIR selection when content is ambiguous.
    resolved, corrected = resolve_material_document_type("mir_grn", "delivery_note")
    assert resolved == "mir_grn"
    assert corrected is False


def test_delivery_reference_identifier_is_not_a_material_quantity():
    lines, warnings = extract_structured_lines(
        [
            "Delivery Note NO 374694",
            "MAT-001 Flush curb concrete 378 LM",
        ],
        document_type="delivery_note",
    )

    assert len(lines) == 1
    assert lines[0]["material_code"] == "MAT-001"
    assert lines[0]["delivered_qty"] == 378
    assert any(item["code"] == "reference_identifier_ignored" for item in warnings)


def test_pending_mir_creates_reviewable_row_without_fabricating_inspection_result():
    text = """
    MATERIAL INSPECTION REQUEST (MIR)
    Description of material UPVC 50mm
    Delivery Note NO 374694
    INSPECTION RESULTS
    A. Accepted without objection [ ]
    B. Accepted subject to notes [ ]
    C. Rejected [ ]
    """
    detected = classify_material_document(text, "MIR - 04.pdf")
    resolved, corrected = resolve_material_document_type("delivery_note", detected)
    lines, warnings = extract_structured_lines([text], document_type=resolved)

    assert detected == "mir_grn"
    assert resolved == "mir_grn"
    assert corrected is True
    assert len(lines) == 1
    assert lines[0]["description"] == "UPVC 50mm"
    assert lines[0]["delivery_note_number"] == "374694"
    assert lines[0]["inspected_qty"] == 0
    assert lines[0]["accepted_qty"] == 0
    assert lines[0]["rejected_qty"] == 0
    assert lines[0]["inspection_result"] == "pending"
    assert not any(item["code"] == "manual_line_review_required" for item in warnings)


def test_mir_prefers_real_shipment_quantity_over_ocr_row_number():
    pages = [
        """
        MATERIAL INSPECTION REQUEST (MIR)
        Description of material UPVC 50mm
        Delivery Note NO 374694
        Supplier name ALMUNIF PIPES
        INSPECTION RESULTS
        A. Accepted without objection [ ]
        B. Accepted subject to notes [ ]
        C. Rejected [ ]
        """,
        """
        CUSTOMER SHIPMENT
        OrderLine ItemCode Item Description Unit of Measure Qty
        1 111050310 uPVC Pipe 50x1.8 mm Cl-3 PN6 S/C G 6 Mt MMP PC 199.00 + {
        1 uPVC Pipe 50x1.8 mm Cl-3 PN6 S/C G 6 Mt MMP PC 1
        """,
    ]
    lines, _ = extract_structured_lines(pages, document_type="mir_grn")

    assert len(lines) == 1
    assert lines[0]["material_code"] == "111050310"
    assert lines[0]["unit"] == "PCS"
    assert lines[0]["inspected_qty"] == 199
    assert lines[0]["inspection_result"] == "pending"
    assert lines[0]["supplier_name"] == "ALMUNIF PIPES"

    enriched = enrich_mir_lines_with_baseline(
        lines,
        [
            {
                "material_id": "mat-50mm",
                "boq_item_number": "11",
                "description": "50MM Upvc Conduit",
                "unit": "LM",
            }
        ],
    )
    assert enriched[0]["source_inspected_qty"] == 199
    assert enriched[0]["inspected_qty"] == 1194
    assert enriched[0]["unit"] == "LM"


def test_weekly_report_extracts_status_and_dates_without_fake_quantities():
    text = """
    12 – List of material
    LIST OF MATERIAL SUBMITTAL & DELIVERY. STATUS
    1 Aggregate based Subbase Hardscape Work Mar 14, 2024 Mar 20, 2024 Apr 02, 2024 Apr 06, 2024 Delivered
    6 UPVC Pipes ELECTRICAL WORK Feb 07, 2024 Feb 27, 2024 Mar 20, 2024 Mar 19, 2024 Delivered
    15Adjustable Head LED Flood
    LightELECTRICAL WORK Mar 19, 2024 Mar 31, 2024 PO issued
    """
    lines, _ = extract_structured_lines([text], document_type="weekly_report")

    assert len(lines) == 3
    assert lines[0]["description"] == "Aggregate based Subbase"
    assert lines[0]["approval_status"] == "delivered"
    assert lines[0]["expected_delivery_date"] == "2024-04-02"
    assert lines[0]["actual_delivery_date"] == "2024-04-06"
    assert lines[1]["actual_delivery_date"] == "2024-03-19"
    assert lines[2]["description"] == "Adjustable Head LED Flood Light"
    assert lines[2]["approval_status"] == "po_issued"
    assert "delivered_qty" not in lines[0]


def test_weekly_report_native_rows_skip_irrelevant_sparse_page_ocr(monkeypatch):
    import sys
    from types import ModuleType

    class FakePage:
        def __init__(self, text: str):
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class FakeReader:
        pages = [
            FakePage(
                """
                WEEKLY REPORT #38
                12 - List of material
                LIST OF MATERIAL SUBMITTAL & DELIVERY. STATUS
                1 Aggregate based Subbase Hardscape Work Mar 14, 2024 Mar 20, 2024 Apr 02, 2024 Apr 06, 2024 Delivered
                """
            ),
            FakePage("15 - Site Photos"),
        ]

    fake_pypdf = ModuleType("pypdf")
    fake_pypdf.PdfReader = lambda _: FakeReader()
    fake_pdf2image = ModuleType("pdf2image")

    def unexpected_ocr(*args, **kwargs):
        raise AssertionError("Weekly report OCR should be skipped when native rows exist")

    fake_pdf2image.convert_from_bytes = unexpected_ocr
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)
    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)

    pages, method, warnings = _extract_pdf_pages(
        b"%PDF-test",
        document_type_hint="weekly_report",
        filename_hint="Weekly Report #38.pdf",
    )
    lines, _ = extract_structured_lines(pages, document_type="weekly_report")

    assert method == "native_text"
    assert warnings == []
    assert len(lines) == 1
    assert lines[0]["description"] == "Aggregate based Subbase"


def test_real_client_weekly_report_uses_native_material_pages_without_ocr():
    if not CLIENT_WEEKLY_REPORT.exists():
        import pytest

        pytest.skip("Real client weekly report is not available on this machine")

    pages, method, warnings = _extract_pdf_pages(
        CLIENT_WEEKLY_REPORT.read_bytes(),
        document_type_hint="weekly_report",
        filename_hint=CLIENT_WEEKLY_REPORT.name,
    )
    lines, _ = extract_structured_lines(pages, document_type="weekly_report")

    assert method == "native_text"
    assert warnings == []
    assert len(lines) >= 20
    assert {line["source_page"] for line in lines}.issubset({28, 29, 30})


def test_progress_invoice_extracts_total_to_date_values_from_wrapped_row():
    text = """
    14 Supply & Install, Raised curb Concrete 200x300x500mm Sandblasted
    finish colour: grey as per specification and drawings ref:LS-20 D-23
    11467 Lm 75.00 860025.00 0% - 16% 137604 16% 137604
    15 Supply & Install, Flush curb Concrete 100x300x500mm
    14538 Lm 75.00 1090350 0% - 5% 54517.50 5% 54517.50
    """
    lines, _ = extract_structured_lines([text], document_type="progress_invoice")

    assert len(lines) == 2
    assert lines[0]["item_number"] == "14"
    assert lines[0]["unit"] == "LM"
    assert lines[0]["certified_percent"] == 16
    assert lines[0]["certified_qty"] == 1834.72
    assert lines[0]["certified_value"] == 137604
    assert lines[1]["certified_percent"] == 5


def test_delivery_parser_reassembles_ocr_columns_split_across_lines():
    lines, _ = extract_structured_lines(
        ["1.1 KERBSTONE 50X30X100CM GREY WITHOUT CHAMFER\nLM\n378"],
        document_type="delivery_note",
    )

    assert len(lines) == 1
    assert lines[0]["description"] == "KERBSTONE 50X30X100CM GREY WITHOUT CHAMFER"
    assert lines[0]["unit"] == "LM"
    assert lines[0]["delivered_qty"] == 378


def test_real_client_weekly_mir_and_invoice_have_reviewable_rows():
    cases = (
        ("weekly_report", CLIENT_MATERIALS / "Weekly Report #38 (18-11-2024).pdf", 20),
        ("mir_grn", CLIENT_MATERIALS / "MIR - 04 .pdf", 1),
        ("progress_invoice", CLIENT_MATERIALS / "APPROVED INVOICE#1.pdf", 4),
    )
    if not all(path.exists() for _, path, _ in cases):
        import pytest

        pytest.skip("Real client material PDFs are not available on this machine")
    for document_type, path, minimum_rows in cases:
        pages, _, _ = _extract_pdf_pages(path.read_bytes())
        lines, _ = extract_structured_lines(pages, document_type=document_type)
        assert len(lines) >= minimum_rows, f"{document_type} did not extract rows"


def test_customer_shipment_parser_accepts_unit_before_quantity_from_client_ocr():
    lines, _ = extract_structured_lines(
        ["1 111050310 uPVC Pipe 50x1.8 mm Cl-3 PN6 S/C G 6 Mt MMP PC 199.00"],
        document_type="customer_shipment",
    )

    assert len(lines) == 1
    assert lines[0]["material_code"] == "111050310"
    assert lines[0]["description"] == "uPVC Pipe 50x1.8 mm Cl-3 PN6 S/C G 6 Mt MMP"
    assert lines[0]["unit"] == "PCS"
    assert lines[0]["delivered_qty"] == 199


def test_customer_shipment_auto_matches_50mm_conduit_and_converts_pieces_to_lm():
    lines, _ = extract_structured_lines(
        ["1 111050310 uPVC Pipe 50x1.8 mm Cl-3 PN6 S/C G 6 Mt MMP PC 199.00"],
        document_type="customer_shipment",
    )
    enriched = enrich_delivery_lines_with_baseline(
        lines,
        [
            {
                "material_id": "mat-100mm",
                "boq_item_number": "11",
                "description": "100MM uPVC Ducts",
                "unit": "LM",
            },
            {
                "material_id": "mat-50mm",
                "boq_item_number": "11",
                "description": "50MM Upvc Conduit",
                "unit": "LM",
            },
            {
                "material_id": "mat-300mm",
                "boq_item_number": "2",
                "description": "PVC pipes class 3-6 bar for Sleeves - 300 mm",
                "unit": "LM",
            },
        ],
    )

    line = enriched[0]
    assert line["source_unit"] == "PCS"
    assert line["source_delivered_qty"] == 199
    assert line["suggested_material_id"] == "mat-50mm"
    assert line["linked_material_id"] == "mat-50mm"
    assert line["match_status"] == "suggested"
    assert line["match_confidence"] >= 0.78
    assert line["piece_length_m"] == 6
    assert line["conversion_factor"] == 6
    assert line["converted_qty"] == 1194
    assert line["delivered_qty"] == 1194
    assert line["unit"] == "LM"
    assert line["conversion_status"] == "suggested"


def test_suggested_unit_conversion_must_be_reviewed_before_confirmation():
    line = {
        "description": "uPVC Pipe 50x1.8 mm 6 Mt",
        "source_unit": "PCS",
        "source_delivered_qty": 199,
        "unit": "LM",
        "delivered_qty": 1194,
        "converted_qty": 1194,
        "conversion_factor": 6,
        "conversion_status": "suggested",
        "linked_material_id": "mat-50mm",
    }
    document = {"document_type": "customer_shipment", "reviewed_lines": [line]}

    try:
        _validated_confirmed_lines(document)
    except HTTPException as error:
        assert error.status_code == 422
        assert "conversion must be reviewed" in str(error.detail)
    else:
        raise AssertionError("An AI unit conversion must be reviewed before posting")

    line["conversion_status"] = "reviewed"
    confirmed = _validated_confirmed_lines(document)
    assert confirmed[0]["delivered_qty"] == 1194
    assert confirmed[0]["source_delivered_qty"] == 199


def test_delivery_parser_preserves_quantity_before_unit_layout():
    lines, _ = extract_structured_lines(
        ["MAT-001 Flush curb concrete 378 LM"],
        document_type="delivery_note",
    )

    assert len(lines) == 1
    assert lines[0]["material_code"] == "MAT-001"
    assert lines[0]["description"] == "Flush curb concrete"
    assert lines[0]["unit"] == "LM"
    assert lines[0]["delivered_qty"] == 378


def test_mixed_pdf_uses_ocr_on_sparse_pages(monkeypatch):
    import sys
    from types import ModuleType

    class FakePage:
        def __init__(self, text: str):
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class FakeReader:
        pages = [
            FakePage("MATERIAL INSPECTION REQUEST project metadata " * 5),
            FakePage("MATERIAL INSPECTION REQUEST Page 2 of 4"),
        ]

    render_calls: list[tuple[int | None, int | None]] = []

    def fake_convert_from_bytes(
        raw_bytes: bytes,
        *,
        dpi: int,
        fmt: str,
        first_page: int | None = None,
        last_page: int | None = None,
    ) -> list[str]:
        assert raw_bytes == b"%PDF-test"
        assert dpi == 220
        assert fmt == "png"
        render_calls.append((first_page, last_page))
        return [f"image-page-{first_page or 1}"]

    fake_pypdf = ModuleType("pypdf")
    fake_pypdf.PdfReader = lambda _: FakeReader()
    fake_pdf2image = ModuleType("pdf2image")
    fake_pdf2image.convert_from_bytes = fake_convert_from_bytes
    fake_pytesseract = ModuleType("pytesseract")
    fake_pytesseract.image_to_string = (
        lambda _: "1 111050310 uPVC Pipe 50x1.8 mm Cl-3 PN6 S/C G 6 Mt MMP PC 199.00"
    )
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)
    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)

    pages, method, warnings = _extract_pdf_pages(b"%PDF-test")
    lines, _ = extract_structured_lines(pages, document_type="delivery_note")

    assert method == "hybrid_ocr"
    assert render_calls == [(2, 2)]
    assert warnings[0]["code"] == "ocr_requires_review"
    assert lines[0]["material_code"] == "111050310"
    assert lines[0]["delivered_qty"] == 199


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


def test_boq_replacement_reuses_stable_material_ids_for_unchanged_rows():
    lines, reused = preserve_matching_boq_material_ids(
        [
            {
                "item_number": "11",
                "description": "50MM UPVC Conduit",
                "unit": "Lm",
                "planned_qty": 8000,
            },
            {
                "item_number": "12",
                "description": "New control panel",
                "unit": "PCS",
                "planned_qty": 2,
            },
        ],
        [
            {
                "material_id": "material-existing-50mm",
                "item_number": "11",
                "description": "50mm uPVC conduit",
                "unit": "LM",
                "planned_qty": 7250,
            }
        ],
    )

    assert reused == 1
    assert lines[0]["material_id"] == "material-existing-50mm"
    assert "material_id" not in lines[1]


def test_material_reset_scopes_are_explicit_and_predictable():
    pending_boq = {"status": "needs_review", "document_type": "boq"}
    confirmed_boq = {"status": "confirmed", "document_type": "boq"}
    confirmed_delivery = {"status": "confirmed", "document_type": "delivery_note"}

    assert document_matches_material_reset_scope(pending_boq, "pending") is True
    assert document_matches_material_reset_scope(confirmed_boq, "pending") is False
    assert document_matches_material_reset_scope(confirmed_boq, "transactions") is False
    assert (
        document_matches_material_reset_scope(confirmed_delivery, "transactions")
        is True
    )
    assert document_matches_material_reset_scope(confirmed_boq, "all") is True


class _DeleteResult:
    def __init__(self, deleted_count: int):
        self.deleted_count = deleted_count


class _PendingDocumentCollection:
    def __init__(self, document: dict | None):
        self.document = dict(document) if document else None

    def find_one(self, query: dict):
        if not self.document:
            return None
        if self.document.get("project_id") != query.get("project_id"):
            return None
        if self.document.get("document_id") != query.get("document_id"):
            return None
        return dict(self.document)

    def delete_one(self, query: dict):
        document = self.find_one(query)
        if not document or document.get("status") != query.get("status"):
            return _DeleteResult(0)
        self.document = None
        return _DeleteResult(1)


def test_discard_pending_upload_is_database_first_and_idempotent(monkeypatch, tmp_path):
    import services.progress.materials.material_service as material_service

    source = tmp_path / "pending.pdf"
    source.write_bytes(b"%PDF-test")
    collection = _PendingDocumentCollection(
        {
            "project_id": "project-1",
            "document_id": "doc-1",
            "status": "needs_review",
            "storage_path": str(source),
            "original_filename": "pending.pdf",
        }
    )
    monkeypatch.setattr(
        material_service,
        "resolve_project",
        lambda _: {"project_id": "project-1"},
    )
    monkeypatch.setattr(material_service, "material_documents_collection", collection)
    monkeypatch.setattr(material_service, "_audit", lambda **_: None)
    monkeypatch.setattr(material_service, "get_material_summary", lambda _: {})

    response = discard_material_document(
        project_ref="project-1",
        document_id="doc-1",
        reason="Wrong upload",
        user=SimpleNamespace(),
    )
    repeated = discard_material_document(
        project_ref="project-1",
        document_id="doc-1",
        reason="Retry after timeout",
        user=SimpleNamespace(),
    )

    assert response["status"] == "discarded"
    assert response["source_removed"] is True
    assert source.exists() is False
    assert repeated["status"] == "already_discarded"


def test_discard_still_succeeds_when_file_cleanup_or_summary_refresh_fails(
    monkeypatch, tmp_path
):
    import services.progress.materials.material_service as material_service

    source = tmp_path / "locked.pdf"
    source.write_bytes(b"%PDF-test")
    collection = _PendingDocumentCollection(
        {
            "project_id": "project-1",
            "document_id": "doc-locked",
            "status": "needs_review",
            "storage_path": str(source),
            "original_filename": "locked.pdf",
        }
    )
    monkeypatch.setattr(
        material_service,
        "resolve_project",
        lambda _: {"project_id": "project-1"},
    )
    monkeypatch.setattr(material_service, "material_documents_collection", collection)
    monkeypatch.setattr(
        material_service.os,
        "remove",
        lambda _: (_ for _ in ()).throw(PermissionError()),
    )
    monkeypatch.setattr(material_service, "_audit", lambda **_: None)
    monkeypatch.setattr(
        material_service,
        "get_material_summary",
        lambda _: (_ for _ in ()).throw(RuntimeError("temporary refresh error")),
    )

    response = discard_material_document(
        project_ref="project-1",
        document_id="doc-locked",
        reason="Wrong upload",
        user=SimpleNamespace(),
    )

    assert response["status"] == "discarded"
    assert response["source_removed"] is False
    assert response["summary_refresh_required"] is True
    assert collection.document is None
