from __future__ import annotations

import hashlib
import io
import os
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from fastapi import HTTPException

from core.config import site_materials_dir
from core.database import (
    material_audit_events_collection,
    material_documents_collection,
    project_materials_collection,
)
from core.auth_context import AuthenticatedUser
from services.progress.work_schedule.baseline_service import resolve_project


DOCUMENT_TYPES = {
    "auto",
    "boq",
    "weekly_report",
    "purchase_order",
    "delivery_note",
    "customer_shipment",
    "mir_grn",
    "progress_invoice",
}
CONFIRMABLE_TYPES = DOCUMENT_TYPES - {"auto"}
TRANSACTION_TYPES = {
    "weekly_report",
    "purchase_order",
    "delivery_note",
    "customer_shipment",
    "mir_grn",
    "progress_invoice",
}
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 250
MAX_OCR_PAGES = 75
MIN_NATIVE_TEXT_CHARS = 120

_UNIT_ALIASES = {
    "MTR": "M",
    "METER": "M",
    "METRE": "M",
    "L.M": "LM",
    "L.M.": "LM",
    "RM": "LM",
    "PC": "PCS",
    "PCE": "PCS",
    "PIECE": "PCS",
    "EA": "PCS",
    "NO": "PCS",
    "NOS": "PCS",
    "TONNE": "TON",
}
_NUMBER = r"[+-]?[\d,]+(?:\.\d+)?"
_UNIT = r"LM|L\.M\.?|M2|M3|M|MTR|PCS?|PCE|EA|NO|NOS|KG|TON|TONNE|BAG"
_BOQ_LINE = re.compile(
    rf"^\s*(?P<item>\d+(?:\.\d+)*)\s+(?P<description>.{{3,}}?)\s+"
    rf"(?P<quantity>{_NUMBER})\s+(?P<unit>{_UNIT})\s+"
    rf"(?P<rate>{_NUMBER})\s+(?P<amount>{_NUMBER})\s*$",
    re.IGNORECASE,
)
_DELIVERY_LINE_PREFIX = (
    rf"^\s*(?:\d{{1,4}}(?:\.\d+)*\s+)?"
    rf"(?:(?P<code>(?=[A-Z0-9./_-]*\d)[A-Z0-9][A-Z0-9./_-]{{3,}})\s+)?"
    rf"(?P<description>.{{4,}}?)\s+"
)
_DELIVERY_LINE_PATTERNS = (
    re.compile(
        _DELIVERY_LINE_PREFIX
        + rf"(?P<quantity>{_NUMBER})\s+(?P<unit>{_UNIT})\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        _DELIVERY_LINE_PREFIX
        + rf"(?P<unit>{_UNIT})\s+(?P<quantity>{_NUMBER})\s*$",
        re.IGNORECASE,
    ),
)
_BOQ_BLOCK_START = re.compile(r"^\s*(?P<item>\d+(?:\.\d+)*)\s+(?P<description>.+)$")
_BOQ_BLOCK_VALUE = re.compile(
    rf"(?P<raw_quantity>[\d,]+)\s*(?P<unit>{_UNIT})\s+"
    rf"(?P<rate>{_NUMBER})\s+(?P<amount>{_NUMBER})",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}-[A-Za-z]{3}-\d{2,4})\b"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _rounded(value: Any) -> float:
    return round(_number(value), 4)


def normalize_unit(value: Any) -> str:
    unit = re.sub(r"\s+", "", str(value or "").upper())
    return _UNIT_ALIASES.get(unit, unit)


def normalize_description(value: Any) -> str:
    description = re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper())
    return " ".join(description.split())


def classify_material_document(text: str, filename: str = "") -> str:
    sample = f"{filename}\n{text[:30000]}".lower()
    rules = (
        ("mir_grn", ("material inspection request", "goods received note", " mir ")),
        ("weekly_report", ("weekly report", "material delivery status", "long lead item")),
        ("customer_shipment", ("customer shipment", "pack/delivery id")),
        ("delivery_note", ("delivery note", "delivery challan")),
        ("purchase_order", ("purchase order", "p.o. number", "po number")),
        ("progress_invoice", ("progress invoice", "interim payment", "payment certificate")),
        ("boq", ("bill of quantities", "priced boq", " boq ")),
    )
    padded = f" {sample} "
    for document_type, keywords in rules:
        if any(keyword in padded for keyword in keywords):
            return document_type
    return "boq" if "boq" in Path(filename).stem.lower() else "delivery_note"


