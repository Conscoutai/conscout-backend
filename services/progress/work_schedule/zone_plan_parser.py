from __future__ import annotations

import itertools
import math
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import pypdfium2 as pdfium
except ImportError:  # pragma: no cover - deployment dependency guard
    pdfium = None


class ZonePlanParseError(ValueError):
    pass


_CHAINAGE_PATTERN = re.compile(r"(?<!\d)(\d{1,3})\s*\+\s*(\d{2,3})(?!\d)")
_EXPLICIT_ZONE_PATTERN = re.compile(
    r"\b(?:ZONE|AREA|SECTION)\s*[-:#]?\s*([A-Z0-9][A-Z0-9_-]{0,30})\b",
    re.IGNORECASE,
)
_STANDALONE_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9+])(\d{1,2})(?![A-Za-z0-9+])")
_MAX_PDF_BYTES = 30 * 1024 * 1024
_MIN_ZONE_COUNT = 2


@dataclass(frozen=True)
class _LocatedText:
    text: str
    center: tuple[float, float]
    box: tuple[float, float, float, float]
    size: float
    value: Optional[int] = None
    source: str = "vector"
    order: int = 0
    line_id: str = ""


def _center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    left, bottom, right, top = box
    return ((left + right) / 2.0, (bottom + top) / 2.0)


