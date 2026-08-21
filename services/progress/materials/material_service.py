from __future__ import annotations

import hashlib
import io
import os
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
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
from services.progress.work_schedule.baseline_service import (
    project_currency_code,
    resolve_project,
)


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
MATERIAL_PROCESSING_VERSION = 10
MATERIAL_RESET_SCOPES = {"pending", "transactions", "all"}
AUTO_MATCH_MIN_CONFIDENCE = 0.78
AUTO_MATCH_MIN_MARGIN = 0.15
AUTO_CORRECT_DETECTED_TYPES = {
    "boq",
    "weekly_report",
    "purchase_order",
    "customer_shipment",
    "mir_grn",
    "progress_invoice",
}

_UNIT_ALIASES = {
    "MTR": "M",
    "METER": "M",
    "METRE": "M",
    "L.M": "LM",
    "L.M.": "LM",
    "RM": "LM",
    "LA": "LM",
    "LBA": "LM",
    "LI": "LM",
    "LT": "LM",
    "PC": "PCS",
    "FCS": "PCS",
    "POS": "PCS",
    "PFS": "PCS",
    "PSS": "PCS",
    "PCE": "PCS",
    "PIECE": "PCS",
    "EA": "PCS",
    "NO": "PCS",
    "NOS": "PCS",
    "TONNE": "TON",
}
_NUMBER = r"[+-]?[\d,]+(?:\.\d+)?"
_UNIT = r"LM|L\.M\.?|M2|M3|M|MTR|PCS?|PCE|EA|NO|NOS|KG|TON|TONNE|BAG|" r"ITEM|LOT|SET"
_DELIVERY_UNIT = rf"{_UNIT}|L(?:A|BA|I|T)|FCS|P(?:OS|FS|SS)"
_BOQ_LINE = re.compile(
    rf"^\s*(?P<item>\d+(?:\.\d+)*)\s+(?P<description>.{{3,}}?)\s+"
    rf"(?P<quantity>{_NUMBER})\s+(?P<unit>{_UNIT})\s+"
    rf"(?P<rate>{_NUMBER})\s+(?P<amount>{_NUMBER})\s*$",
    re.IGNORECASE,
)
_DELIVERY_LINE_PREFIX = (
    rf"^\s*(?:(?P<source_item>\d{{1,4}}(?:\.\d+)*)\s+)?"
    rf"(?:(?P<code>(?=[A-Z0-9./_-]*\d)[A-Z0-9][A-Z0-9./_-]{{3,}})\s+)?"
    rf"(?P<description>.{{4,}}?)\s+"
)
_OCR_ROW_TRAILING = r"(?:\s*[+|{}\[\]<>~`'\"]+\s*)*$"
_OCR_CELL_SEPARATOR = r"(?:\s+|\s*[.:;,]\s*)+"
_DELIVERY_LINE_PATTERNS = (
    re.compile(
        _DELIVERY_LINE_PREFIX
        + rf"(?P<quantity>{_NUMBER})\s+(?P<unit>{_DELIVERY_UNIT})"
        + _OCR_ROW_TRAILING,
        re.IGNORECASE,
    ),
    re.compile(
        _DELIVERY_LINE_PREFIX
        + rf"(?P<unit>{_DELIVERY_UNIT}){_OCR_CELL_SEPARATOR}(?P<quantity>{_NUMBER})"
        + _OCR_ROW_TRAILING,
        re.IGNORECASE,
    ),
)
_BOQ_BLOCK_START = re.compile(r"^\s*(?P<item>\d+(?:\.\d+)*)\s+(?P<description>.+)$")
_BOQ_BLOCK_VALUE = re.compile(
    rf"(?P<raw_quantity>[\d,]+)\s*(?P<unit>{_UNIT})\s+"
    rf"(?P<first_after_unit>{_NUMBER})\s+(?P<second_after_unit>{_NUMBER})"
    rf"(?:\s+(?P<third_after_unit>{_NUMBER}))?",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}-[A-Za-z]{3}-\d{2,4})\b"
)
_REPORT_DATE = re.compile(
    r"\b(?:"
    r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|"
    r"\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}|"
    r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r")\b",
    re.IGNORECASE,
)
_WEEKLY_ROW_START = re.compile(r"^\s*(?P<item>\d+(?:\.\d+)*)\s*(?P<body>.*)$")
_WEEKLY_REFERENCE = re.compile(
    r"(?P<reference>HARDSCAPE|ELECTRICAL|IRRIGATION|SOFTSCAPE)\s*WORK\b",
    re.IGNORECASE,
)
_INVOICE_ROW_START = re.compile(
    r"^\s*(?P<item>[A-Z](?:\.)?|\d+(?:[-.]\d+)*)\s+(?P<body>.+)$",
    re.IGNORECASE,
)
_INVOICE_PROGRESS_PAIR = re.compile(
    rf"(?P<percent>[\d,]+(?:\.\d+)?)\s*%\s*(?P<amount>{_NUMBER}|-)"
)
_INVOICE_PREFIX_QTY_UNIT = re.compile(
    rf"^(?P<description>.+?)\s+(?P<quantity>{_NUMBER})\s+(?P<unit>{_UNIT})\s+"
    rf"(?P<rate>{_NUMBER})\s+(?P<amount>{_NUMBER})\s*$",
    re.IGNORECASE,
)
_INVOICE_PREFIX_UNIT_QTY = re.compile(
    rf"^(?P<description>.+?)\s+(?P<unit>{_UNIT})\s+(?P<quantity>{_NUMBER})\s+"
    rf"(?P<rate>{_NUMBER})\s+(?P<amount>{_NUMBER})\s*$",
    re.IGNORECASE,
)
_REFERENCE_IDENTIFIER_LABEL = re.compile(
    r"^(?:DELIVERY NOTE|DELIVERY ID|PACK ID|PACK DELIVERY ID|"
    r"ORDER NO|ORDER NUMBER|ORDER ID|PO NO|PO NUMBER|PO ID|"
    r"MIR|MIR NO|MIR NUMBER|MIR SN|GRN|GRN NO|GRN NUMBER|"
    r"DOCUMENT NO|DOCUMENT NUMBER|CUSTOMER SHIPMENT)$"
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


def preserve_matching_boq_material_ids(
    new_lines: Iterable[dict[str, Any]],
    active_lines: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Reuse stable IDs for unchanged BOQ rows during a baseline replacement."""
    active_by_item: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    active_by_description: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(
        list
    )
    for raw_line in active_lines:
        line = dict(raw_line)
        material_id = str(line.get("material_id") or "").strip()
        unit = normalize_unit(line.get("unit"))
        description = normalize_description(line.get("description"))
        item_number = str(line.get("item_number") or "").strip()
        if not material_id or not unit or not description:
            continue
        if item_number:
            active_by_item[(item_number, unit)].append(line)
        active_by_description[(description, unit)].append(line)

    output: list[dict[str, Any]] = []
    reused = 0
    for raw_line in new_lines:
        line = dict(raw_line)
        if str(line.get("material_id") or "").strip():
            output.append(line)
            continue
        unit = normalize_unit(line.get("unit"))
        description = normalize_description(line.get("description"))
        item_number = str(line.get("item_number") or "").strip()
        candidates = active_by_item.get((item_number, unit), []) if item_number else []
        if len(candidates) != 1 or (
            description
            and normalize_description(candidates[0].get("description")) != description
        ):
            candidates = active_by_description.get((description, unit), [])
        if len(candidates) == 1:
            line["material_id"] = candidates[0]["material_id"]
            reused += 1
        output.append(line)
    return output, reused


def document_matches_material_reset_scope(document: dict[str, Any], scope: str) -> bool:
    if scope == "pending":
        return document.get("status") == "needs_review"
    if scope == "transactions":
        return document.get("document_type") != "boq"
    if scope == "all":
        return True
    raise HTTPException(400, f"Unsupported material reset scope: {scope}")


def _description_tokens(value: Any) -> set[str]:
    ignored = {
        "A",
        "AN",
        "AND",
        "AS",
        "FOR",
        "OF",
        "PER",
        "S",
        "SUPPLY",
        "THE",
        "WITH",
    }
    return {
        token
        for token in normalize_description(value).split()
        if token not in ignored and len(token) > 1
    }


def _nominal_diameter_mm(value: Any) -> float:
    """Return the first pipe diameter, including values such as 50x1.8 mm."""
    text = str(value or "").upper().replace("×", "X")
    match = re.search(
        r"(?<![A-Z0-9])(?P<diameter>\d{1,4}(?:\.\d+)?)"
        r"(?:\s*X\s*\d+(?:\.\d+)?)?\s*MM\b",
        text,
    )
    return _number(match.group("diameter")) if match else 0.0


_DIMENSION_TRIPLET = re.compile(
    r"(?<![A-Z0-9])(?P<a>[0-9SOG]{1,4}(?:\.[0-9]+)?)\s*[XK]\s*"
    r"(?P<b>[0-9SOG]{1,4}(?:\.[0-9]+)?)\s*[XK]\s*"
    r"(?P<c>[0-9SOG]{1,4}(?:\.[0-9]+)?)(?:\s*(?P<unit>CM|MM))?\b",
    re.IGNORECASE,
)


def _ocr_dimension_number(value: Any) -> float:
    corrected = (
        str(value or "")
        .upper()
        .translate(str.maketrans({"S": "5", "O": "0", "G": "3"}))
    )
    return _number(corrected)


def _looks_like_kerbstone_word(value: Any) -> bool:
    word = re.sub(r"[^A-Z]", "", str(value or "").upper())
    return (
        7 <= len(word) <= 12
        and "STONE" in word
        and SequenceMatcher(None, word, "KERBSTONE").ratio() >= 0.62
    )


def _is_kerbstone_family(value: Any) -> bool:
    normalized = normalize_description(value)
    if any(
        marker in normalized
        for marker in ("KERBSTONE", "KERB STONE", "KERSTONE", "CURBSTONE", "CURB")
    ):
        return True
    words = normalized.split()
    return any(_looks_like_kerbstone_word(word) for word in words) or any(
        _looks_like_kerbstone_word(words[index] + words[index + 1])
        for index in range(len(words) - 1)
    )


def _normalize_dimension_ocr_text(value: Any) -> str:
    text = str(value or "").upper().replace("×", "X")
    # Common table OCR defects: ``100M`` for ``10CM`` and ``K`` for an X
    # separator. Corrections stay inside dimension-shaped tokens.
    text = re.sub(r"(?<![A-Z0-9])([0-9SO]0)0M\b", r"\1CM", text)
    text = re.sub(r"(?<=[0-9SOG])K\s*(?=[0-9SOG])", "X", text)
    text = re.sub(r"(?<=[0-9])C(?:R|H)\b", "CM", text)
    return text


def _dimension_signature_mm(value: Any) -> tuple[float, ...]:
    """Return a sorted three-dimension signature, correcting narrow OCR errors."""
    text = _normalize_dimension_ocr_text(value)
    match = _DIMENSION_TRIPLET.search(text)
    if not match:
        return ()
    dimensions = [_ocr_dimension_number(match.group(name)) for name in ("a", "b", "c")]
    if any(dimension <= 0 for dimension in dimensions):
        return ()
    unit = str(match.group("unit") or "").upper()
    if unit == "CM" or (
        not unit and max(dimensions) <= 100 and _is_kerbstone_family(value)
    ):
        dimensions = [dimension * 10 for dimension in dimensions]
    return tuple(sorted(round(dimension, 4) for dimension in dimensions))


def _clean_delivery_description(value: Any) -> str:
    """Clean only high-confidence OCR defects without rewriting source meaning."""
    description = " ".join(str(value or "").split()).strip(" -:")
    description = re.sub(
        r"\b([A-Z]{2,8})\s+TONE\b",
        lambda match: (
            "KERBSTONE"
            if _looks_like_kerbstone_word(match.group(1) + "TONE")
            else match.group(0)
        ),
        description,
        flags=re.IGNORECASE,
    )
    description = re.sub(
        r"\b[A-Z]{7,12}\b",
        lambda match: (
            "KERBSTONE"
            if _looks_like_kerbstone_word(match.group(0))
            else match.group(0)
        ),
        description,
        flags=re.IGNORECASE,
    )
    description = re.sub(
        r"^(?:AI|AL)\s+(?=(?:KERB|CURB))",
        "",
        description,
        flags=re.IGNORECASE,
    )
    description = re.sub(r"\bKERSTONE\b", "KERBSTONE", description, flags=re.IGNORECASE)

    def replace_dimensions(match: re.Match[str]) -> str:
        values = [
            f"{_ocr_dimension_number(match.group(name)):g}" for name in ("a", "b", "c")
        ]
        return "X".join(values) + str(match.group("unit") or "").upper()

    dimension_text = description.replace("×", "X")
    dimension_text = re.sub(
        r"(?<![A-Z0-9])([0-9SO]0)0M\b",
        r"\1CM",
        dimension_text,
        flags=re.IGNORECASE,
    )
    dimension_text = re.sub(
        r"(?<=[0-9SOG])K\s*(?=[0-9SOG])",
        "X",
        dimension_text,
        flags=re.IGNORECASE,
    )
    dimension_text = re.sub(
        r"(?<=[0-9])C(?:R|H)\b", "CM", dimension_text, flags=re.IGNORECASE
    )
    return _DIMENSION_TRIPLET.sub(replace_dimensions, dimension_text)


def _piece_length_m(value: Any) -> float:
    """Return an explicit per-piece length such as 6 Mt, 6 MTR, or 6 metres."""
    text = str(value or "").upper()
    matches = re.findall(
        r"(?<![A-Z0-9.])(?P<length>\d+(?:\.\d+)?)\s*"
        r"(?:METRES?|METERS?|MTRS?|MTS?|MT|M)(?![A-Z])",
        text,
    )
    sensible = [_number(value) for value in matches if 0 < _number(value) <= 30]
    if sensible:
        return sensible[-1]
    dimensions = _dimension_signature_mm(value)
    if dimensions and _is_kerbstone_family(value):
        return round(max(dimensions) / 1000, 4)
    return 0.0


def _polymer_family(value: Any) -> str:
    normalized = normalize_description(value)
    if "UPVC" in normalized:
        return "UPVC"
    if "PVC" in normalized:
        return "PVC"
    if "HDPE" in normalized:
        return "HDPE"
    return ""


def _is_pipe_family(value: Any) -> bool:
    tokens = _description_tokens(value)
    return bool(
        tokens.intersection(
            {
                "PIPE",
                "PIPES",
                "CONDUIT",
                "CONDUITS",
                "DUCT",
                "DUCTS",
                "SLEEVE",
                "SLEEVES",
            }
        )
    )


def _source_dimensions_mm(source: Any) -> tuple[float, ...]:
    if isinstance(source, dict):
        direct = _dimension_signature_mm(source.get("description"))
        if direct:
            return direct
        inferred = source.get("bundle_dimension_signature_mm") or []
        values = tuple(
            sorted(_number(value) for value in inferred if _number(value) > 0)
        )
        return values if len(values) == 3 else ()
    return _dimension_signature_mm(source)


def _source_piece_length_m(source: Any) -> float:
    description = source.get("description") if isinstance(source, dict) else source
    explicit = _piece_length_m(description)
    if explicit:
        return explicit
    dimensions = _source_dimensions_mm(source)
    if dimensions and _is_kerbstone_family(description):
        return round(max(dimensions) / 1000, 4)
    return 0.0


def _can_convert_units(source_unit: str, baseline_unit: str, source: Any) -> bool:
    return (
        normalize_unit(source_unit) == "PCS"
        and normalize_unit(baseline_unit) in {"LM", "M"}
        and _source_piece_length_m(source) > 0
    )


def _material_match_score(
    source_line: dict[str, Any], material: dict[str, Any]
) -> tuple[float, list[str]]:
    source_description = str(source_line.get("description") or "")
    baseline_description = str(material.get("description") or "")
    source_tokens = _description_tokens(source_description)
    baseline_tokens = _description_tokens(baseline_description)
    overlap = source_tokens.intersection(baseline_tokens)
    union = source_tokens.union(baseline_tokens)
    score = (len(overlap) / len(union) * 0.22) if union else 0.0
    reasons: list[str] = []

    source_polymer = _polymer_family(source_description)
    baseline_polymer = _polymer_family(baseline_description)
    if source_polymer and baseline_polymer:
        if source_polymer == baseline_polymer:
            score += 0.24
            reasons.append(f"same {source_polymer} material family")
        elif {source_polymer, baseline_polymer} == {"UPVC", "PVC"}:
            score += 0.16
            reasons.append("compatible PVC material family")
        else:
            score -= 0.25

    source_diameter = _nominal_diameter_mm(source_description)
    baseline_diameter = _nominal_diameter_mm(baseline_description)
    if source_diameter and baseline_diameter:
        if abs(source_diameter - baseline_diameter) < 0.01:
            score += 0.42
            reasons.append(f"same {source_diameter:g} mm nominal diameter")
        else:
            score -= 0.6

    if _is_pipe_family(source_description) and _is_pipe_family(baseline_description):
        score += 0.16
        reasons.append("same pipe/conduit family")

    source_dimensions = _source_dimensions_mm(source_line)
    baseline_dimensions = _dimension_signature_mm(baseline_description)
    if _is_kerbstone_family(source_description) and _is_kerbstone_family(
        baseline_description
    ):
        score += 0.38
        reasons.append("same kerbstone/curb material family")
        if source_dimensions and baseline_dimensions:
            if source_dimensions == baseline_dimensions:
                score += 0.44
                reasons.append("same three-dimensional kerbstone size")
            else:
                score -= 0.65

    source_unit = normalize_unit(source_line.get("unit"))
    baseline_unit = normalize_unit(material.get("unit"))
    if source_unit and source_unit == baseline_unit:
        score += 0.08
        reasons.append("same unit")
    elif _can_convert_units(source_unit, baseline_unit, source_line):
        score += 0.08
        reasons.append("explicit per-piece length supports unit conversion")

    return max(0.0, min(round(score, 4), 1.0)), reasons


def enrich_delivery_lines_with_baseline(
    lines: list[dict[str, Any]], materials: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Add auditable match/conversion suggestions without losing source values."""
    baseline_materials = list(materials)
    prepared_lines = _propagate_bundle_delivery_dimensions(lines)
    enriched: list[dict[str, Any]] = []
    for source in prepared_lines:
        line = dict(source)
        source_unit = normalize_unit(line.get("unit"))
        source_qty = _number(line.get("delivered_qty"))
        source_description = str(line.get("description") or "").strip()
        line.update(
            source_description=source_description,
            source_unit=source_unit,
            source_delivered_qty=source_qty,
        )

        ranked = sorted(
            (
                (*_material_match_score(line, material), material)
                for material in baseline_materials
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if not ranked:
            enriched.append(line)
            continue

        top_score, top_reasons, top_material = ranked[0]
        runner_up_score = ranked[1][0] if len(ranked) > 1 else 0.0
        if (
            top_score < AUTO_MATCH_MIN_CONFIDENCE
            or top_score - runner_up_score < AUTO_MATCH_MIN_MARGIN
        ):
            line.update(
                match_status="needs_review",
                match_confidence=top_score,
                match_method="deterministic_description_and_dimension_match",
            )
            enriched.append(line)
            continue

        target_id = str(top_material.get("material_id") or "")
        target_unit = normalize_unit(top_material.get("unit"))
        line.update(
            suggested_material_id=target_id,
            suggested_material_description=str(top_material.get("description") or ""),
            suggested_material_unit=target_unit,
            suggested_boq_item_number=str(top_material.get("boq_item_number") or ""),
            linked_material_id=target_id,
            match_status="suggested",
            match_confidence=top_score,
            match_reasons=top_reasons,
            match_method="deterministic_description_and_dimension_match",
        )

        if source_unit == target_unit:
            line["conversion_status"] = "not_required"
        elif _can_convert_units(source_unit, target_unit, line):
            factor = _source_piece_length_m(line)
            converted_qty = _rounded(source_qty * factor)
            line.update(
                unit=target_unit,
                delivered_qty=converted_qty,
                piece_length_m=factor,
                conversion_factor=factor,
                conversion_factor_unit="M_PER_PCS",
                converted_qty=converted_qty,
                converted_unit=target_unit,
                conversion_formula=f"{source_qty:g} PCS x {factor:g} m/PCS = {converted_qty:g} {target_unit}",
                conversion_status="suggested",
                conversion_confidence=0.95,
            )
        else:
            # Suggest the material but require a human-entered compatible quantity.
            line["linked_material_id"] = ""
            line["match_status"] = "needs_review"
            line["conversion_status"] = "unsupported"
        enriched.append(line)
    return enriched


def enrich_mir_lines_with_baseline(
    lines: list[dict[str, Any]], materials: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Reuse the auditable delivery matcher for MIR quantities and descriptions."""
    delivery_shaped: list[dict[str, Any]] = []
    for source in lines:
        line = dict(source)
        line["delivered_qty"] = _number(line.get("inspected_qty"))
        delivery_shaped.append(line)
    enriched = enrich_delivery_lines_with_baseline(delivery_shaped, materials)
    output: list[dict[str, Any]] = []
    for enriched_line in enriched:
        line = dict(enriched_line)
        inspected_qty = _number(line.pop("delivered_qty", 0))
        source_qty = _number(line.get("source_delivered_qty"))
        line["source_inspected_qty"] = source_qty
        line["inspected_qty"] = inspected_qty
        result = str(line.get("inspection_result") or "pending").lower()
        line["accepted_qty"] = inspected_qty if result.startswith("accepted") else 0.0
        line["rejected_qty"] = inspected_qty if result == "rejected" else 0.0
        output.append(line)
    return output


def classify_material_document(text: str, filename: str = "") -> str:
    sample = f"{filename}\n{text[:30000]}".lower()
    rules = (
        ("mir_grn", ("material inspection request", "goods received note", " mir ")),
        (
            "weekly_report",
            ("weekly report", "material delivery status", "long lead item"),
        ),
        ("customer_shipment", ("customer shipment", "pack/delivery id")),
        ("delivery_note", ("delivery note", "delivery challan")),
        ("purchase_order", ("purchase order", "p.o. number", "po number")),
        (
            "progress_invoice",
            ("progress invoice", "interim payment", "payment certificate"),
        ),
        ("boq", ("bill of quantities", "priced boq", " boq ")),
    )
    padded = f" {sample} "
    for document_type, keywords in rules:
        if any(keyword in padded for keyword in keywords):
            return document_type
    return "boq" if "boq" in Path(filename).stem.lower() else "delivery_note"


def resolve_material_document_type(
    requested_type: str, detected_type: str
) -> tuple[str, bool]:
    """Auto-correct only strong classifications; delivery_note is the fallback."""
    if requested_type == "auto":
        return detected_type, False
    should_correct = (
        requested_type != detected_type and detected_type in AUTO_CORRECT_DETECTED_TYPES
    )
    return (detected_type, True) if should_correct else (requested_type, False)


def _looks_like_reference_identifier_row(description: str) -> bool:
    """Reject headers such as `Delivery Note NO 374694` as material rows."""
    normalized = normalize_description(description)
    if _REFERENCE_IDENTIFIER_LABEL.fullmatch(normalized):
        return True
    return "DELIVERY" in normalized and (
        normalized.endswith("DELIVERY")
        or normalized.endswith("DELIVERY NO")
        or any(
            marker in normalized
            for marker in (
                "MANUFACTUR",
                "FACTERY",
                "FACTORY",
                "CUSTOMER NAME",
                "TRANSPORT NAME",
                "DRIVER NAME",
            )
        )
    )


def _has_material_description_signal(description: str) -> bool:
    """Require at least one readable material word, not only OCR fragments."""
    return any(
        len(token) >= 3 and bool(re.search(r"[A-Z]", token))
        for token in normalize_description(description).split()
    )


def _source_date(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip().replace("Sept", "Sep"))
    cleaned = re.sub(r"(?<=[A-Za-z])\.(?=,?\s+\d{4})", "", cleaned)
    for pattern in (
        "%b %d, %Y",
        "%b %d %Y",
        "%B %d, %Y",
        "%B %d %Y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d.%m.%Y",
        "%d-%b-%Y",
    ):
        try:
            return datetime.strptime(cleaned, pattern).date().isoformat()
        except ValueError:
            continue
    return cleaned


def _extraction_footer(
    lines: list[dict[str, Any]], warnings: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
            "message": "Document extraction is best effort; confirmation is required before ledger posting.",
        }
    )
    return lines, warnings


def _weekly_report_lines(page_text: str, page_number: int) -> list[dict[str, Any]]:
    upper = normalize_description(page_text)
    is_long_lead = "LONG LEAD ITEM" in upper
    if "LIST OF MATERIAL" not in upper and not is_long_lead:
        return []

    blocks: list[tuple[str, str]] = []
    current_item = ""
    current_parts: list[str] = []
    for raw_line in page_text.splitlines():
        compact = " ".join(raw_line.split())
        if not compact:
            continue
        start = _WEEKLY_ROW_START.match(compact)
        if start and start.group("body").strip():
            if current_item:
                blocks.append((current_item, " ".join(current_parts)))
            current_item = start.group("item")
            current_parts = [start.group("body").strip()]
        elif current_item:
            current_parts.append(compact)
    if current_item:
        blocks.append((current_item, " ".join(current_parts)))

    output: list[dict[str, Any]] = []
    for item_number, body in blocks:
        reference_match = _WEEKLY_REFERENCE.search(body)
        if not reference_match:
            continue
        description = body[: reference_match.start()].strip(" -:.")
        if len(normalize_description(description)) < 4:
            continue
        dates = [_source_date(match.group(0)) for match in _REPORT_DATE.finditer(body)]
        normalized_body = normalize_description(body)
        if "NOT DELIVERED" in normalized_body:
            approval_status = "not_delivered"
        elif "DELIVERED" in normalized_body:
            approval_status = "delivered"
        elif "PO ISSUED" in normalized_body:
            approval_status = "po_issued"
        elif "APPROVED" in normalized_body:
            approval_status = "approved"
        elif "SUBMITTED" in normalized_body:
            approval_status = "submitted"
        else:
            approval_status = "reported"
        if not dates and approval_status == "reported":
            continue

        line: dict[str, Any] = {
            "line_id": f"line_{uuid4().hex}",
            "source_page": page_number,
            "item_number": item_number,
            "description": description,
            "unit": "",
            "reference_category": reference_match.group("reference").title(),
            "approval_status": approval_status,
            "source_dates": dates,
            "confidence": 0.72,
            "warnings": [
                "Weekly reports provide delivery dates/status, not measured material quantities. Link the row to the BOQ material."
            ],
        }
        if is_long_lead:
            if dates:
                line["expected_delivery_date"] = dates[0]
            if len(dates) > 1:
                line["actual_delivery_date"] = dates[1]
        else:
            if dates:
                line["submittal_date"] = dates[0]
            if len(dates) > 1:
                line["approval_date"] = dates[1]
            if len(dates) > 2:
                line["expected_delivery_date"] = dates[2]
            if len(dates) > 3:
                line["actual_delivery_date"] = dates[3]
            elif approval_status == "delivered" and len(dates) == 3:
                line["actual_delivery_date"] = dates[2]
        output.append(line)
    return output


def _delivery_lines_for_page(
    page_text: str, page_number: int, *, document_type: str
) -> tuple[list[dict[str, Any]], bool]:
    raw_lines = [" ".join(value.split()) for value in page_text.splitlines()]
    compact_lines = [value for value in raw_lines if value]
    candidates = list(compact_lines)
    standalone_row_indexes = {
        index
        for index, value in enumerate(compact_lines)
        if any(pattern.match(value) for pattern in _DELIVERY_LINE_PATTERNS)
    }
    # Scanned delivery notes frequently split the description, unit, and
    # quantity across several OCR lines. Always examine short adjacent windows;
    # never extend beyond an already complete row, and containment
    # de-duplication below keeps a clean direct row when available.
    for size in (2, 3, 4):
        candidates.extend(
            " ".join(compact_lines[index : index + size])
            for index in range(max(0, len(compact_lines) - size + 1))
            if not any(
                row_index in standalone_row_indexes
                for row_index in range(index, index + size - 1)
            )
        )

    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float]] = set()
    reference_ignored = False
    for compact in candidates:
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
        source_description_ocr = values["description"].strip(" -:")
        description = _clean_delivery_description(source_description_ocr)
        if _looks_like_reference_identifier_row(description):
            reference_ignored = True
            continue
        normalized = normalize_description(description)
        if not _has_material_description_signal(description) or any(
            marker in normalized
            for marker in (
                "DESCRIPTION QTY",
                "DESCRIPTION OF MATERIAL",
                "ITEM DESCRIPTION",
                "QUANTITY UNIT",
                "DELIVERY NOTE DATE",
                "CUSTOMER SHIPMENT DATE",
            )
        ):
            continue
        quantity = abs(_number(values["quantity"]))
        if quantity <= 0:
            continue
        unit = normalize_unit(values["unit"])
        key = (normalized, unit, quantity)
        if key in seen:
            continue
        contained_index = next(
            (
                index
                for index, existing in enumerate(output)
                if existing.get("unit") == unit
                and _number(existing.get("ordered_qty", existing.get("delivered_qty")))
                == quantity
                and (
                    normalize_description(existing.get("description")) in normalized
                    or normalized in normalize_description(existing.get("description"))
                )
            ),
            None,
        )
        if contained_index is not None:
            existing_description = normalize_description(
                output[contained_index].get("description")
            )
            existing_specificity = sum(
                (
                    bool(_dimension_signature_mm(existing_description)),
                    _is_kerbstone_family(existing_description),
                    _is_pipe_family(existing_description),
                )
            )
            candidate_specificity = sum(
                (
                    bool(_dimension_signature_mm(description)),
                    _is_kerbstone_family(description),
                    _is_pipe_family(description),
                )
            )
            if candidate_specificity < existing_specificity or (
                candidate_specificity == existing_specificity
                and len(normalized) >= len(existing_description)
            ):
                continue
            output.pop(contained_index)
        seen.add(key)
        field = "ordered_qty" if document_type == "purchase_order" else "delivered_qty"
        line = {
            "line_id": f"line_{uuid4().hex}",
            "source_page": page_number,
            "item_number": values.get("source_item") or "",
            "material_code": values.get("code") or "",
            "description": description,
            "source_description_ocr": source_description_ocr,
            "unit": unit,
            "source_unit_ocr": str(values.get("unit") or "").upper(),
            field: quantity,
            "confidence": 0.67,
            "warnings": ["Link this row to a confirmed BOQ baseline material."],
        }
        page_context = _delivery_page_context(page_text, page_number)
        page_context.pop("delivery_number_candidates", None)
        line.update(page_context)
        output.append(line)
    return _consolidate_delivery_page_rows(output), reference_ignored


def _delivery_row_quality(line: dict[str, Any]) -> float:
    description = str(line.get("description") or "")
    normalized = normalize_description(description)
    score = 0.0
    if _is_kerbstone_family(description):
        score += 5
    if _dimension_signature_mm(description):
        score += 8
    if "GREY" in normalized:
        score += 2
    if "WITHOUT CHAMFER" in normalized:
        score += 2
    if normalize_unit(line.get("source_unit_ocr")) == normalize_unit(line.get("unit")):
        score += 1
    score -= max(len(normalized) - 90, 0) / 30
    return score


def _consolidate_delivery_page_rows(
    lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse repeated OCR variants while preserving distinct material rows."""
    output: list[dict[str, Any]] = []
    for line in lines:
        if not _is_kerbstone_family(line.get("description")):
            output.append(line)
            continue
        source_qty = _number(line.get("ordered_qty", line.get("delivered_qty")))
        source_dimensions = _dimension_signature_mm(line.get("description"))
        duplicate_index: int | None = None
        for index, existing in enumerate(output):
            if not _is_kerbstone_family(existing.get("description")):
                continue
            if normalize_unit(existing.get("unit")) != normalize_unit(line.get("unit")):
                continue
            existing_qty = _number(
                existing.get("ordered_qty", existing.get("delivered_qty"))
            )
            if abs(existing_qty - source_qty) > max(10, source_qty * 0.05):
                continue
            existing_dimensions = _dimension_signature_mm(existing.get("description"))
            if (
                existing_dimensions
                and source_dimensions
                and existing_dimensions != source_dimensions
            ):
                continue
            duplicate_index = index
            break
        if duplicate_index is None:
            output.append(line)
        else:
            existing = output[duplicate_index]
            alternatives = {
                _number(value)
                for value in (
                    *existing.get("ocr_quantity_alternatives", []),
                    existing.get("ordered_qty", existing.get("delivered_qty")),
                    *line.get("ocr_quantity_alternatives", []),
                    line.get("ordered_qty", line.get("delivered_qty")),
                )
                if _number(value) > 0
            }
            selected = (
                line
                if _delivery_row_quality(line) > _delivery_row_quality(existing)
                else existing
            )
            selected["ocr_quantity_alternatives"] = sorted(alternatives)
            output[duplicate_index] = selected
    return output


def _propagate_bundle_delivery_quantities(
    lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve a narrow OCR quantity outlier from a strong multi-page consensus."""
    output = [dict(line) for line in lines]
    kerbstone_lines = [
        line for line in output if _is_kerbstone_family(line.get("description"))
    ]
    if len({line.get("source_page") for line in kerbstone_lines}) < 3:
        return output
    frequencies: dict[float, int] = defaultdict(int)
    for line in kerbstone_lines:
        quantities = {
            _number(line.get("delivered_qty")),
            *(_number(value) for value in line.get("ocr_quantity_alternatives", [])),
        }
        for quantity in quantities:
            if quantity > 0:
                frequencies[quantity] += 1
    if not frequencies:
        return output
    consensus, support = max(frequencies.items(), key=lambda item: (item[1], item[0]))
    if support < 3:
        return output
    for line in kerbstone_lines:
        selected = _number(line.get("delivered_qty"))
        alternatives = {
            _number(value) for value in line.get("ocr_quantity_alternatives", [])
        }
        if (
            selected != consensus
            and consensus in alternatives
            and abs(selected - consensus) <= max(10, consensus * 0.05)
        ):
            line["source_quantity_ocr_selected"] = selected
            line["delivered_qty"] = consensus
            line["bundle_quantity_consensus"] = consensus
            line.setdefault("warnings", []).append(
                "An OCR quantity variant was resolved from the repeated quantity across this delivery-note bundle."
            )
    return output


def _propagate_bundle_delivery_dimensions(
    lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output = _propagate_bundle_delivery_quantities(lines)
    kerbstone_lines = [
        line for line in output if _is_kerbstone_family(line.get("description"))
    ]
    signatures: dict[tuple[float, ...], list[dict[str, Any]]] = defaultdict(list)
    for line in kerbstone_lines:
        signature = _dimension_signature_mm(line.get("description"))
        if signature:
            signatures[signature].append(line)
    if not signatures:
        return output
    signature, evidence = max(signatures.items(), key=lambda item: len(item[1]))
    if len(signature) != 3 or (len(kerbstone_lines) > 1 and len(evidence) < 2):
        return output
    representative = max(evidence, key=_delivery_row_quality)
    canonical_description = str(representative.get("description") or "")
    evidence_pages = sorted(
        {
            int(line.get("source_page") or 0)
            for line in evidence
            if int(line.get("source_page") or 0) > 0
        }
    )
    for line in kerbstone_lines:
        direct = _dimension_signature_mm(line.get("description"))
        if direct and direct != signature:
            continue
        line["bundle_dimension_signature_mm"] = list(signature)
        line["bundle_dimension_evidence_pages"] = evidence_pages
        if not direct:
            line["description"] = canonical_description
            line.setdefault("warnings", []).append(
                "Dimensions were normalized from matching kerbstone rows on other pages in this PDF bundle."
            )
    return output


def _delivery_page_context(page_text: str, page_number: int) -> dict[str, Any]:
    compact = " ".join(str(page_text or "").split())
    delivery_match = re.search(
        r"(?:DELIVERY\s*(?:NOTE\s*)?(?:NO|NUMBER|#)|DELIVERY\s+NO)"
        r"\s*[:.\-]?\s*(?P<number>\d{4,})",
        compact,
        re.IGNORECASE,
    )
    po_match = re.search(
        r"(?:PURCHASE\s*ORDER|CUSTOMER\s*ORDER|PO)\s*(?:NO|NUMBER|#)?"
        r"\s*[:.\-]?\s*(?P<number>(?=[A-Z0-9/\-]*\d)[A-Z0-9][A-Z0-9/\-]{3,})",
        compact,
        re.IGNORECASE,
    )
    agc_match = re.search(
        r"\bAGC\s*[0-9SO]{4,}(?:\s*[-/]\s*[0-9SO])?\b",
        compact,
        re.IGNORECASE,
    )
    dates = _DATE.findall(compact)
    delivery_area = re.search(r"DELIV.{0,220}", compact, re.IGNORECASE)
    delivery_dates = _DATE.findall(delivery_area.group(0)) if delivery_area else []
    delivery_number = delivery_match.group("number") if delivery_match else ""
    numeric_candidates = []
    for raw_number in re.findall(r"(?<!\d)\d{5,7}(?!\d)", compact):
        numeric_candidates.extend(
            raw_number[index : index + 5]
            for index in range(max(1, len(raw_number) - 4))
        )
    po_reference = po_match.group("number") if po_match else ""
    if agc_match:
        po_reference = re.sub(r"\s+", "", agc_match.group(0)).upper()
    return {
        "source_page": page_number,
        "delivery_note_number": delivery_number,
        "source_document_number": delivery_number,
        "source_document_date": (
            delivery_dates[-1] if delivery_dates else dates[-1] if dates else ""
        ),
        "po_reference": po_reference,
        "delivery_number_candidates": list(dict.fromkeys(numeric_candidates)),
    }


def _reconcile_delivery_contexts_with_filename(
    contexts: list[dict[str, Any]], filename: str
) -> list[dict[str, Any]]:
    """Use a bundled filename only when every page has matching OCR evidence."""
    output = [dict(context) for context in contexts]
    filename_numbers = re.findall(r"(?<!\d)(\d{5})(?!\d)", Path(filename).stem)
    if len(filename_numbers) == len(output) and len(output) > 1:
        expected_by_page = list(reversed(filename_numbers))
        evidence_matches = []
        for context, expected in zip(output, expected_by_page):
            candidates = [
                str(candidate)
                for candidate in context.get("delivery_number_candidates", [])
            ]
            best = max(
                (
                    SequenceMatcher(None, candidate, expected).ratio()
                    for candidate in candidates
                ),
                default=0.0,
            )
            evidence_matches.append(best)
        if all(score >= 0.55 for score in evidence_matches):
            for context, expected in zip(output, expected_by_page):
                context["delivery_note_number"] = expected
                context["source_document_number"] = expected
                context["document_number_source"] = "filename_reconciled_with_page_ocr"

    po_references = [
        str(context.get("po_reference") or "")
        for context in output
        if re.search(r"\d", str(context.get("po_reference") or ""))
    ]
    agc_references = [
        reference for reference in po_references if reference.startswith("AGC")
    ]
    shared_po_reference = max(
        agc_references or po_references,
        key=lambda reference: (reference.startswith("AGC"), len(reference)),
        default="",
    )
    if shared_po_reference:
        for context in output:
            if not re.search(r"\d", str(context.get("po_reference") or "")):
                context["po_reference"] = shared_po_reference

    for context in output:
        context.pop("delivery_number_candidates", None)
    return output


def _delivery_page_contexts(
    pages: Iterable[str], *, filename: str = ""
) -> list[dict[str, Any]]:
    contexts = [
        _delivery_page_context(page_text, page_number)
        for page_number, page_text in enumerate(pages, start=1)
    ]
    return _reconcile_delivery_contexts_with_filename(contexts, filename)


def _apply_delivery_page_contexts(
    lines: list[dict[str, Any]], contexts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_page = {int(context.get("source_page") or 0): context for context in contexts}
    output: list[dict[str, Any]] = []
    for source in lines:
        line = dict(source)
        line.pop("delivery_number_candidates", None)
        context = by_page.get(int(line.get("source_page") or 0))
        if context:
            for key in (
                "delivery_note_number",
                "source_document_number",
                "source_document_date",
                "po_reference",
                "document_number_source",
            ):
                line[key] = context.get(key, "")
        output.append(line)
    return output


def _mir_lines(pages: list[str]) -> list[dict[str, Any]]:
    combined = " ".join(" ".join(page.split()) for page in pages)
    description_match = re.search(
        r"Description\s+of\s+material\s+(.+?)(?=\s+Technical\s+submittal|\s+Delivery\s+Note|\s+Supplier\s+name|$)",
        combined,
        re.IGNORECASE,
    )
    description = description_match.group(1).strip(" -:") if description_match else ""
    delivery_match = re.search(
        r"Delivery\s+Note\s*(?:NO|NUMBER|#)?\s*[:.-]?\s*([A-Z0-9/-]+)",
        combined,
        re.IGNORECASE,
    )
    supplier_match = re.search(
        r"Supplier\s+name\s+(.+?)(?=\s+Received\s+By|\s+MIR\s+Attachments|\s+INSPECTION\s+RESULTS|$)",
        combined,
        re.IGNORECASE,
    )

    shipment_rows: list[dict[str, Any]] = []
    for page_number, page_text in enumerate(pages, start=1):
        page_rows, _ = _delivery_lines_for_page(
            page_text, page_number, document_type="customer_shipment"
        )
        shipment_rows.extend(page_rows)
    selected = (
        max(
            shipment_rows,
            key=lambda row: (
                bool(str(row.get("material_code") or "").strip()),
                _number(row.get("delivered_qty")) > 1,
                _number(row.get("delivered_qty")),
            ),
        )
        if shipment_rows
        else {}
    )
    inspected_qty = _number(selected.get("delivered_qty"))
    unit = normalize_unit(selected.get("unit"))
    if selected.get("description"):
        description = str(selected["description"])
    if not description:
        return []

    result = "pending"
    checked = re.compile(r"(?:\[\s*[Xx]\s*\]|☒|☑|■)")
    decision_segments = {
        "accepted": re.search(
            r"A\.\s*Accepted(.+?)(?=B\.\s*Accepted|C\.\s*Rejected|$)",
            combined,
            re.IGNORECASE,
        ),
        "accepted_with_notes": re.search(
            r"B\.\s*Accepted(.+?)(?=C\.\s*Rejected|$)", combined, re.IGNORECASE
        ),
        "rejected": re.search(
            r"C\.\s*Rejected(.+?)(?=INSPECTED\s+BY|CONTRACTOR|$)",
            combined,
            re.IGNORECASE,
        ),
    }
    for candidate_result, segment in decision_segments.items():
        if segment and checked.search(segment.group(1)):
            result = candidate_result
            break

    line: dict[str, Any] = {
        "line_id": f"line_{uuid4().hex}",
        "source_page": int(selected.get("source_page") or 1),
        "material_code": str(selected.get("material_code") or ""),
        "description": description,
        "unit": unit,
        "inspected_qty": inspected_qty,
        "accepted_qty": inspected_qty if result.startswith("accepted") else 0.0,
        "rejected_qty": inspected_qty if result == "rejected" else 0.0,
        "inspection_result": result,
        "delivery_note_number": delivery_match.group(1) if delivery_match else "",
        "supplier_name": supplier_match.group(1).strip(" -:") if supplier_match else "",
        "confidence": 0.68 if inspected_qty > 0 else 0.58,
        "warnings": [
            "The inspection decision must be explicitly reviewed; blank MIR decision boxes remain pending."
        ],
    }
    return [line]


def _progress_invoice_lines(page_text: str, page_number: int) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    buffer: list[str] = []
    for raw_line in page_text.splitlines():
        compact = " ".join(raw_line.split())
        if not compact:
            continue
        if not buffer:
            if _INVOICE_ROW_START.match(compact):
                buffer = [compact]
            else:
                continue
        else:
            buffer.append(compact)
        joined = " ".join(buffer)
        if len(_INVOICE_PROGRESS_PAIR.findall(joined)) < 3:
            continue

        start = _INVOICE_ROW_START.match(joined)
        if not start:
            buffer = []
            continue
        body = start.group("body")
        first_percent = re.search(r"[\d,]+(?:\.\d+)?\s*%", body)
        pairs = list(_INVOICE_PROGRESS_PAIR.finditer(body))
        if not first_percent or len(pairs) < 3:
            buffer = []
            continue
        prefix = body[: first_percent.start()].strip()
        values = _INVOICE_PREFIX_QTY_UNIT.match(
            prefix
        ) or _INVOICE_PREFIX_UNIT_QTY.match(prefix)
        if not values:
            buffer = []
            continue
        progress = pairs[-3:]
        total_percent = _number(progress[-1].group("percent"))
        total_value = _number(progress[-1].group("amount"))
        if total_percent <= 0 and total_value <= 0:
            buffer = []
            continue
        quantity = _number(values.group("quantity"))
        description = values.group("description").strip(" -:")
        if len(normalize_description(description)) >= 4:
            output.append(
                {
                    "line_id": f"line_{uuid4().hex}",
                    "source_page": page_number,
                    "item_number": start.group("item").rstrip("."),
                    "description": description,
                    "unit": normalize_unit(values.group("unit")),
                    "contract_qty": quantity,
                    "certified_qty": _rounded(quantity * total_percent / 100),
                    "certified_unit_rate": _number(values.group("rate")),
                    "certified_percent": total_percent,
                    "certified_value": total_value,
                    "confidence": 0.7,
                    "warnings": [
                        "Certified values use the invoice total-to-date percentage and amount; confirm against the source row."
                    ],
                }
            )
        buffer = []
    return output


def _ocr_page_text(image: Any, pytesseract: Any, *, enhanced: bool) -> str:
    default_text = pytesseract.image_to_string(image) or ""
    if not enhanced:
        return default_text
    try:
        from PIL import ImageEnhance, ImageOps

        grayscale = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=1)
        sharpened = ImageEnhance.Sharpness(
            ImageEnhance.Contrast(grayscale).enhance(1.8)
        ).enhance(1.6)
        width, height = sharpened.size
        central_table = sharpened.crop(
            (0, int(height * 0.18), width, int(height * 0.76))
        )
        variants = [
            pytesseract.image_to_string(sharpened, config="--psm 6") or "",
            pytesseract.image_to_string(central_table, config="--psm 6") or "",
            pytesseract.image_to_string(central_table, config="--psm 11") or "",
        ]
        distinct = [default_text]
        for value in variants:
            normalized = " ".join(value.split())
            if normalized and all(
                normalized != " ".join(item.split()) for item in distinct
            ):
                distinct.append(value)
        return "\n".join(distinct)
    except Exception:
        return default_text


def _extract_pdf_pages(
    raw_bytes: bytes,
    *,
    document_type_hint: str = "auto",
    filename_hint: str = "",
) -> tuple[list[str], str, list[dict[str, Any]]]:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            raise HTTPException(
                503, "PDF extraction requires the pypdf dependency"
            ) from error

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

    # Weekly reports commonly contain image-only schedule and site-photo pages
    # alongside native-text material tables. OCRing every sparse page blocks the
    # upload request for minutes even when all material rows are already
    # available. Prefer the native material tables and keep OCR as a fallback
    # only when no reliable weekly material row can be parsed.
    native_text = "\n\f\n".join(pages)
    hinted_type = str(document_type_hint or "auto").strip().lower()
    detected_native_type = classify_material_document(native_text, filename_hint)
    is_weekly_report = (
        hinted_type == "weekly_report" or detected_native_type == "weekly_report"
    )
    if is_weekly_report and any(
        _weekly_report_lines(page_text, page_number)
        for page_number, page_text in enumerate(pages, start=1)
    ):
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
            ocr_text = _ocr_page_text(
                image,
                pytesseract,
                enhanced=len(sparse_page_indexes) <= 20,
            )
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
    page_values = list(pages)

    if document_type == "weekly_report":
        for page_number, page_text in enumerate(page_values, start=1):
            lines.extend(_weekly_report_lines(page_text, page_number))
        return _extraction_footer(lines, warnings)

    if document_type == "mir_grn":
        lines.extend(_mir_lines(page_values))
        return _extraction_footer(lines, warnings)

    if document_type == "progress_invoice":
        for page_number, page_text in enumerate(page_values, start=1):
            lines.extend(_progress_invoice_lines(page_text, page_number))
        return _extraction_footer(lines, warnings)

    for page_number, page_text in enumerate(page_values, start=1):
        if document_type in {
            "delivery_note",
            "customer_shipment",
            "purchase_order",
        }:
            page_lines, reference_ignored = _delivery_lines_for_page(
                page_text, page_number, document_type=document_type
            )
            for line in page_lines:
                key = (
                    page_number,
                    line.get("material_code"),
                    normalize_description(line.get("description")),
                    line.get("unit"),
                    line.get("ordered_qty", line.get("delivered_qty")),
                )
                if key not in seen:
                    seen.add(key)
                    lines.append(line)
            if reference_ignored and not any(
                item.get("code") == "reference_identifier_ignored" for item in warnings
            ):
                warnings.append(
                    {
                        "code": "reference_identifier_ignored",
                        "message": "Document, order, pack, and delivery identifiers were excluded from material quantities.",
                    }
                )
            continue
        if document_type == "boq":
            page_lines = page_text.splitlines()
            starts = [
                index
                for index, value in enumerate(page_lines)
                if _BOQ_BLOCK_START.match(" ".join(value.split()))
            ]
            for position, start in enumerate(starts):
                end = (
                    starts[position + 1]
                    if position + 1 < len(starts)
                    else len(page_lines)
                )
                block = " ".join(
                    " ".join(value.split()) for value in page_lines[start:end]
                )
                row = _BOQ_BLOCK_START.match(block)
                if not row:
                    continue
                values = _BOQ_BLOCK_VALUE.search(row.group("description"))
                if not values:
                    continue
                third_after_unit = _number(values.group("third_after_unit"))
                if third_after_unit > 0:
                    # Some PDFs place the unit before the financial columns,
                    # especially when a drawing reference ends with a number:
                    # ``LS 20 Nos 1 52,087 52,087``. In that layout the three
                    # values after the unit are quantity, rate, and amount.
                    quantity = _number(values.group("first_after_unit"))
                    rate = _number(values.group("second_after_unit"))
                    amount = third_after_unit
                    quantity_warning = (
                        "Quantity, rate, and amount were read from the three columns "
                        "after the unit; confirm against the source page."
                    )
                else:
                    rate = _number(values.group("first_after_unit"))
                    amount = _number(values.group("second_after_unit"))
                    quantity = amount / rate if rate > 0 else 0.0
                    quantity_warning = (
                        "Quantity was cross-checked from line amount / contract rate; "
                        "confirm against the source page."
                    )
                if rate <= 0 or amount <= 0:
                    continue
                # With only rate and amount after the unit, PDF extraction may
                # have joined a drawing reference such as ``D-22`` to the raw
                # quantity. In that layout amount / rate is the safer value.
                description = row.group("description")[: values.start()].strip(" -:")
                key = (
                    page_number,
                    row.group("item"),
                    normalize_description(description),
                )
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
                        "warnings": [quantity_warning],
                    }
                )
        for raw_line in page_text.splitlines():
            compact = " ".join(raw_line.split())
            if len(compact) < 5:
                continue
            match = _BOQ_LINE.match(compact) if document_type == "boq" else None
            if match:
                values = match.groupdict()
                key = (
                    page_number,
                    values["item"],
                    normalize_description(values["description"]),
                )
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
                    "warnings": [
                        "Confirm the extracted table row against the source page."
                    ],
                }
                line.update(
                    planned_qty=quantity,
                    contract_unit_rate=rate,
                    line_amount=_number(values["amount"]),
                )
                lines.append(line)
                continue

    return _extraction_footer(lines, warnings)


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


def _public_document(
    document: dict[str, Any], *, include_lines: bool = True
) -> dict[str, Any]:
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
    force_reprocess: bool = False,
) -> dict[str, Any]:
    requested_type = str(document_type or "auto").strip().lower()
    if requested_type not in DOCUMENT_TYPES:
        raise HTTPException(
            400, f"Unsupported material document type: {requested_type}"
        )
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
    project_currency = project_currency_code(project)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    existing = material_documents_collection.find_one(
        {"project_id": project_id, "source_sha256": digest}
    )
    reprocess_existing = bool(
        existing
        and (
            force_reprocess
            or (
                existing.get("status") == "needs_review"
                and (
                    int(existing.get("processing_version") or 0)
                    < MATERIAL_PROCESSING_VERSION
                    or (
                        not existing.get("extracted_lines")
                        and not existing.get("reviewed_lines")
                    )
                )
            )
        )
    )
    if existing and not reprocess_existing:
        return {"status": "already_uploaded", "document": _public_document(existing)}

    pages, extraction_method, extraction_warnings = _extract_pdf_pages(
        raw_bytes,
        document_type_hint=requested_type,
        filename_hint=safe_filename,
    )
    text = "\n\f\n".join(pages)
    detected_type = classify_material_document(text, safe_filename)
    resolved_type, classification_auto_corrected = resolve_material_document_type(
        requested_type, detected_type
    )
    lines, line_warnings = extract_structured_lines(pages, document_type=resolved_type)
    page_contexts: list[dict[str, Any]] = []
    if resolved_type == "delivery_note":
        page_contexts = _delivery_page_contexts(pages, filename=safe_filename)
        lines = _apply_delivery_page_contexts(lines, page_contexts)
        page_document_numbers = {
            str(context.get("delivery_note_number") or "")
            for context in page_contexts
            if context.get("delivery_note_number")
        }
        if len(page_document_numbers) > 1:
            line_warnings.append(
                {
                    "code": "bundled_delivery_notes_extracted_by_page",
                    "message": (
                        f"{len(page_document_numbers)} delivery notes were found in this PDF bundle. "
                        "Each page was extracted and retained as separate source evidence."
                    ),
                }
            )
    if resolved_type in {"delivery_note", "customer_shipment", "mir_grn"} and lines:
        baseline_materials = list(
            project_materials_collection.find({"project_id": project_id})
        )
        if resolved_type == "mir_grn":
            lines = enrich_mir_lines_with_baseline(lines, baseline_materials)
        else:
            lines = enrich_delivery_lines_with_baseline(lines, baseline_materials)
        if any(
            line.get("match_status") == "suggested"
            or line.get("conversion_status") == "suggested"
            for line in lines
        ):
            line_warnings.append(
                {
                    "code": "auto_match_suggestions_require_review",
                    "message": "AI-assisted BOQ matches and unit conversions were suggested. Verify them before confirmation.",
                }
            )
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
        warning_code = (
            "classification_auto_corrected"
            if classification_auto_corrected
            else "classification_mismatch"
        )
        warning_message = (
            f"Selected as {requested_type}, but content was safely reclassified as {detected_type}."
            if classification_auto_corrected
            else f"Selected as {requested_type}, but content resembles {detected_type}."
        )
        warnings.insert(
            0,
            {
                "code": warning_code,
                "message": warning_message,
            },
        )
    extracted_header = _extract_header(text, resolved_type)
    if project_currency:
        extracted_header["currency"] = project_currency
    if page_contexts:
        extracted_header["page_documents"] = page_contexts
        extracted_header["bundled_document_count"] = len(
            {
                context.get("delivery_note_number")
                for context in page_contexts
                if context.get("delivery_note_number")
            }
        )
    document = {
        "document_id": document_id,
        "project_id": project_id,
        "site_name": project["site_name"],
        "floorplan_id": project["floorplan_id"],
        "document_type": resolved_type,
        "requested_document_type": requested_type,
        "detected_document_type": detected_type,
        "classification_auto_corrected": classification_auto_corrected,
        "original_filename": safe_filename,
        "stored_filename": stored_filename,
        "storage_path": stored_path,
        "source_sha256": digest,
        "source_size_bytes": len(raw_bytes),
        "page_count": len(pages),
        "extraction_method": extraction_method,
        "processing_version": MATERIAL_PROCESSING_VERSION,
        "processing_status": (
            "processed" if extraction_method != "ocr_required" else "review_required"
        ),
        "status": "needs_review",
        "extracted_header": extracted_header,
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
                {"project_id": project_id, "document_id": document_id},
                {
                    "$set": document,
                    "$unset": {
                        "reviewed_header": "",
                        "reviewed_lines": "",
                        "review_note": "",
                        "reviewed_by_user_id": "",
                        "reviewed_by_email": "",
                        "reviewed_at": "",
                        "confirmed_header": "",
                        "confirmed_lines": "",
                        "confirmed_by_user_id": "",
                        "confirmed_by_email": "",
                        "confirmed_at": "",
                        "void_reason": "",
                        "voided_by_user_id": "",
                        "voided_by_email": "",
                        "voided_at": "",
                        "superseded_by_document_id": "",
                        "superseded_at": "",
                    },
                },
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
    cursor = material_documents_collection.find(
        {"project_id": project["project_id"]}
    ).sort("uploaded_at", -1)
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
        raise HTTPException(
            409, "Confirmed, superseded, or voided documents cannot be edited"
        )
    now = utc_now()
    normalized_lines = _normalize_review_lines(lines)
    for line in normalized_lines:
        suggested_material_id = str(line.get("suggested_material_id") or "")
        linked_material_id = str(line.get("linked_material_id") or "")
        if suggested_material_id:
            line["match_status"] = (
                "reviewed"
                if linked_material_id == suggested_material_id
                else "manual_override"
            )
            line["match_reviewed_by_user_id"] = user.user_id
            line["match_reviewed_at"] = now
        source_unit = normalize_unit(line.get("source_unit"))
        target_unit = normalize_unit(line.get("unit"))
        if (
            source_unit
            and source_unit != target_unit
            and _number(line.get("conversion_factor")) > 0
        ):
            line["conversion_status"] = "reviewed"
            line["conversion_reviewed_by_user_id"] = user.user_id
            line["conversion_reviewed_at"] = now
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
            "source_delivered_qty",
            "source_inspected_qty",
            "piece_length_m",
            "conversion_factor",
            "converted_qty",
            "match_confidence",
            "conversion_confidence",
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
        raise HTTPException(
            422, "Add at least one reviewed material line before confirmation"
        )
    for index, line in enumerate(lines, start=1):
        description = str(line.get("description") or "").strip()
        unit = normalize_unit(line.get("unit"))
        if document_type == "boq":
            if not description or not unit or _number(line.get("planned_qty")) <= 0:
                raise HTTPException(
                    422,
                    f"BOQ line {index} requires description, unit, and planned quantity",
                )
            line["material_id"] = str(
                line.get("material_id") or f"material_{uuid4().hex}"
            )
        elif document_type in TRANSACTION_TYPES:
            if not line.get("linked_material_id"):
                raise HTTPException(
                    422,
                    f"Line {index} must be linked to a confirmed BOQ baseline material",
                )
            source_unit = normalize_unit(line.get("source_unit"))
            if source_unit and source_unit != unit:
                if line.get("conversion_status") != "reviewed":
                    raise HTTPException(
                        422,
                        f"Line {index} unit conversion must be reviewed before confirmation",
                    )
                if (
                    _number(line.get("source_delivered_qty")) <= 0
                    or _number(line.get("conversion_factor")) <= 0
                    or _number(line.get("converted_qty")) <= 0
                ):
                    raise HTTPException(
                        422,
                        f"Line {index} has incomplete unit conversion evidence",
                    )
            if (
                document_type in {"delivery_note", "customer_shipment"}
                and _number(line.get("delivered_qty")) <= 0
            ):
                raise HTTPException(
                    422, f"Delivery line {index} requires a delivered quantity"
                )
            if (
                document_type == "purchase_order"
                and _number(line.get("ordered_qty")) <= 0
            ):
                raise HTTPException(
                    422, f"PO line {index} requires an ordered quantity"
                )
            if document_type == "mir_grn":
                result = str(line.get("inspection_result") or "pending").strip().lower()
                if result in {"", "pending", "submitted"}:
                    raise HTTPException(
                        422, f"MIR/GRN line {index} has no final inspection result"
                    )
                inspected = _number(line.get("inspected_qty"))
                decided = _number(line.get("accepted_qty")) + _number(
                    line.get("rejected_qty")
                )
                if inspected > 0 and decided > inspected + 0.0001:
                    raise HTTPException(
                        422,
                        f"MIR/GRN line {index} acceptance exceeds inspected quantity",
                    )
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

    reused_material_ids = 0
    replaced_boq_document_id = ""
    document_for_validation = document
    if document.get("document_type") == "boq":
        active_boq = material_documents_collection.find_one(
            {
                "project_id": project_id,
                "document_type": "boq",
                "status": "confirmed",
                "document_id": {"$ne": document_id},
            }
        )
        if active_boq:
            candidate_lines = (
                document.get("reviewed_lines")
                if document.get("reviewed_lines") is not None
                else document.get("extracted_lines") or []
            )
            stable_lines, reused_material_ids = preserve_matching_boq_material_ids(
                candidate_lines,
                active_boq.get("confirmed_lines") or [],
            )
            document_for_validation = {**document, "reviewed_lines": stable_lines}
            replaced_boq_document_id = str(active_boq.get("document_id") or "")

    lines = _validated_confirmed_lines(document_for_validation)
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
    header = dict(
        document.get("reviewed_header") or document.get("extracted_header") or {}
    )
    project_currency = project_currency_code(project)
    if project_currency:
        header["currency"] = project_currency
    now = utc_now()
    if document.get("document_type") == "boq":
        material_documents_collection.update_many(
            {
                "project_id": project_id,
                "document_type": "boq",
                "status": "confirmed",
                "document_id": {"$ne": document_id},
            },
            {
                "$set": {
                    "status": "superseded",
                    "superseded_by_document_id": document_id,
                    "superseded_at": now,
                    "updated_at": now,
                }
            },
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
        details={
            "line_count": len(lines),
            "replaced_boq_document_id": replaced_boq_document_id,
            "reused_material_id_count": reused_material_ids,
        },
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
    if document.get("document_type") == "boq" and document.get("status") == "confirmed":
        confirmed_transaction = material_documents_collection.find_one(
            {
                "project_id": project_id,
                "document_type": {"$ne": "boq"},
                "status": "confirmed",
            }
        )
        if confirmed_transaction:
            raise HTTPException(
                409,
                "The active BOQ cannot be voided while confirmed material transactions exist. Replace the BOQ or reset transactions first.",
            )
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


def _remove_material_source_file(
    document: dict[str, Any], *, strict: bool = True
) -> bool:
    storage_path = str(document.get("storage_path") or "").strip()
    if not storage_path:
        return False
    try:
        os.remove(storage_path)
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        if not strict:
            return False
        raise HTTPException(
            500, "The stored material document could not be removed"
        ) from error


def discard_material_document(
    *, project_ref: str, document_id: str, reason: str, user: AuthenticatedUser
) -> dict[str, Any]:
    """Permanently discard an unconfirmed upload so the same PDF can be retested."""
    project = resolve_project(project_ref)
    project_id = project["project_id"]
    document = material_documents_collection.find_one(
        {"project_id": project_id, "document_id": document_id}
    )
    if not document:
        return {
            "status": "already_discarded",
            "document_id": document_id,
            "source_removed": False,
        }
    if document.get("status") != "needs_review":
        raise HTTPException(
            409, "Only documents waiting for review can be permanently discarded"
        )
    delete_result = material_documents_collection.delete_one(
        {
            "project_id": project_id,
            "document_id": document_id,
            "status": "needs_review",
        }
    )
    if int(getattr(delete_result, "deleted_count", 0)) != 1:
        current = material_documents_collection.find_one(
            {"project_id": project_id, "document_id": document_id}
        )
        if current:
            raise HTTPException(
                409,
                "The document changed while it was being discarded; refresh and try again",
            )
        return {
            "status": "already_discarded",
            "document_id": document_id,
            "source_removed": False,
        }

    # The database record is authoritative. A missing or temporarily locked
    # source file must not leave a deleted upload visible in Manage Uploads.
    source_removed = _remove_material_source_file(document, strict=False)
    audit_recorded = True
    try:
        _audit(
            project_id=project_id,
            document_id=document_id,
            event_type="discarded",
            user=user,
            details={
                "reason": str(reason or "").strip(),
                "filename": document.get("original_filename"),
                "source_removed": source_removed,
            },
        )
    except Exception:
        # Do not report a failed delete after the requested record is gone.
        audit_recorded = False

    response: dict[str, Any] = {
        "status": "discarded",
        "document_id": document_id,
        "source_removed": source_removed,
        "audit_recorded": audit_recorded,
    }
    try:
        response["summary"] = get_material_summary(project_id)
    except Exception:
        response["summary_refresh_required"] = True
    return response


def reprocess_material_document(
    *, project_ref: str, document_id: str, user: AuthenticatedUser
) -> dict[str, Any]:
    project = resolve_project(project_ref)
    project_id = project["project_id"]
    document = material_documents_collection.find_one(
        {"project_id": project_id, "document_id": document_id}
    )
    if not document:
        raise HTTPException(404, "Material document not found")
    if document.get("status") in {"confirmed", "superseded"}:
        raise HTTPException(409, "Void the document before reprocessing it")
    storage_path = str(document.get("storage_path") or "").strip()
    if not storage_path or not os.path.isfile(storage_path):
        raise HTTPException(404, "The stored source PDF is unavailable")
    with open(storage_path, "rb") as source:
        raw_bytes = source.read()
    return upload_material_document(
        project_ref=project_id,
        filename=str(document.get("original_filename") or "material-document.pdf"),
        raw_bytes=raw_bytes,
        document_type=str(
            document.get("requested_document_type")
            or document.get("document_type")
            or "auto"
        ),
        user=user,
        force_reprocess=True,
    )


def restore_material_document(
    *, project_ref: str, document_id: str, reason: str, user: AuthenticatedUser
) -> dict[str, Any]:
    project = resolve_project(project_ref)
    project_id = project["project_id"]
    document = material_documents_collection.find_one(
        {"project_id": project_id, "document_id": document_id}
    )
    if not document:
        raise HTTPException(404, "Material document not found")
    if document.get("status") not in {"voided", "superseded"}:
        raise HTTPException(409, "Only voided or superseded documents can be restored")
    confirmed_lines = document.get("confirmed_lines") or []
    if not confirmed_lines:
        raise HTTPException(
            422, "This document has no confirmed data to restore; reprocess it instead"
        )

    validation_document = {**document, "reviewed_lines": confirmed_lines}
    lines = _validated_confirmed_lines(validation_document)
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

    now = utc_now()
    if document.get("document_type") == "boq":
        material_documents_collection.update_many(
            {
                "project_id": project_id,
                "document_type": "boq",
                "status": "confirmed",
                "document_id": {"$ne": document_id},
            },
            {
                "$set": {
                    "status": "superseded",
                    "superseded_by_document_id": document_id,
                    "superseded_at": now,
                    "updated_at": now,
                }
            },
        )
    material_documents_collection.update_one(
        {"project_id": project_id, "document_id": document_id},
        {
            "$set": {
                "status": "confirmed",
                "confirmed_lines": lines,
                "restored_at": now,
                "restored_by_user_id": user.user_id,
                "restored_by_email": user.email,
                "restore_reason": str(reason or "").strip(),
                "updated_at": now,
            },
            "$unset": {
                "void_reason": "",
                "voided_by_user_id": "",
                "voided_by_email": "",
                "voided_at": "",
                "superseded_by_document_id": "",
                "superseded_at": "",
            },
        },
    )
    _audit(
        project_id=project_id,
        document_id=document_id,
        event_type="restored",
        user=user,
        details={"reason": reason, "line_count": len(lines)},
    )
    rebuild_material_ledger(project_id)
    return {
        "status": "restored",
        "document": get_material_document(project_id, document_id),
        "summary": get_material_summary(project_id),
    }


def reset_material_documents(
    *,
    project_ref: str,
    scope: str,
    reason: str,
    confirmation: str,
    user: AuthenticatedUser,
) -> dict[str, Any]:
    project = resolve_project(project_ref)
    project_id = project["project_id"]
    normalized_scope = str(scope or "").strip().lower()
    if normalized_scope not in MATERIAL_RESET_SCOPES:
        raise HTTPException(
            400, f"Unsupported material reset scope: {normalized_scope}"
        )
    if str(confirmation or "").strip() != "RESET MATERIALS":
        raise HTTPException(
            422, "Type RESET MATERIALS to confirm this destructive action"
        )

    documents = list(material_documents_collection.find({"project_id": project_id}))
    selected = [
        document
        for document in documents
        if document_matches_material_reset_scope(document, normalized_scope)
    ]
    removed_files = 0
    for document in selected:
        if _remove_material_source_file(document):
            removed_files += 1
    document_ids = [str(document.get("document_id") or "") for document in selected]
    document_ids = [document_id for document_id in document_ids if document_id]
    if document_ids:
        material_documents_collection.delete_many(
            {"project_id": project_id, "document_id": {"$in": document_ids}}
        )
    rebuild_material_ledger(project_id)
    _audit(
        project_id=project_id,
        document_id="project_materials",
        event_type="reset",
        user=user,
        details={
            "scope": normalized_scope,
            "reason": str(reason or "").strip(),
            "removed_document_count": len(document_ids),
            "removed_file_count": removed_files,
        },
    )
    return {
        "status": "reset",
        "scope": normalized_scope,
        "removed_document_count": len(document_ids),
        "removed_file_count": removed_files,
        "summary": get_material_summary(project_id),
    }


def build_material_ledger(
    documents: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
                "normalized_description": normalize_description(
                    line.get("description")
                ),
                "unit": normalize_unit(line.get("unit")),
                "currency": str(
                    line.get("currency")
                    or document.get("confirmed_header", {}).get("currency")
                    or ""
                ),
                "planned_qty": planned,
                "contract_unit_rate": contract_rate,
                "planned_contract_value": _number(line.get("line_amount"))
                or planned * contract_rate,
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
                item["approval_status"] = str(
                    line.get("approval_status") or item["approval_status"]
                )
                item["expected_delivery_date"] = str(
                    line.get("expected_delivery_date") or item["expected_delivery_date"]
                )
                item["actual_delivery_date"] = str(
                    line.get("actual_delivery_date") or item["actual_delivery_date"]
                )
            elif document_type == "purchase_order":
                item["ordered_qty"] += _number(line.get("ordered_qty"))
                rate = _number(line.get("purchase_unit_rate"))
                if rate:
                    item["purchase_unit_rate"] = rate
            elif document_type in {"delivery_note", "customer_shipment"}:
                item["delivered_qty"] += _number(line.get("delivered_qty")) - _number(
                    line.get("return_qty")
                )
                actual_date = str(
                    line.get("actual_delivery_date")
                    or document.get("confirmed_header", {}).get("document_date")
                    or ""
                )
                if actual_date:
                    item["actual_delivery_date"] = actual_date
            elif document_type == "mir_grn":
                item["inspected_qty"] += _number(line.get("inspected_qty"))
                item["accepted_qty"] += _number(line.get("accepted_qty"))
                item["rejected_qty"] += _number(line.get("rejected_qty"))
            elif document_type == "progress_invoice":
                item["certified_percent"] = max(
                    item["certified_percent"], _number(line.get("certified_percent"))
                )
                item["certified_value"] = max(
                    item["certified_value"], _number(line.get("certified_value"))
                )

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
        item["value_basis"] = (
            "po_purchase_rate" if item["purchase_unit_rate"] else "boq_contract_rate"
        )
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
    cursor = project_materials_collection.find(
        {"project_id": project["project_id"]}
    ).sort([("status", 1), ("description", 1)])
    return [
        {key: value for key, value in item.items() if key != "_id"} for item in cursor
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
            source_field = (
                "certified_value" if field == "certified_contract_value" else field
            )
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
        "currency": project_currency_code(project)
        or next(
            (str(item.get("currency")) for item in ledger if item.get("currency")),
            "",
        ),
        "totals": {key: _rounded(value) for key, value in totals.items()},
        "quantity_totals": {
            unit: {key: _rounded(value) for key, value in values.items()}
            for unit, values in quantity_totals.items()
        },
        "document_counts": dict(document_counts),
        "status_counts": dict(status_counts),
        "material_count": len(ledger),
        "needs_review_count": sum(
            1 for item in active_documents if item.get("status") == "needs_review"
        ),
        "confirmed_document_count": sum(
            1 for item in active_documents if item.get("status") == "confirmed"
        ),
        "overdue_count": sum(1 for item in ledger if item.get("is_overdue") is True),
        "unit_warning_count": sum(len(item.get("warnings") or []) for item in ledger),
    }