def _extract_pdf_pages(raw_bytes: bytes) -> tuple[list[str], str, list[dict[str, Any]]]:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            raise HTTPException(503, "PDF extraction requires the pypdf dependency") from error

    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
    except Exception as error:
        raise HTTPException(422, "The uploaded PDF could not be read") from error
    if len(reader.pages) > MAX_PDF_PAGES:
        raise HTTPException(413, f"PDFs are limited to {MAX_PDF_PAGES} pages")

    pages = [(page.extract_text() or "") for page in reader.pages]
    native_text_available = any(page.strip() for page in pages)
    sparse_page_indexes = [
        index
        for index, page in enumerate(pages)
        if len(" ".join(page.split())) < MIN_NATIVE_TEXT_CHARS
    ]
    if not sparse_page_indexes:
        return pages, "native_text", []

    warnings: list[dict[str, Any]] = []
    if len(sparse_page_indexes) > MAX_OCR_PAGES:
        warnings.append(
            {
                "code": "ocr_page_limit",
                "message": f"PDFs with over {MAX_OCR_PAGES} sparse pages require a split upload or offline processing.",
            }
        )
        method = "native_text" if native_text_available else "ocr_required"
        return pages, method, warnings
    try:
        from pdf2image import convert_from_bytes
        import pytesseract

        images_by_page: dict[int, Any] = {}
        if len(sparse_page_indexes) == len(pages):
            images = convert_from_bytes(raw_bytes, dpi=220, fmt="png")
            images_by_page.update(zip(sparse_page_indexes, images))
        else:
            for index in sparse_page_indexes:
                images = convert_from_bytes(
                    raw_bytes,
                    dpi=220,
                    fmt="png",
                    first_page=index + 1,
                    last_page=index + 1,
                )
                if images:
                    images_by_page[index] = images[0]

        ocr_page_count = 0
        for index, image in images_by_page.items():
            ocr_text = pytesseract.image_to_string(image) or ""
            if len(" ".join(ocr_text.split())) > len(" ".join(pages[index].split())):
                pages[index] = ocr_text
                ocr_page_count += 1

        if ocr_page_count:
            warnings.append(
                {
                    "code": "ocr_requires_review",
                    "message": f"OCR was used on {ocr_page_count} sparse PDF page(s). Values require page-by-page review.",
                }
            )
            method = "hybrid_ocr" if native_text_available else "ocr"
            return pages, method, warnings
    except Exception as error:
        warnings.append(
            {
                "code": "ocr_unavailable",
                "message": f"Sparse PDF pages could not be OCRed: {error}",
            }
        )
    method = "native_text" if native_text_available else "ocr_required"
    return pages, method, warnings