def _union_boxes(
    boxes: Iterable[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    materialized = list(boxes)
    return (
        min(box[0] for box in materialized),
        min(box[1] for box in materialized),
        max(box[2] for box in materialized),
        max(box[3] for box in materialized),
    )


def _char_box(
    text_page: Any, index: int
) -> Optional[tuple[float, float, float, float]]:
    try:
        raw = text_page.get_charbox(index)
        box = tuple(float(value) for value in raw)
    except Exception:
        return None
    if len(box) != 4 or not all(math.isfinite(value) for value in box):
        return None
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box  # type: ignore[return-value]


def _normalized_zone_code(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.strip()).strip("-:# ")
    if len(cleaned) == 1 or cleaned.isdigit() or cleaned.isupper():
        return cleaned.upper()
    return cleaned.title()


def _located_span(
    text_page: Any,
    text: str,
    start: int,
    end: int,
    *,
    label: str,
    source: str,
) -> Optional[_LocatedText]:
    boxes = [
        box
        for index in range(start, end)
        if (box := _char_box(text_page, index)) is not None
    ]
    if not boxes:
        return None
    box = _union_boxes(boxes)
    return _LocatedText(
        text=label,
        center=_center(box),
        box=box,
        size=max(box[2] - box[0], box[3] - box[1]),
        source=source,
        order=start,
    )


def _label_candidates(
    text_page: Any, text: str, page_diagonal: float
) -> tuple[dict[str, list[_LocatedText]], list[str]]:
    minimum_size = max(8.0, page_diagonal * 0.004)
    candidates: dict[str, list[_LocatedText]] = {}
    explicit_codes: list[str] = []

    for match in _EXPLICIT_ZONE_PATTERN.finditer(text):
        code = _normalized_zone_code(match.group(1))
        located = _located_span(
            text_page,
            text,
            match.start(1),
            match.end(1),
            label=code,
            source="vector_explicit",
        )
        if located is None or located.size < minimum_size * 0.65:
            continue
        candidates.setdefault(code, []).append(located)
        if code not in explicit_codes:
            explicit_codes.append(code)

    # CAD text extraction can concatenate a zone letter with a nearby numeric
    # chainage (for example ``0+500B``), so letters are isolated from other
    # letters rather than from every alphanumeric character.
    for index, raw_character in enumerate(text):
        character = raw_character.upper()
        if character not in string.ascii_uppercase:
            continue
        previous = text[index - 1] if index > 0 else " "
        following = text[index + 1] if index + 1 < len(text) else " "
        if previous.isalpha() or following.isalpha():
            continue
        located = _located_span(
            text_page,
            text,
            index,
            index + 1,
            label=character,
            source="vector_standalone",
        )
        if located is not None and located.size >= minimum_size:
            candidates.setdefault(character, []).append(located)

    for match in _STANDALONE_NUMBER_PATTERN.finditer(text):
        code = str(int(match.group(1)))
        located = _located_span(
            text_page,
            text,
            match.start(1),
            match.end(1),
            label=code,
            source="vector_standalone",
        )
        if located is not None and located.size >= minimum_size:
            candidates.setdefault(code, []).append(located)
    return candidates, explicit_codes


def _ordered_sequence_score(
    combination: tuple[_LocatedText, ...], page_diagonal: float
) -> Optional[float]:
    first = combination[0].center
    last = combination[-1].center
    dx = last[0] - first[0]
    dy = last[1] - first[1]
    length = math.hypot(dx, dy)
    if length < page_diagonal * 0.12:
        return None
    axis = (dx / length, dy / length)
    projections = [
        (item.center[0] - first[0]) * axis[0] + (item.center[1] - first[1]) * axis[1]
        for item in combination
    ]
    gaps = [right - left for left, right in zip(projections, projections[1:])]
    if any(gap <= page_diagonal * 0.01 for gap in gaps):
        return None
    residuals = [
        abs(
            (item.center[0] - first[0]) * -axis[1]
            + (item.center[1] - first[1]) * axis[0]
        )
        for item in combination
    ]
    average_gap = sum(gaps) / len(gaps)
    gap_spread = max(abs(gap - average_gap) for gap in gaps) / max(average_gap, 1e-9)
    residual = sum(residuals) / len(residuals) / max(page_diagonal, 1e-9)
    if residual > 0.05 or gap_spread > 0.75:
        return None
    average_size = sum(item.size for item in combination) / len(combination)
    return (
        residual
        + gap_spread * 0.15
        - average_size / page_diagonal * 0.02
        - len(combination) * 0.08
    )


def _consecutive_code_sequences(codes: set[str]) -> list[list[str]]:
    sequences: list[list[str]] = []
    for universe in (
        list(string.ascii_uppercase),
        [str(value) for value in range(1, 100)],
    ):
        present = [value in codes for value in universe]
        index = 0
        while index < len(universe):
            if not present[index]:
                index += 1
                continue
            end = index
            while end + 1 < len(universe) and present[end + 1]:
                end += 1
            run = universe[index : end + 1]
            for length in range(len(run), _MIN_ZONE_COUNT - 1, -1):
                for start in range(0, len(run) - length + 1):
                    sequences.append(run[start : start + length])
            index = end + 1
    return sequences


def _candidate_combinations(
    codes: list[str], candidates: dict[str, list[_LocatedText]]
) -> Iterable[tuple[_LocatedText, ...]]:
    limit = 6 if len(codes) <= 6 else (3 if len(codes) <= 8 else 1)
    groups = [
        sorted(candidates[code], key=lambda item: item.size, reverse=True)[:limit]
        for code in codes
    ]
    return itertools.product(*groups)


def _select_zone_labels(
    candidates: dict[str, list[_LocatedText]],
    explicit_codes: list[str],
    page_diagonal: float,
) -> list[_LocatedText]:
    if len(candidates) < _MIN_ZONE_COUNT:
        raise ZonePlanParseError(
            "Could not detect at least two ordered zone labels in the PDF. "
            "Use labels such as A, B, C or Zone 1, Zone 2, Zone 3."
        )

    sequences = _consecutive_code_sequences(set(candidates))
    if len(explicit_codes) >= _MIN_ZONE_COUNT:
        sequences.insert(0, explicit_codes)

    seen_sequences: set[tuple[str, ...]] = set()
    best: Optional[tuple[float, list[_LocatedText]]] = None
    for codes in sequences:
        sequence_key = tuple(codes)
        if sequence_key in seen_sequences or any(
            code not in candidates for code in codes
        ):
            continue
        seen_sequences.add(sequence_key)
        for combination in _candidate_combinations(codes, candidates):
            score = _ordered_sequence_score(combination, page_diagonal)
            if score is not None and (best is None or score < best[0]):
                best = (score, list(combination))

    if best is None:
        raise ZonePlanParseError(
            "Zone labels were found, but they do not form a reliable ordered sequence. "
            "Mark each zone clearly along the project alignment."
        )
    return best[1]


def _chainage_candidates(text_page: Any, text: str) -> list[_LocatedText]:
    located: list[_LocatedText] = []
    for match in _CHAINAGE_PATTERN.finditer(text):
        boxes = [
            box
            for index in range(match.start(), match.end())
            if (box := _char_box(text_page, index)) is not None
        ]
        if len(boxes) < 3:
            continue
        box = _union_boxes(boxes)
        suffix = int(match.group(2))
        value = int(match.group(1)) * 1000 + suffix
        located.append(
            _LocatedText(
                text=match.group(0).replace(" ", ""),
                center=_center(box),
                box=box,
                size=max(box[2] - box[0], box[3] - box[1]),
                value=value,
            )
        )
    return located


def _ocr_page_tokens(
    page: Any, page_width: float, page_height: float
) -> list[_LocatedText]:
    try:
        import pytesseract
    except ImportError as error:  # pragma: no cover - deployment dependency guard
        raise ZonePlanParseError(
            "This PDF needs OCR, but the backend OCR package is unavailable"
        ) from error

    bitmap = None
    try:
        render_scale = min(3.0, 4200.0 / max(page_width, page_height))
        bitmap = page.render(scale=max(render_scale, 0.05))
        image = bitmap.to_pil().convert("RGB")
        data = pytesseract.image_to_data(
            image,
            output_type=pytesseract.Output.DICT,
            config="--psm 11",
        )
    except pytesseract.TesseractNotFoundError as error:
        raise ZonePlanParseError(
            "This PDF needs OCR, but Tesseract is not installed on the backend"
        ) from error
    except Exception as error:
        raise ZonePlanParseError(f"Unable to OCR the zone plan PDF: {error}") from error
    finally:
        if bitmap is not None:
            bitmap.close()

    image_width, image_height = image.size
    tokens: list[_LocatedText] = []
    count = len(data.get("text", []))
    for index in range(count):
        word = str(data["text"][index] or "").strip()
        if not word:
            continue
        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = -1
        if confidence < 30:
            continue
        left = float(data["left"][index])
        top = float(data["top"][index])
        width = float(data["width"][index])
        height = float(data["height"][index])
        if width <= 0 or height <= 0:
            continue
        box = (
            left / image_width * page_width,
            page_height - (top + height) / image_height * page_height,
            (left + width) / image_width * page_width,
            page_height - top / image_height * page_height,
        )
        line_id = ":".join(
            str(data.get(field, [0] * count)[index])
            for field in ("page_num", "block_num", "par_num", "line_num")
        )
        tokens.append(
            _LocatedText(
                text=word,
                center=_center(box),
                box=box,
                size=max(box[2] - box[0], box[3] - box[1]),
                source="ocr",
                order=index,
                line_id=line_id,
            )
        )
    return tokens


def _ocr_label_candidates(
    tokens: list[_LocatedText], page_diagonal: float
) -> tuple[dict[str, list[_LocatedText]], list[str]]:
    minimum_size = max(8.0, page_diagonal * 0.004)
    candidates: dict[str, list[_LocatedText]] = {}
    explicit_codes: list[str] = []
    prefixes = {"ZONE", "AREA", "SECTION"}

    for index, token in enumerate(tokens):
        normalized_word = token.text.strip().strip("-:#")
        if normalized_word.upper() in prefixes:
            for following in tokens[index + 1 : index + 4]:
                if following.line_id != token.line_id:
                    continue
                raw_code = following.text.strip().strip("-:#")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,30}", raw_code):
                    continue
                code = _normalized_zone_code(raw_code)
                located = _LocatedText(
                    text=code,
                    center=following.center,
                    box=following.box,
                    size=following.size,
                    source="ocr_explicit",
                    order=following.order,
                    line_id=following.line_id,
                )
                if located.size >= minimum_size * 0.65:
                    candidates.setdefault(code, []).append(located)
                    if code not in explicit_codes:
                        explicit_codes.append(code)
                break

        if re.fullmatch(r"[A-Za-z]", normalized_word):
            code = normalized_word.upper()
        elif re.fullmatch(r"\d{1,2}", normalized_word):
            code = str(int(normalized_word))
        else:
            continue
        if token.size < minimum_size:
            continue
        candidates.setdefault(code, []).append(
            _LocatedText(
                text=code,
                center=token.center,
                box=token.box,
                size=token.size,
                source="ocr_standalone",
                order=token.order,
                line_id=token.line_id,
            )
        )
    return candidates, explicit_codes


def _ocr_chainage_candidates(tokens: list[_LocatedText]) -> list[_LocatedText]:
    by_line: dict[str, list[_LocatedText]] = {}
    for token in tokens:
        by_line.setdefault(token.line_id, []).append(token)

    located: list[_LocatedText] = []
    seen: set[tuple[int, int, int]] = set()
    for line_tokens in by_line.values():
        line_tokens.sort(key=lambda item: item.order)
        for start in range(len(line_tokens)):
            for length in range(1, min(3, len(line_tokens) - start) + 1):
                window = line_tokens[start : start + length]
                raw = (
                    "".join(item.text for item in window)
                    .replace("O", "0")
                    .replace("o", "0")
                )
                match = _CHAINAGE_PATTERN.fullmatch(raw.strip())
                if match is None:
                    continue
                value = int(match.group(1)) * 1000 + int(match.group(2))
                box = _union_boxes(item.box for item in window)
                key = (value, round(_center(box)[0]), round(_center(box)[1]))
                if key in seen:
                    continue
                seen.add(key)
                located.append(
                    _LocatedText(
                        text=match.group(0).replace(" ", ""),
                        center=_center(box),
                        box=box,
                        size=max(box[2] - box[0], box[3] - box[1]),
                        value=value,
                        source="ocr",
                        order=window[0].order,
                        line_id=window[0].line_id,
                    )
                )
    return located


def _dot(point: tuple[float, float], axis: tuple[float, float]) -> float:
    return point[0] * axis[0] + point[1] * axis[1]


def _display_point(
    point: tuple[float, float], page_width: float, page_height: float
) -> tuple[float, float]:
    return (point[0] / page_width, 1.0 - point[1] / page_height)


def _select_boundaries(
    labels: list[_LocatedText],
    chainages: list[_LocatedText],
    page_width: float,
    page_height: float,
) -> tuple[list[float], list[Optional[int]], list[str]]:
    normalized_labels = [
        _display_point(label.center, page_width, page_height) for label in labels
    ]
    first = normalized_labels[0]
    last = normalized_labels[-1]
    dx = last[0] - first[0]
    dy = last[1] - first[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        raise ZonePlanParseError(
            "Detected zone labels do not define a usable direction"
        )
    axis = (dx / length, dy / length)
    label_projections = [_dot(point, axis) for point in normalized_labels]
    expected = [
        label_projections[0] - (label_projections[1] - label_projections[0]) / 2.0,
        *[
            (left + right) / 2.0
            for left, right in zip(label_projections, label_projections[1:])
        ],
        label_projections[-1] + (label_projections[-1] - label_projections[-2]) / 2.0,
    ]

    normalized_chainages: list[tuple[_LocatedText, float, float]] = []
    for marker in chainages:
        point = _display_point(marker.center, page_width, page_height)
        projection = _dot(point, axis)
        perpendicular = abs(
            (point[0] - first[0]) * -axis[1] + (point[1] - first[1]) * axis[0]
        )
        if perpendicular <= 0.12:
            normalized_chainages.append((marker, projection, perpendicular))

    selected: list[tuple[_LocatedText, float]] = []
    previous_projection = -math.inf
    previous_value = -1
    used: set[int] = set()
    for target in expected:
        options = [
            (index, marker, projection, perpendicular)
            for index, (marker, projection, perpendicular) in enumerate(
                normalized_chainages
            )
            if index not in used
            and projection > previous_projection + 1e-5
            and marker.value is not None
            and marker.value > previous_value
        ]
        if not options:
            selected = []
            break
        chosen = min(
            options,
            key=lambda item: abs(item[2] - target) + item[3] * 0.35,
        )
        index, marker, projection, _ = chosen
        if abs(projection - target) > 0.11:
            selected = []
            break
        used.add(index)
        selected.append((marker, projection))
        previous_projection = projection
        previous_value = int(marker.value or 0)

    warnings: list[str] = []
    if len(selected) == len(expected):
        boundary_values = [int(marker.value or 0) for marker, _ in selected]
        if all(
            right > left for left, right in zip(boundary_values, boundary_values[1:])
        ):
            return (
                [projection for _, projection in selected],
                boundary_values,
                warnings,
            )

    warnings.append(
        "Exact chainage markers could not be paired with every zone boundary; label midpoints were used."
    )
    return expected, [None] * len(expected), warnings


def _clip_polygon(
    polygon: list[tuple[float, float]],
    *,
    axis: tuple[float, float],
    threshold: float,
    keep_greater: bool,
) -> list[tuple[float, float]]:
    if not polygon:
        return []

    def inside(point: tuple[float, float]) -> bool:
        value = _dot(point, axis)
        return value >= threshold - 1e-9 if keep_greater else value <= threshold + 1e-9

    def intersection(
        start: tuple[float, float], end: tuple[float, float]
    ) -> tuple[float, float]:
        start_value = _dot(start, axis)
        end_value = _dot(end, axis)
        denominator = end_value - start_value
        if abs(denominator) <= 1e-12:
            return start
        ratio = (threshold - start_value) / denominator
        return (
            start[0] + (end[0] - start[0]) * ratio,
            start[1] + (end[1] - start[1]) * ratio,
        )

    output: list[tuple[float, float]] = []
    previous = polygon[-1]
    previous_inside = inside(previous)
    for current in polygon:
        current_inside = inside(current)
        if current_inside:
            if not previous_inside:
                output.append(intersection(previous, current))
            output.append(current)
        elif previous_inside:
            output.append(intersection(previous, current))
        previous = current
        previous_inside = current_inside
    return output


def _zone_polygon(
    *,
    axis: tuple[float, float],
    start: float,
    end: float,
    width: float,
    height: float,
) -> list[dict[str, float]]:
    polygon = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    polygon = _clip_polygon(polygon, axis=axis, threshold=start, keep_greater=True)
    polygon = _clip_polygon(polygon, axis=axis, threshold=end, keep_greater=False)
    if len(polygon) < 3:
        raise ZonePlanParseError("A detected zone produced an invalid polygon")
    return [
        {"x": round(point[0] * width, 6), "y": round(point[1] * height, 6)}
        for point in polygon
    ]


def _format_chainage(value: Optional[int]) -> str:
    if value is None:
        return ""
    return f"{value // 1000}+{value % 1000:03d}"


def parse_zone_plan_pdf(
    raw_bytes: bytes,
    *,
    filename: str = "zone-plan.pdf",
    floorplan_bounds: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if pdfium is None:
        raise ZonePlanParseError(
            "Zone-plan PDF support is unavailable because pypdfium2 is not installed"
        )
    safe_filename = Path(filename or "zone-plan.pdf").name
    if Path(safe_filename).suffix.lower() != ".pdf":
        raise ZonePlanParseError("Zone plan must be a PDF file")
    if not raw_bytes.startswith(b"%PDF-"):
        raise ZonePlanParseError("The uploaded zone plan is not a valid PDF")
    if len(raw_bytes) > _MAX_PDF_BYTES:
        raise ZonePlanParseError("Zone plan PDF must be 30 MB or smaller")

    document = None
    page = None
    text_page = None
    try:
        document = pdfium.PdfDocument(raw_bytes)
        page_count = len(document)
        if page_count < 1:
            raise ZonePlanParseError("The zone plan PDF has no pages")
        page = document[0]
        page_width, page_height = (float(value) for value in page.get_size())
        if page_width <= 0 or page_height <= 0:
            raise ZonePlanParseError("The first PDF page has invalid dimensions")
        text_page = page.get_textpage()
        text = text_page.get_text_range()
        page_diagonal = math.hypot(page_width, page_height)
        labels: Optional[list[_LocatedText]] = None
        vector_error: Optional[Exception] = None
        if text.strip():
            try:
                candidates, explicit_codes = _label_candidates(
                    text_page, text, page_diagonal
                )
                labels = _select_zone_labels(candidates, explicit_codes, page_diagonal)
            except ZonePlanParseError as error:
                vector_error = error

        ocr_tokens: Optional[list[_LocatedText]] = None
        if labels is None:
            try:
                ocr_tokens = _ocr_page_tokens(page, page_width, page_height)
                candidates, explicit_codes = _ocr_label_candidates(
                    ocr_tokens, page_diagonal
                )
                labels = _select_zone_labels(candidates, explicit_codes, page_diagonal)
            except ZonePlanParseError as ocr_error:
                vector_reason = (
                    str(vector_error)
                    if vector_error is not None
                    else "No searchable/vector zone labels were found"
                )
                raise ZonePlanParseError(
                    f"Zone labels could not be detected. {vector_reason}. "
                    f"OCR fallback also failed: {ocr_error}"
                ) from ocr_error

        chainages = _chainage_candidates(text_page, text) if text.strip() else []
        if len(chainages) < len(labels) + 1:
            if ocr_tokens is None:
                try:
                    ocr_tokens = _ocr_page_tokens(page, page_width, page_height)
                except ZonePlanParseError:
                    ocr_tokens = []
            chainages.extend(_ocr_chainage_candidates(ocr_tokens))

        label_uses_ocr = any(label.source.startswith("ocr") for label in labels)
        chainage_uses_ocr = any(marker.source == "ocr" for marker in chainages)
        if label_uses_ocr and chainage_uses_ocr:
            extraction_method = "ocr"
        elif label_uses_ocr or chainage_uses_ocr:
            extraction_method = "hybrid"
        else:
            extraction_method = "vector_text"

        boundaries, boundary_values, warnings = _select_boundaries(
            labels, chainages, page_width, page_height
        )
        if label_uses_ocr:
            warnings.insert(
                0,
                "Zone labels were extracted with backend OCR; review the detected names and order.",
            )
        elif chainage_uses_ocr:
            warnings.insert(
                0,
                "Zone labels came from vector text and chainage markers were supplemented with backend OCR.",
            )

        normalized_labels = [
            _display_point(label.center, page_width, page_height) for label in labels
        ]
        axis_dx = normalized_labels[-1][0] - normalized_labels[0][0]
        axis_dy = normalized_labels[-1][1] - normalized_labels[0][1]
        axis_length = math.hypot(axis_dx, axis_dy)
        axis = (axis_dx / axis_length, axis_dy / axis_length)
        if abs(axis[0]) >= abs(axis[1]):
            orientation = "left_to_right" if axis[0] >= 0 else "right_to_left"
        else:
            orientation = "top_to_bottom" if axis[1] >= 0 else "bottom_to_top"

        raw_bounds = floorplan_bounds or {}
        try:
            floorplan_width = float(raw_bounds.get("width") or page_width)
            floorplan_height = float(raw_bounds.get("height") or page_height)
        except (TypeError, ValueError) as error:
            raise ZonePlanParseError(
                "The project floorplan has invalid dimensions"
            ) from error
        if floorplan_width <= 0 or floorplan_height <= 0:
            raise ZonePlanParseError("The project floorplan has invalid dimensions")

        zones: list[dict[str, Any]] = []
        for index, label in enumerate(labels):
            start_value = boundary_values[index]
            end_value = boundary_values[index + 1]
            zones.append(
                {
                    "name": f"Zone {label.text}",
                    "code": label.text,
                    "start_chainage_m": start_value,
                    "end_chainage_m": end_value,
                    "start_chainage": _format_chainage(start_value),
                    "end_chainage": _format_chainage(end_value),
                    "points": _zone_polygon(
                        axis=axis,
                        start=boundaries[index],
                        end=boundaries[index + 1],
                        width=floorplan_width,
                        height=floorplan_height,
                    ),
                    "source": "zone_plan_pdf",
                    "extraction_method": label.source,
                }
            )

        warnings.append(
            "Zones were aligned by the PDF page bounds. Confirm that the uploaded zone plan uses the same crop and orientation as the project floorplan."
        )
        chainage_start = boundary_values[0]
        chainage_end = boundary_values[-1]
        return {
            "source_type": "zone_plan_pdf",
            "source_filename": safe_filename,
            "page_count": page_count,
            "page_index": 0,
            "page_width": page_width,
            "page_height": page_height,
            "orientation": orientation,
            "extraction_method": extraction_method,
            "ocr_used": label_uses_ocr or chainage_uses_ocr,
            "zones": zones,
            "summary": {
                "zone_count": len(zones),
                "zone_names": [zone["name"] for zone in zones],
                "chainage_start_m": chainage_start,
                "chainage_end_m": chainage_end,
                "chainage_start": _format_chainage(chainage_start),
                "chainage_end": _format_chainage(chainage_end),
                "total_chainage_m": (
                    chainage_end - chainage_start
                    if chainage_start is not None and chainage_end is not None
                    else None
                ),
                "orientation": orientation,
                "extraction_method": extraction_method,
                "ocr_used": label_uses_ocr or chainage_uses_ocr,
            },
            "warnings": warnings,
        }
    except ZonePlanParseError:
        raise
    except Exception as error:
        raise ZonePlanParseError(f"Unable to read zone plan PDF: {error}") from error
    finally:
        if text_page is not None:
            text_page.close()
        if page is not None:
            page.close()
        if document is not None:
            document.close()