def extract_structured_lines(
    pages: Iterable[str], *, document_type: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lines: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for page_number, page_text in enumerate(pages, start=1):
        if document_type == "boq":
            page_lines = page_text.splitlines()
            starts = [
                index
                for index, value in enumerate(page_lines)
                if _BOQ_BLOCK_START.match(" ".join(value.split()))
            ]
            for position, start in enumerate(starts):
                end = starts[position + 1] if position + 1 < len(starts) else len(page_lines)
                block = " ".join(" ".join(value.split()) for value in page_lines[start:end])
                row = _BOQ_BLOCK_START.match(block)
                if not row:
                    continue
                values = _BOQ_BLOCK_VALUE.search(row.group("description"))
                if not values:
                    continue
                rate = _number(values.group("rate"))
                amount = _number(values.group("amount"))
                if rate <= 0 or amount <= 0:
                    continue
                # PDF table extraction sometimes joins a drawing reference such
                # as ``D-22`` to quantity ``14538``. Amount / rate is the safer
                # quantity candidate, and every value still requires review.
                quantity = amount / rate
                description = row.group("description")[: values.start()].strip(" -:")
                key = (page_number, row.group("item"), normalize_description(description))
                if len(description) < 4 or key in seen:
                    continue
                seen.add(key)
                lines.append(
                    {
                        "line_id": f"line_{uuid4().hex}",
                        "source_page": page_number,
                        "item_number": row.group("item"),
                        "description": description,
                        "unit": normalize_unit(values.group("unit")),
                        "planned_qty": quantity,
                        "contract_unit_rate": rate,
                        "line_amount": amount,
                        "confidence": 0.62,
                        "warnings": [
                            "Quantity was cross-checked from line amount / contract rate; confirm against the source page."
                        ],
                    }
                )
        for raw_line in page_text.splitlines():
            compact = " ".join(raw_line.split())
            if len(compact) < 5:
                continue
            match = _BOQ_LINE.match(compact) if document_type in {"boq", "progress_invoice"} else None
            if match:
                values = match.groupdict()
                key = (page_number, values["item"], normalize_description(values["description"]))
                if key in seen:
                    continue
                seen.add(key)
                quantity = _number(values["quantity"])
                rate = _number(values["rate"])
                line: dict[str, Any] = {
                    "line_id": f"line_{uuid4().hex}",
                    "source_page": page_number,
                    "item_number": values["item"],
                    "description": values["description"].strip(" -:"),
                    "unit": normalize_unit(values["unit"]),
                    "confidence": 0.72,
                    "warnings": ["Confirm the extracted table row against the source page."],
                }
                if document_type == "boq":
                    line.update(
                        planned_qty=quantity,
                        contract_unit_rate=rate,
                        line_amount=_number(values["amount"]),
                    )
                else:
                    line.update(
                        certified_qty=quantity,
                        certified_unit_rate=rate,
                        certified_value=_number(values["amount"]),
                    )
                lines.append(line)
                continue

            if document_type in {"delivery_note", "customer_shipment", "purchase_order"}:
                match = next(
                    (
                        candidate
                        for pattern in _DELIVERY_LINE_PATTERNS
                        if (candidate := pattern.match(compact))
                    ),
                    None,
                )
                if not match:
                    continue
                values = match.groupdict()
                description = values["description"].strip(" -:")
                if len(normalize_description(description)) < 4:
                    continue
                key = (page_number, values.get("code"), normalize_description(description))
                if key in seen:
                    continue
                seen.add(key)
                field = "ordered_qty" if document_type == "purchase_order" else "delivered_qty"
                lines.append(
                    {
                        "line_id": f"line_{uuid4().hex}",
                        "source_page": page_number,
                        "material_code": values.get("code") or "",
                        "description": description,
                        "unit": normalize_unit(values["unit"]),
                        field: _number(values["quantity"]),
                        "confidence": 0.65,
                        "warnings": ["Link this row to a confirmed BOQ baseline material."],
                    }
                )

    if not lines:
        warnings.append(
            {
                "code": "manual_line_review_required",
                "message": "No reliable material rows were detected. Add or correct rows during review.",
            }
        )
    warnings.append(
        {
            "code": "best_effort_extraction",
            "message": "Phase 1 extraction is best effort; confirmation is required before ledger posting.",
        }
    )
    return lines, warnings


def _extract_header(text: str, document_type: str) -> dict[str, Any]:
    dates = _DATE.findall(text[:50000])
    header: dict[str, Any] = {
        "document_date": dates[0] if dates else "",
        "document_number": "",
    }
    patterns = {
        "delivery_note": r"(?:delivery\s*note|dn)\s*(?:no\.?|number|#)?\s*[:.-]?\s*([A-Z0-9/-]+)",
        "customer_shipment": r"(?:pack/delivery\s*id|delivery\s*id)\s*[:.-]?\s*([A-Z0-9/-]+)",
        "mir_grn": r"(?:mir|grn)\s*(?:no\.?|number|#)?\s*[:.-]?\s*([A-Z0-9/-]+)",
        "purchase_order": r"(?:purchase\s*order|po)\s*(?:no\.?|number|#)?\s*[:.-]?\s*([A-Z0-9/-]+)",
        "weekly_report": r"weekly\s*report\s*(?:no\.?|number|#)?\s*[:.-]?\s*([A-Z0-9/-]+)",
        "progress_invoice": r"(?:invoice|application)\s*(?:no\.?|number|#)?\s*[:.-]?\s*([A-Z0-9/-]+)",
    }
    pattern = patterns.get(document_type)
    if pattern:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            header["document_number"] = match.group(1)
    return header


def _public_document(document: dict[str, Any], *, include_lines: bool = True) -> dict[str, Any]:
    result = {
        key: value
        for key, value in document.items()
        if key not in {"_id", "storage_path", "extracted_text"}
    }
    if not include_lines:
        result.pop("extracted_lines", None)
        result.pop("reviewed_lines", None)
        result.pop("confirmed_lines", None)
    return result


def _audit(
    *,
    project_id: str,
    document_id: str,
    event_type: str,
    user: AuthenticatedUser,
    details: dict[str, Any] | None = None,
) -> None:
    material_audit_events_collection.insert_one(
        {
            "event_id": f"material_audit_{uuid4().hex}",
            "project_id": project_id,
            "document_id": document_id,
            "event_type": event_type,
            "actor_user_id": user.user_id,
            "actor_email": user.email,
            "actor_name": user.name,
            "details": details or {},
            "created_at": utc_now(),
        }
    )


def upload_material_document(
    *,
    project_ref: str,
    filename: str,
    raw_bytes: bytes,
    document_type: str,
    user: AuthenticatedUser,
) -> dict[str, Any]:
    requested_type = str(document_type or "auto").strip().lower()
    if requested_type not in DOCUMENT_TYPES:
        raise HTTPException(400, f"Unsupported material document type: {requested_type}")
    safe_filename = Path(filename or "material-document.pdf").name
    if Path(safe_filename).suffix.lower() != ".pdf":
        raise HTTPException(400, "Material documents must be PDF files in Phase 1")
    if not raw_bytes:
        raise HTTPException(400, "The uploaded file is empty")
    if len(raw_bytes) > MAX_FILE_BYTES:
        raise HTTPException(413, "Material documents are limited to 25 MB")
    if not raw_bytes.startswith(b"%PDF-"):
        raise HTTPException(400, "The uploaded file is not a valid PDF")

    project = resolve_project(project_ref)
    project_id = project["project_id"]
    digest = hashlib.sha256(raw_bytes).hexdigest()
    existing = material_documents_collection.find_one(
        {"project_id": project_id, "source_sha256": digest}
    )
    reprocess_existing = bool(
        existing
        and existing.get("status") == "needs_review"
        and not existing.get("extracted_lines")
        and not existing.get("reviewed_lines")
    )
    if existing and not reprocess_existing:
        return {"status": "already_uploaded", "document": _public_document(existing)}

    pages, extraction_method, extraction_warnings = _extract_pdf_pages(raw_bytes)
    text = "\n\f\n".join(pages)
    detected_type = classify_material_document(text, safe_filename)
    resolved_type = detected_type if requested_type == "auto" else requested_type
    lines, line_warnings = extract_structured_lines(pages, document_type=resolved_type)
    now = utc_now()
    document_id = (
        str(existing.get("document_id"))
        if reprocess_existing and existing
        else f"material_doc_{uuid4().hex}"
    )
    document_directory = os.path.join(site_materials_dir(project_id), "documents")
    os.makedirs(document_directory, exist_ok=True)
    stored_filename = (
        str(existing.get("stored_filename"))
        if reprocess_existing and existing and existing.get("stored_filename")
        else f"{document_id}_{safe_filename}"
    )
    stored_path = (
        str(existing.get("storage_path"))
        if reprocess_existing and existing and existing.get("storage_path")
        else os.path.join(document_directory, stored_filename)
    )
    with open(stored_path, "wb") as output:
        output.write(raw_bytes)

    warnings = [*extraction_warnings, *line_warnings]
    if requested_type != "auto" and requested_type != detected_type:
        warnings.insert(
            0,
            {
                "code": "classification_mismatch",
                "message": f"Selected as {requested_type}, but content resembles {detected_type}.",
            },
        )
    document = {
        "document_id": document_id,
        "project_id": project_id,
        "site_name": project["site_name"],
        "floorplan_id": project["floorplan_id"],
        "document_type": resolved_type,
        "requested_document_type": requested_type,
        "detected_document_type": detected_type,
        "original_filename": safe_filename,
        "stored_filename": stored_filename,
        "storage_path": stored_path,
        "source_sha256": digest,
        "source_size_bytes": len(raw_bytes),
        "page_count": len(pages),
        "extraction_method": extraction_method,
        "processing_status": "processed" if extraction_method != "ocr_required" else "review_required",
        "status": "needs_review",
        "extracted_header": _extract_header(text, resolved_type),
        "extracted_lines": lines,
        "extracted_text": text,
        "text_preview": " ".join(text.split())[:1500],
        "warnings": warnings,
        "uploaded_by_user_id": user.user_id,
        "uploaded_by_email": user.email,
        "uploaded_at": existing.get("uploaded_at", now) if existing else now,
        "processed_at": now,
        "updated_at": now,
    }
    if reprocess_existing:
        document.update(
            reprocessed_at=now,
            reprocessed_by_user_id=user.user_id,
            reprocessed_by_email=user.email,
        )
    try:
        if reprocess_existing and existing:
            material_documents_collection.update_one(
                {"document_id": document_id}, {"$set": document}
            )
        else:
            material_documents_collection.insert_one(document)
    except Exception:
        if not reprocess_existing:
            try:
                os.remove(stored_path)
            except OSError:
                pass
        raise
    _audit(
        project_id=project_id,
        document_id=document_id,
        event_type="reprocessed" if reprocess_existing else "uploaded",
        user=user,
        details={"document_type": resolved_type, "filename": safe_filename},
    )
    status = "reprocessed" if reprocess_existing else "needs_review"
    return {"status": status, "document": _public_document(document)}


def list_material_documents(project_ref: str) -> list[dict[str, Any]]:
    project = resolve_project(project_ref)
    cursor = material_documents_collection.find({"project_id": project["project_id"]}).sort(
        "uploaded_at", -1
    )
    return [_public_document(item, include_lines=False) for item in cursor]


def get_material_document(project_ref: str, document_id: str) -> dict[str, Any]:
    project = resolve_project(project_ref)
    document = material_documents_collection.find_one(
        {"project_id": project["project_id"], "document_id": document_id}
    )
    if not document:
        raise HTTPException(404, "Material document not found")
    return _public_document(document)


def update_material_document_review(
    *,
    project_ref: str,
    document_id: str,
    header: dict[str, Any],
    lines: list[dict[str, Any]],
    review_note: str,
    user: AuthenticatedUser,
) -> dict[str, Any]:
    project = resolve_project(project_ref)
    project_id = project["project_id"]
    document = material_documents_collection.find_one(
        {"project_id": project_id, "document_id": document_id}
    )
    if not document:
        raise HTTPException(404, "Material document not found")
    if document.get("status") in {"confirmed", "superseded", "voided"}:
        raise HTTPException(409, "Confirmed, superseded, or voided documents cannot be edited")
    normalized_lines = _normalize_review_lines(lines)
    now = utc_now()
    material_documents_collection.update_one(
        {"project_id": project_id, "document_id": document_id},
        {
            "$set": {
                "reviewed_header": dict(header or {}),
                "reviewed_lines": normalized_lines,
                "review_note": str(review_note or "").strip(),
                "reviewed_by_user_id": user.user_id,
                "reviewed_by_email": user.email,
                "reviewed_at": now,
                "updated_at": now,
            }
        },
    )
    _audit(
        project_id=project_id,
        document_id=document_id,
        event_type="review_updated",
        user=user,
        details={"line_count": len(normalized_lines), "note": review_note},
    )
    return get_material_document(project_id, document_id)


def _normalize_review_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for source in lines or []:
        line = dict(source)
        line["line_id"] = str(line.get("line_id") or f"line_{uuid4().hex}")
        line["description"] = str(line.get("description") or "").strip()
        line["unit"] = normalize_unit(line.get("unit"))
        line["linked_material_id"] = str(line.get("linked_material_id") or "").strip()
        for field in (
            "planned_qty",
            "ordered_qty",
            "delivered_qty",
            "return_qty",
            "inspected_qty",
            "accepted_qty",
            "rejected_qty",
            "contract_unit_rate",
            "purchase_unit_rate",
            "line_amount",
            "certified_percent",
            "certified_qty",
            "certified_value",
        ):
            if field in line:
                line[field] = _number(line[field])
        normalized.append(line)
    return normalized


def _validated_confirmed_lines(document: dict[str, Any]) -> list[dict[str, Any]]:
    document_type = str(document.get("document_type") or "")
    if document_type not in CONFIRMABLE_TYPES:
        raise HTTPException(422, "Document classification must be confirmed first")
    lines = _normalize_review_lines(
        document.get("reviewed_lines")
        if document.get("reviewed_lines") is not None
        else document.get("extracted_lines") or []
    )
    if not lines:
        raise HTTPException(422, "Add at least one reviewed material line before confirmation")
    for index, line in enumerate(lines, start=1):
        description = str(line.get("description") or "").strip()
        unit = normalize_unit(line.get("unit"))
        if document_type == "boq":
            if not description or not unit or _number(line.get("planned_qty")) <= 0:
                raise HTTPException(422, f"BOQ line {index} requires description, unit, and planned quantity")
            line["material_id"] = str(line.get("material_id") or f"material_{uuid4().hex}")
        elif document_type in TRANSACTION_TYPES:
            if not line.get("linked_material_id"):
                raise HTTPException(422, f"Line {index} must be linked to a confirmed BOQ baseline material")
            if document_type in {"delivery_note", "customer_shipment"} and _number(line.get("delivered_qty")) <= 0:
                raise HTTPException(422, f"Delivery line {index} requires a delivered quantity")
            if document_type == "purchase_order" and _number(line.get("ordered_qty")) <= 0:
                raise HTTPException(422, f"PO line {index} requires an ordered quantity")
            if document_type == "mir_grn":
                result = str(line.get("inspection_result") or "pending").strip().lower()
                if result in {"", "pending", "submitted"}:
                    raise HTTPException(422, f"MIR/GRN line {index} has no final inspection result")
                inspected = _number(line.get("inspected_qty"))
                decided = _number(line.get("accepted_qty")) + _number(line.get("rejected_qty"))
                if inspected > 0 and decided > inspected + 0.0001:
                    raise HTTPException(422, f"MIR/GRN line {index} acceptance exceeds inspected quantity")
        line["unit"] = unit
    return lines


def validate_linked_material_line(
    *,
    document_type: str,
    line: dict[str, Any],
    material: dict[str, Any],
    line_number: int,
) -> None:
    incoming_unit = normalize_unit(line.get("unit"))
    baseline_unit = normalize_unit(material.get("unit"))
    if incoming_unit and baseline_unit and incoming_unit != baseline_unit:
        raise HTTPException(
            422,
            f"Line {line_number} unit {incoming_unit} does not match baseline unit {baseline_unit}",
        )
    if document_type != "mir_grn":
        return
    available = max(
        _number(material.get("delivered_qty"))
        - _number(material.get("accepted_qty"))
        - _number(material.get("rejected_qty")),
        0.0,
    )
    decided = _number(line.get("accepted_qty")) + _number(line.get("rejected_qty"))
    if decided > available + 0.0001:
        raise HTTPException(
            422,
            f"MIR/GRN line {line_number} decides {decided:g} {baseline_unit}, but only {available:g} delivered quantity is awaiting inspection",
        )


def confirm_material_document(
    *, project_ref: str, document_id: str, user: AuthenticatedUser
) -> dict[str, Any]:
    project = resolve_project(project_ref)
    project_id = project["project_id"]
    document = material_documents_collection.find_one(
        {"project_id": project_id, "document_id": document_id}
    )
    if not document:
        raise HTTPException(404, "Material document not found")
    if document.get("status") == "confirmed":
        return {"status": "already_confirmed", "document": _public_document(document)}
    if document.get("status") in {"superseded", "voided"}:
        raise HTTPException(409, "Superseded or voided documents cannot be confirmed")

    lines = _validated_confirmed_lines(document)
    if document.get("document_type") in TRANSACTION_TYPES:
        for index, line in enumerate(lines, start=1):
            material = project_materials_collection.find_one(
                {
                    "project_id": project_id,
                    "material_id": line.get("linked_material_id"),
                }
            )
            if not material:
                raise HTTPException(
                    422,
                    f"Line {index} is linked to a BOQ material that is not active",
                )
            validate_linked_material_line(
                document_type=str(document.get("document_type") or ""),
                line=line,
                material=material,
                line_number=index,
            )
    header = dict(document.get("reviewed_header") or document.get("extracted_header") or {})
    now = utc_now()
    if document.get("document_type") == "boq":
        material_documents_collection.update_many(
            {
                "project_id": project_id,
                "document_type": "boq",
                "status": "confirmed",
                "document_id": {"$ne": document_id},
            },
            {"$set": {"status": "superseded", "superseded_at": now, "updated_at": now}},
        )
    material_documents_collection.update_one(
        {"project_id": project_id, "document_id": document_id},
        {
            "$set": {
                "status": "confirmed",
                "confirmed_header": header,
                "confirmed_lines": lines,
                "confirmed_by_user_id": user.user_id,
                "confirmed_by_email": user.email,
                "confirmed_at": now,
                "updated_at": now,
            }
        },
    )
    _audit(
        project_id=project_id,
        document_id=document_id,
        event_type="confirmed",
        user=user,
        details={"line_count": len(lines)},
    )
    rebuild_material_ledger(project_id)
    return {
        "status": "confirmed",
        "document": get_material_document(project_id, document_id),
        "summary": get_material_summary(project_id),
    }


def void_material_document(
    *, project_ref: str, document_id: str, reason: str, user: AuthenticatedUser
) -> dict[str, Any]:
    project = resolve_project(project_ref)
    project_id = project["project_id"]
    document = material_documents_collection.find_one(
        {"project_id": project_id, "document_id": document_id}
    )
    if not document:
        raise HTTPException(404, "Material document not found")
    if document.get("status") == "voided":
        return {"status": "already_voided", "document": _public_document(document)}
    now = utc_now()
    material_documents_collection.update_one(
        {"project_id": project_id, "document_id": document_id},
        {
            "$set": {
                "status": "voided",
                "void_reason": str(reason or "").strip(),
                "voided_by_user_id": user.user_id,
                "voided_by_email": user.email,
                "voided_at": now,
                "updated_at": now,
            }
        },
    )
    _audit(
        project_id=project_id,
        document_id=document_id,
        event_type="voided",
        user=user,
        details={"reason": reason},
    )
    rebuild_material_ledger(project_id)
    return {
        "status": "voided",
        "document": get_material_document(project_id, document_id),
        "summary": get_material_summary(project_id),
    }


def build_material_ledger(documents: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    confirmed = [item for item in documents if item.get("status") == "confirmed"]
    materials: dict[str, dict[str, Any]] = {}
    reconciliation_warnings: list[dict[str, Any]] = []

    for document in confirmed:
        if document.get("document_type") != "boq":
            continue
        for line in document.get("confirmed_lines") or []:
            material_id = str(line.get("material_id") or "").strip()
            if not material_id:
                continue
            planned = _number(line.get("planned_qty"))
            contract_rate = _number(line.get("contract_unit_rate"))
            materials[material_id] = {
                "material_id": material_id,
                "boq_document_id": document.get("document_id"),
                "boq_item_number": str(line.get("item_number") or ""),
                "material_code": str(line.get("material_code") or ""),
                "description": str(line.get("description") or ""),
                "normalized_description": normalize_description(line.get("description")),
                "unit": normalize_unit(line.get("unit")),
                "currency": str(line.get("currency") or document.get("confirmed_header", {}).get("currency") or ""),
                "planned_qty": planned,
                "contract_unit_rate": contract_rate,
                "planned_contract_value": _number(line.get("line_amount")) or planned * contract_rate,
                "ordered_qty": 0.0,
                "purchase_unit_rate": 0.0,
                "committed_value": 0.0,
                "delivered_qty": 0.0,
                "inspected_qty": 0.0,
                "accepted_qty": 0.0,
                "rejected_qty": 0.0,
                "certified_percent": 0.0,
                "certified_value": 0.0,
                "approval_status": "",
                "expected_delivery_date": "",
                "actual_delivery_date": "",
                "source_document_ids": [document.get("document_id")],
                "warnings": [],
            }

    for document in confirmed:
        document_type = str(document.get("document_type") or "")
        if document_type == "boq":
            continue
        for line in document.get("confirmed_lines") or []:
            material_id = str(line.get("linked_material_id") or "").strip()
            item = materials.get(material_id)
            if not item:
                reconciliation_warnings.append(
                    {
                        "code": "unmatched_confirmed_line",
                        "document_id": document.get("document_id"),
                        "line_id": line.get("line_id"),
                    }
                )
                continue
            incoming_unit = normalize_unit(line.get("unit"))
            if incoming_unit and item["unit"] and incoming_unit != item["unit"]:
                warning = {
                    "code": "unit_mismatch",
                    "document_id": document.get("document_id"),
                    "line_id": line.get("line_id"),
                    "baseline_unit": item["unit"],
                    "document_unit": incoming_unit,
                }
                item["warnings"].append(warning)
                reconciliation_warnings.append(warning)
                continue
            item["source_document_ids"].append(document.get("document_id"))
            if document_type == "weekly_report":
                item["approval_status"] = str(line.get("approval_status") or item["approval_status"])
                item["expected_delivery_date"] = str(line.get("expected_delivery_date") or item["expected_delivery_date"])
                item["actual_delivery_date"] = str(line.get("actual_delivery_date") or item["actual_delivery_date"])
            elif document_type == "purchase_order":
                item["ordered_qty"] += _number(line.get("ordered_qty"))
                rate = _number(line.get("purchase_unit_rate"))
                if rate:
                    item["purchase_unit_rate"] = rate
            elif document_type in {"delivery_note", "customer_shipment"}:
                item["delivered_qty"] += _number(line.get("delivered_qty")) - _number(line.get("return_qty"))
                actual_date = str(line.get("actual_delivery_date") or document.get("confirmed_header", {}).get("document_date") or "")
                if actual_date:
                    item["actual_delivery_date"] = actual_date
            elif document_type == "mir_grn":
                item["inspected_qty"] += _number(line.get("inspected_qty"))
                item["accepted_qty"] += _number(line.get("accepted_qty"))
                item["rejected_qty"] += _number(line.get("rejected_qty"))
            elif document_type == "progress_invoice":
                item["certified_percent"] = max(item["certified_percent"], _number(line.get("certified_percent")))
                item["certified_value"] = max(item["certified_value"], _number(line.get("certified_value")))

    today = date.today().isoformat()
    for item in materials.values():
        item["committed_value"] = item["ordered_qty"] * item["purchase_unit_rate"]
        target = item["ordered_qty"] if item["ordered_qty"] > 0 else item["planned_qty"]
        item["delivery_target_qty"] = target
        item["pending_delivery_qty"] = max(target - item["delivered_qty"], 0.0)
        item["pending_inspection_qty"] = max(
            item["delivered_qty"] - item["accepted_qty"] - item["rejected_qty"], 0.0
        )
        item["remaining_acceptance_qty"] = max(target - item["accepted_qty"], 0.0)
        item["over_delivery_qty"] = max(item["delivered_qty"] - target, 0.0)
        rate = item["purchase_unit_rate"] or item["contract_unit_rate"]
        item["value_basis"] = "po_purchase_rate" if item["purchase_unit_rate"] else "boq_contract_rate"
        item["delivered_reference_value"] = item["delivered_qty"] * rate
        item["accepted_reference_value"] = item["accepted_qty"] * rate
        overdue = bool(
            item["expected_delivery_date"]
            and item["expected_delivery_date"] < today
            and item["pending_delivery_qty"] > 0
        )
        item["is_overdue"] = overdue
        if item["rejected_qty"] > 0:
            status = "rejected_on_hold"
        elif item["over_delivery_qty"] > 0:
            status = "over_delivered"
        elif overdue:
            status = "overdue_shortage"
        elif target > 0 and item["accepted_qty"] >= target:
            status = "accepted"
        elif item["pending_inspection_qty"] > 0:
            status = "pending_inspection"
        elif item["delivered_qty"] >= target and target > 0:
            status = "fully_delivered"
        elif item["delivered_qty"] > 0:
            status = "partially_delivered"
        else:
            status = "not_delivered"
        item["status"] = status
        for field, value in list(item.items()):
            if isinstance(value, float):
                item[field] = _rounded(value)
        item["source_document_ids"] = list(dict.fromkeys(item["source_document_ids"]))
    return list(materials.values()), reconciliation_warnings


def rebuild_material_ledger(project_ref: str) -> list[dict[str, Any]]:
    project = resolve_project(project_ref)
    project_id = project["project_id"]
    documents = list(material_documents_collection.find({"project_id": project_id}))
    ledger, warnings = build_material_ledger(documents)
    project_materials_collection.delete_many({"project_id": project_id})
    now = utc_now()
    if ledger:
        project_materials_collection.insert_many(
            [
                {
                    **item,
                    "project_id": project_id,
                    "site_name": project["site_name"],
                    "floorplan_id": project["floorplan_id"],
                    "recalculated_at": now,
                }
                for item in ledger
            ]
        )
    if warnings:
        material_documents_collection.update_many(
            {"project_id": project_id, "status": "confirmed"},
            {"$set": {"last_reconciliation_warning_count": len(warnings)}},
        )
    return ledger


def get_material_ledger(project_ref: str) -> list[dict[str, Any]]:
    project = resolve_project(project_ref)
    cursor = project_materials_collection.find({"project_id": project["project_id"]}).sort(
        [("status", 1), ("description", 1)]
    )
    return [
        {key: value for key, value in item.items() if key != "_id"}
        for item in cursor
    ]


def get_material_summary(project_ref: str) -> dict[str, Any]:
    project = resolve_project(project_ref)
    project_id = project["project_id"]
    documents = list(material_documents_collection.find({"project_id": project_id}))
    ledger = get_material_ledger(project_id)
    active_documents = [item for item in documents if item.get("status") != "voided"]
    document_counts: dict[str, int] = defaultdict(int)
    for document in active_documents:
        document_counts[str(document.get("document_type") or "unknown")] += 1
    totals = {
        "planned_contract_value": 0.0,
        "committed_value": 0.0,
        "delivered_reference_value": 0.0,
        "accepted_reference_value": 0.0,
        "certified_contract_value": 0.0,
    }
    quantity_totals: dict[str, dict[str, float]] = {}
    status_counts: dict[str, int] = defaultdict(int)
    for item in ledger:
        for field in totals:
            source_field = "certified_value" if field == "certified_contract_value" else field
            totals[field] += _number(item.get(source_field))
        unit = str(item.get("unit") or "UNSPECIFIED")
        bucket = quantity_totals.setdefault(
            unit,
            {
                "planned_qty": 0.0,
                "ordered_qty": 0.0,
                "delivered_qty": 0.0,
                "accepted_qty": 0.0,
                "rejected_qty": 0.0,
                "pending_delivery_qty": 0.0,
                "pending_inspection_qty": 0.0,
            },
        )
        for field in bucket:
            bucket[field] += _number(item.get(field))
        status_counts[str(item.get("status") or "unknown")] += 1
    return {
        "project_id": project_id,
        "site_name": project["site_name"],
        "currency": next((str(item.get("currency")) for item in ledger if item.get("currency")), ""),
        "totals": {key: _rounded(value) for key, value in totals.items()},
        "quantity_totals": {
            unit: {key: _rounded(value) for key, value in values.items()}
            for unit, values in quantity_totals.items()
        },
        "document_counts": dict(document_counts),
        "status_counts": dict(status_counts),
        "material_count": len(ledger),
        "needs_review_count": sum(1 for item in active_documents if item.get("status") == "needs_review"),
        "confirmed_document_count": sum(1 for item in active_documents if item.get("status") == "confirmed"),
        "overdue_count": sum(1 for item in ledger if item.get("is_overdue") is True),
        "unit_warning_count": sum(len(item.get("warnings") or []) for item in ledger),
    }
