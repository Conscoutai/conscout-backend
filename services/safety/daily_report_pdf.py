from __future__ import annotations

import base64
import os
import tempfile
from datetime import datetime
from typing import Any, Iterable, Optional

from fpdf import FPDF


NAVY = (11, 35, 71)
BLUE = (43, 125, 225)
BLUE_SOFT = (239, 246, 255)
INK = (20, 31, 55)
SLATE = (103, 120, 150)
BORDER = (216, 226, 240)
PANEL = (248, 250, 253)
GREEN = (22, 163, 74)
GREEN_SOFT = (237, 249, 242)
AMBER = (245, 158, 11)
AMBER_SOFT = (255, 248, 235)
RED = (220, 38, 38)
RED_SOFT = (254, 242, 242)
PURPLE = (124, 58, 237)
CYAN = (6, 182, 212)


# Exact ConScout mark used by the Flutter Progress and Comment reports, resized
# to 64 px for compact embedding in the backend-generated PDF.
_CONSCOUT_LOGO_BASE64 = (
    "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"
)


def _latin1(value: Any, fallback: str = "Not available") -> str:
    if value is None:
        text = fallback
    else:
        text = str(value).strip() or fallback
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _metric(value: Any, suffix: str = "", decimals: int = 0) -> str:
    parsed = _number(value)
    if parsed is None:
        return "--"
    if decimals == 0 and parsed.is_integer():
        result = str(int(parsed))
    else:
        result = f"{parsed:.{decimals}f}"
    return f"{result}{suffix}"


def _signed_metric(value: Any) -> str:
    parsed = _number(value)
    if parsed is None:
        return "--"
    return f"{parsed:+g}"


def _short_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "Not available"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%d %b %Y")
    except ValueError:
        return raw


def _timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "Not recorded"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime(
            "%d %b %Y - %H:%M"
        )
    except ValueError:
        return raw


def _actor(value: Any) -> str:
    if isinstance(value, dict):
        return _latin1(value.get("name") or value.get("email") or value.get("user_id"))
    return _latin1(value)


def _status_palette(status: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    normalized = status.lower().replace(" ", "_")
    if normalized in {"safe", "finalized", "approved", "active", "completed"}:
        return GREEN, GREEN_SOFT
    if normalized in {"stop_work", "critical", "high", "failed", "overdue"}:
        return RED, RED_SOFT
    if normalized in {"caution", "warning", "draft", "pending"}:
        return AMBER, AMBER_SOFT
    if normalized in {"reviewed", "preliminary", "in_progress"}:
        return BLUE, BLUE_SOFT
    return SLATE, PANEL


def _truncate(pdf: FPDF, text: Any, width: float) -> str:
    result = _latin1(text)
    if pdf.get_string_width(result) <= width:
        return result
    suffix = "..."
    while result and pdf.get_string_width(result + suffix) > width:
        result = result[:-1]
    return result + suffix


class _SafetyDailyPdf(FPDF):
    def __init__(self, *, logo_path: str, report_id: str, issued_at: str) -> None:
        super().__init__("P", "mm", "A4")
        self.logo_path = logo_path
        self.report_id = report_id
        self.issued_at = issued_at
        self.set_margins(12, 32, 12)
        self.set_auto_page_break(auto=True, margin=17)
        self.alias_nb_pages()

    def header(self) -> None:
        if self.page_no() > 1:
            self.set_y(12)
            return

        self.set_fill_color(*NAVY)
        self.rect(0, 0, self.w, 4, style="F")
        self.image(self.logo_path, x=12, y=9, w=12, h=12)
        self.set_xy(27, 9)
        self.set_text_color(*NAVY)
        self.set_font("Helvetica", "B", 9)
        self.cell(37, 5, "CONSCOUT", ln=2)
        self.set_font("Helvetica", "", 6.5)
        self.set_text_color(*SLATE)
        self.cell(37, 4, "CONSTRUCTION MONITOR")

        self.set_xy(66, 10)
        self.set_text_color(*NAVY)
        self.set_font("Helvetica", "B", 13)
        self.cell(78, 7, "SAFETY & MANPOWER DAILY REPORT", align="C")

        self.set_draw_color(*BORDER)
        self.set_xy(149, 8)
        self.set_font("Helvetica", "B", 5.8)
        self.set_text_color(*SLATE)
        self.cell(18, 5, "REPORT ID", border=1)
        self.set_font("Helvetica", "", 5.8)
        self.set_text_color(*INK)
        self.cell(31, 5, _truncate(self, self.report_id, 28), border=1, ln=2)
        self.set_x(149)
        self.set_font("Helvetica", "B", 5.8)
        self.set_text_color(*SLATE)
        self.cell(18, 5, "ISSUED", border=1)
        self.set_font("Helvetica", "", 5.8)
        self.set_text_color(*INK)
        self.cell(31, 5, _truncate(self, self.issued_at, 28), border=1)

        self.set_draw_color(*BORDER)
        self.line(12, 26, 198, 26)
        self.set_text_color(*INK)
        self.set_y(30)

    def footer(self) -> None:
        self.set_y(-13)
        self.set_draw_color(*BORDER)
        self.line(12, self.get_y(), 198, self.get_y())
        self.ln(2)
        self.set_font("Helvetica", "", 6.5)
        self.set_text_color(*SLATE)
        self.cell(145, 5, "Generated from the Safety workspace - ConScout")
        self.set_font("Helvetica", "B", 6.5)
        self.cell(41, 5, f"Page {self.page_no()}/{{nb}}", align="R")


def _panel(pdf: FPDF, x: float, y: float, w: float, h: float) -> None:
    pdf.set_fill_color(255, 255, 255)
    pdf.set_draw_color(*BORDER)
    pdf.rect(x, y, w, h, style="DF")


def _panel_title(pdf: FPDF, x: float, y: float, title: str, subtitle: str = "") -> None:
    pdf.set_xy(x, y)
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.cell(0, 5, _latin1(title))
    if subtitle:
        pdf.set_xy(x, y + 5)
        pdf.set_text_color(*SLATE)
        pdf.set_font("Helvetica", "", 6.2)
        pdf.cell(0, 4, _latin1(subtitle))


def _project_strip(pdf: FPDF, *, context: dict[str, Any], report: dict[str, Any]) -> None:
    x, y, w, h = 12.0, pdf.get_y(), 186.0, 18.0
    pdf.set_fill_color(*PANEL)
    pdf.set_draw_color(*BORDER)
    pdf.rect(x, y, w, h, style="DF")
    values = [
        ("PROJECT", context.get("site_name") or context.get("project_id")),
        ("REPORT DATE", _short_date(report.get("record_date"))),
        ("REVISION", f"Revision {report.get('revision') or 1}"),
        ("STATUS", str(report.get("status") or "draft").replace("_", " ").title()),
    ]
    cell_w = w / len(values)
    for index, (label, value) in enumerate(values):
        cx = x + index * cell_w
        if index:
            pdf.set_draw_color(*BORDER)
            pdf.line(cx, y + 4, cx, y + h - 4)
        pdf.set_xy(cx + 4, y + 4)
        pdf.set_font("Helvetica", "B", 5.8)
        pdf.set_text_color(*SLATE)
        pdf.cell(cell_w - 8, 4, label)
        pdf.set_xy(cx + 4, y + 9)
        pdf.set_font("Helvetica", "B", 8.2)
        pdf.set_text_color(*INK)
        pdf.cell(cell_w - 8, 5, _truncate(pdf, value, cell_w - 9))
    pdf.set_y(y + h)


def _work_state_banner(pdf: FPDF, state: dict[str, Any]) -> None:
    status = str(state.get("status") or "unknown").lower()
    accent, soft = _status_palette(status)
    reasons = [_latin1(reason) for reason in (state.get("reasons") or []) if reason]
    x, y, w, h = 12.0, pdf.get_y() + 4, 186.0, 17.0
    pdf.set_fill_color(*soft)
    pdf.set_draw_color(*accent)
    pdf.rect(x, y, w, h, style="DF")
    pdf.set_fill_color(*accent)
    pdf.rect(x, y, 3, h, style="F")
    pdf.set_xy(x + 7, y + 3)
    pdf.set_text_color(*accent)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.cell(37, 5, f"WORK STATE: {status.replace('_', ' ').upper()}")
    pdf.set_xy(x + 49, y + 3)
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "B", 7.2)
    summary = reasons[0] if reasons else "No automated intervention reason recorded."
    pdf.cell(w - 57, 5, _truncate(pdf, summary, w - 59))
    pdf.set_xy(x + 49, y + 9)
    pdf.set_text_color(*SLATE)
    pdf.set_font("Helvetica", "", 6.2)
    extra = (
        f"{len(reasons) - 1} additional reason(s) listed in the action panel."
        if len(reasons) > 1
        else "Status calculated from captured safety, labor, and weather data."
    )
    pdf.cell(w - 57, 4, extra)
    pdf.set_y(y + h)


def _kpi_cards(
    pdf: FPDF,
    *,
    manpower: dict[str, Any],
    ppe: dict[str, Any],
    counts: dict[str, Any],
) -> None:
    cards = [
        ("XER PLANNED FTE", _metric(manpower.get("planned_workers")), "Schedule plan", BLUE),
        ("AI DETECTED", _metric(manpower.get("observed_workers")), "Tour headcount", GREEN),
        ("VARIANCE", _signed_metric(manpower.get("variance")), "Actual vs plan", RED if (_number(manpower.get("variance")) or 0) < 0 else GREEN),
        ("PPE COMPLIANCE", _metric(ppe.get("compliance_percent"), "%", 1), "Reviewed findings", PURPLE),
        ("OPEN HAZARDS", _metric(counts.get("open_hazards")), "Awaiting closure", RED),
        ("ACTIVE PTW", _metric(counts.get("active_permits")), "Approved controls", GREEN),
    ]
    x, y, total_w, gap, h = 12.0, pdf.get_y() + 4, 186.0, 3.0, 25.0
    card_w = (total_w - gap * (len(cards) - 1)) / len(cards)
    for index, (label, value, caption, color) in enumerate(cards):
        cx = x + index * (card_w + gap)
        pdf.set_fill_color(255, 255, 255)
        pdf.set_draw_color(*BORDER)
        pdf.rect(cx, y, card_w, h, style="DF")
        pdf.set_fill_color(*color)
        pdf.rect(cx, y, card_w, 2, style="F")
        pdf.set_xy(cx + 3, y + 4)
        pdf.set_text_color(*SLATE)
        pdf.set_font("Helvetica", "B", 5.2)
        pdf.cell(card_w - 6, 4, _truncate(pdf, label, card_w - 6), align="C")
        pdf.set_xy(cx + 2, y + 9)
        pdf.set_text_color(*color)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(card_w - 4, 7, _truncate(pdf, value, card_w - 4), align="C")
        pdf.set_xy(cx + 2, y + 18)
        pdf.set_text_color(*SLATE)
        pdf.set_font("Helvetica", "", 5.4)
        pdf.cell(card_w - 4, 4, _truncate(pdf, caption, card_w - 4), align="C")
    pdf.set_y(y + h)


def _workforce_chart(pdf: FPDF, history: list[dict[str, Any]], x: float, y: float, w: float, h: float) -> None:
    _panel(pdf, x, y, w, h)
    _panel_title(pdf, x + 5, y + 4, "7-DAY WORKFORCE TREND", "XER planned FTE vs reviewed tour-AI headcount")
    rows = history[-7:]
    has_values = any(
        _number(row.get("planned_workers")) is not None
        or _number(row.get("observed_workers")) is not None
        for row in rows
    )
    if not rows or not has_values:
        pdf.set_xy(x + 6, y + 29)
        pdf.set_text_color(*SLATE)
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(w - 12, 6, "No workforce history available for charting.", align="C")
        pdf.set_xy(x + 6, y + 37)
        pdf.set_font("Helvetica", "", 6.2)
        pdf.cell(w - 12, 4, "Analyse dated site tours to populate this trend.", align="C")
        return

    pdf.set_fill_color(*BLUE)
    pdf.rect(x + w - 40, y + 6.2, 4, 2.2, style="F")
    pdf.set_font("Helvetica", "", 5.7)
    pdf.set_text_color(*SLATE)
    pdf.set_xy(x + w - 35, y + 5)
    pdf.cell(11, 4, "Plan")
    pdf.set_fill_color(*GREEN)
    pdf.rect(x + w - 22, y + 6.2, 4, 2.2, style="F")
    pdf.set_xy(x + w - 17, y + 5)
    pdf.cell(14, 4, "AI")

    chart_x, chart_y = x + 8, y + 17
    chart_w, chart_h = w - 14, h - 25
    maximum = max(
        [
            value
            for row in rows
            for value in (
                _number(row.get("planned_workers")),
                _number(row.get("observed_workers")),
            )
            if value is not None
        ]
        or [1]
    )
    maximum = max(maximum, 1)
    pdf.set_draw_color(*BORDER)
    for step in range(3):
        gy = chart_y + (chart_h - 8) * step / 2
        pdf.line(chart_x, gy, chart_x + chart_w, gy)
    group_w = chart_w / len(rows)
    bar_w = min(3.6, max(2.2, group_w * 0.24))
    for index, row in enumerate(rows):
        center = chart_x + group_w * index + group_w / 2
        planned = _number(row.get("planned_workers"))
        observed = _number(row.get("observed_workers"))
        for offset, value, color in ((-bar_w, planned, BLUE), (0, observed, GREEN)):
            if value is None:
                continue
            bar_h = max(0.8, (value / maximum) * (chart_h - 12))
            pdf.set_fill_color(*color)
            pdf.rect(center + offset, chart_y + chart_h - 8 - bar_h, bar_w, bar_h, style="F")
        label = str(row.get("record_date") or "")[-5:]
        pdf.set_xy(center - group_w / 2, chart_y + chart_h - 6)
        pdf.set_text_color(*SLATE)
        pdf.set_font("Helvetica", "", 5.2)
        pdf.cell(group_w, 4, _latin1(label, "--"), align="C")


def _control_chart(pdf: FPDF, counts: dict[str, Any], x: float, y: float, w: float, h: float) -> None:
    _panel(pdf, x, y, w, h)
    _panel_title(pdf, x + 5, y + 4, "SAFETY CONTROL LOAD", "Current open or active site controls")
    items = [
        ("Open hazards", int(_number(counts.get("open_hazards")) or 0), RED),
        ("Active PTW", int(_number(counts.get("active_permits")) or 0), GREEN),
        ("Overdue inspections", int(_number(counts.get("overdue_checks")) or 0), AMBER),
        ("Exclusion zones", int(_number(counts.get("active_zones")) or 0), PURPLE),
        ("Pending AI reviews", int(_number(counts.get("pending_reviews")) or 0), BLUE),
    ]
    maximum = max([value for _, value, _ in items] or [1])
    maximum = max(maximum, 1)
    start_y = y + 16
    for index, (label, value, color) in enumerate(items):
        row_y = start_y + index * 8
        pdf.set_xy(x + 5, row_y)
        pdf.set_text_color(*SLATE)
        pdf.set_font("Helvetica", "", 5.8)
        pdf.cell(28, 4, _truncate(pdf, label, 27))
        bar_x, bar_w = x + 35, w - 48
        pdf.set_fill_color(235, 240, 247)
        pdf.rect(bar_x, row_y + 0.8, bar_w, 3, style="F")
        if value:
            pdf.set_fill_color(*color)
            pdf.rect(bar_x, row_y + 0.8, max(1.2, bar_w * value / maximum), 3, style="F")
        pdf.set_xy(x + w - 11, row_y)
        pdf.set_text_color(*color if value else GREEN)
        pdf.set_font("Helvetica", "B", 6.5)
        pdf.cell(7, 4, str(value), align="R")


def _weather_panel(pdf: FPDF, weather: dict[str, Any], x: float, y: float, w: float, h: float) -> None:
    _panel(pdf, x, y, w, h)
    provider = str(weather.get("provider") or "unavailable").replace("_", " ").title()
    observed = _timestamp(weather.get("observed_at"))
    _panel_title(pdf, x + 5, y + 4, "LOCATION-BASED WEATHER", f"{provider} - observed {observed}")
    metrics = [
        ("TEMPERATURE", _metric(weather.get("temperature_c"), " C", 1), BLUE),
        ("FEELS LIKE", _metric(weather.get("apparent_temperature_c"), " C", 1), RED),
        ("WIND / GUST", _metric(weather.get("wind_kph"), " km/h", 1), CYAN),
        ("RAIN RATE", _metric(weather.get("precipitation_mm_h"), " mm/h", 1), BLUE),
        ("HUMIDITY", _metric(weather.get("relative_humidity_percent"), "%", 0), PURPLE),
    ]
    gap = 3.0
    card_w = (w - 10 - gap * (len(metrics) - 1)) / len(metrics)
    for index, (label, value, color) in enumerate(metrics):
        cx = x + 5 + index * (card_w + gap)
        cy = y + 16
        pdf.set_fill_color(*PANEL)
        pdf.set_draw_color(*BORDER)
        pdf.rect(cx, cy, card_w, h - 21, style="DF")
        pdf.set_fill_color(*color)
        pdf.rect(cx, cy, 2, h - 21, style="F")
        pdf.set_xy(cx + 4, cy + 4)
        pdf.set_text_color(*SLATE)
        pdf.set_font("Helvetica", "B", 5.2)
        pdf.cell(card_w - 6, 4, label)
        pdf.set_xy(cx + 4, cy + 10)
        pdf.set_text_color(*color)
        pdf.set_font("Helvetica", "B", 8.3)
        pdf.cell(card_w - 6, 5, _truncate(pdf, value, card_w - 6))


def _ppe_panel(pdf: FPDF, ppe: dict[str, Any], x: float, y: float, w: float, h: float) -> None:
    _panel(pdf, x, y, w, h)
    _panel_title(pdf, x + 5, y + 4, "PPE COMPLIANCE", "Reviewed visual findings only")
    compliant = int(_number(ppe.get("compliant")) or 0)
    non_compliant = int(_number(ppe.get("non_compliant")) or 0)
    unknown = int(_number(ppe.get("unknown")) or 0)
    total = compliant + non_compliant + unknown
    bar_x, bar_y, bar_w = x + 6, y + 18, w - 12
    pdf.set_fill_color(232, 238, 246)
    pdf.rect(bar_x, bar_y, bar_w, 6, style="F")
    if total:
        cursor = bar_x
        for value, color in ((compliant, GREEN), (non_compliant, RED), (unknown, SLATE)):
            if not value:
                continue
            segment = bar_w * value / total
            pdf.set_fill_color(*color)
            pdf.rect(cursor, bar_y, segment, 6, style="F")
            cursor += segment
    else:
        pdf.set_xy(x + 6, y + 26)
        pdf.set_font("Helvetica", "", 6.5)
        pdf.set_text_color(*SLATE)
        pdf.cell(w - 12, 4, "No reviewed PPE evidence is available.", align="C")
    legends = [
        ("Compliant", compliant, GREEN),
        ("Non-compliant", non_compliant, RED),
        ("Unknown", unknown, SLATE),
    ]
    for index, (label, value, color) in enumerate(legends):
        lx = x + 6 + index * ((w - 12) / 3)
        pdf.set_fill_color(*color)
        pdf.rect(lx, y + h - 13, 3, 3, style="F")
        pdf.set_xy(lx + 4, y + h - 15)
        pdf.set_text_color(*INK)
        pdf.set_font("Helvetica", "B", 5.8)
        pdf.cell((w - 12) / 3 - 4, 6, f"{label}: {value}")


def _actions_panel(pdf: FPDF, reasons: Iterable[Any], x: float, y: float, w: float, h: float) -> None:
    _panel(pdf, x, y, w, h)
    _panel_title(pdf, x + 5, y + 4, "SAFETY REASONS & ACTIONS", "Automated conditions requiring review")
    entries = [_latin1(reason) for reason in reasons if str(reason or "").strip()]
    if not entries:
        entries = ["No automated intervention reason was recorded for this snapshot."]
    pdf.set_xy(x + 6, y + 16)
    pdf.set_font("Helvetica", "", 6.2)
    for index, reason in enumerate(entries[:4], start=1):
        color = RED if "stop" in reason.lower() or "critical" in reason.lower() else AMBER
        pdf.set_fill_color(*color)
        pdf.rect(x + 6, pdf.get_y() + 1.1, 2.5, 2.5, style="F")
        pdf.set_x(x + 11)
        pdf.set_text_color(*INK)
        pdf.cell(w - 17, 5.8, _truncate(pdf, f"{index}. {reason}", w - 18), ln=1)
    if len(entries) > 4:
        pdf.set_x(x + 11)
        pdf.set_text_color(*SLATE)
        pdf.set_font("Helvetica", "I", 5.8)
        pdf.cell(w - 17, 5, f"+ {len(entries) - 4} additional item(s)")


def _record_label(record: dict[str, Any]) -> str:
    return _latin1(
        record.get("title")
        or record.get("activity_type")
        or record.get("permit_type")
        or record.get("check_type")
        or "Safety record"
    )


def _draw_record_table(
    pdf: FPDF,
    *,
    title: str,
    records: list[dict[str, Any]],
    x: float,
    y: float,
    w: float,
    h: float,
) -> None:
    _panel(pdf, x, y, w, h)
    pdf.set_fill_color(*BLUE_SOFT)
    pdf.rect(x, y, w, 9, style="F")
    pdf.set_xy(x + 4, y + 2)
    pdf.set_text_color(*NAVY)
    pdf.set_font("Helvetica", "B", 7.5)
    pdf.cell(w - 8, 5, f"{title.upper()} ({len(records)})")
    header_y = y + 11
    widths = (w * 0.56, w * 0.22, w * 0.22)
    headers = ("Record", "Status", "Risk / Type")
    pdf.set_fill_color(*PANEL)
    pdf.set_draw_color(*BORDER)
    pdf.set_xy(x + 3, header_y)
    pdf.set_font("Helvetica", "B", 5.5)
    pdf.set_text_color(*SLATE)
    for width, header in zip(widths, headers):
        pdf.cell(width - 2, 6, header, border=1, fill=True)
    pdf.ln(6)
    if not records:
        pdf.set_xy(x + 5, header_y + 11)
        pdf.set_font("Helvetica", "", 6.5)
        pdf.set_text_color(*SLATE)
        pdf.cell(w - 10, 5, "No records included in this daily snapshot.", align="C")
        return
    for index, record in enumerate(records[:6]):
        row_y = header_y + 6 + index * 6
        if row_y + 6 > y + h - 3:
            break
        pdf.set_xy(x + 3, row_y)
        pdf.set_font("Helvetica", "", 5.7)
        pdf.set_text_color(*INK)
        values = (
            _record_label(record),
            str(record.get("status") or "unknown").replace("_", " ").title(),
            str(
                record.get("severity")
                or record.get("permit_type")
                or record.get("check_type")
                or record.get("source")
                or "--"
            ).replace("_", " ").title(),
        )
        for width, value in zip(widths, values):
            pdf.cell(width - 2, 6, _truncate(pdf, value, width - 4), border=1)


def _signoff_panel(pdf: FPDF, report: dict[str, Any], x: float, y: float, w: float, h: float) -> None:
    _panel(pdf, x, y, w, h)
    _panel_title(pdf, x + 5, y + 4, "AUDIT WORKFLOW & SIGN-OFF", "Immutable revision history for compliance evidence")
    columns = [
        ("GENERATED", report.get("generated_at") or report.get("created_at"), report.get("created_by"), BLUE),
        ("REVIEWED", report.get("reviewed_at"), report.get("reviewed_by"), AMBER),
        ("FINALIZED", report.get("finalized_at"), report.get("finalized_by"), GREEN),
    ]
    gap = 4.0
    cell_w = (w - 10 - gap * 2) / 3
    for index, (label, timestamp, actor, color) in enumerate(columns):
        cx = x + 5 + index * (cell_w + gap)
        cy = y + 16
        pdf.set_fill_color(*PANEL)
        pdf.set_draw_color(*BORDER)
        pdf.rect(cx, cy, cell_w, h - 21, style="DF")
        pdf.set_fill_color(*color)
        pdf.rect(cx, cy, cell_w, 2, style="F")
        pdf.set_xy(cx + 4, cy + 5)
        pdf.set_font("Helvetica", "B", 6)
        pdf.set_text_color(*color)
        pdf.cell(cell_w - 8, 4, label)
        pdf.set_xy(cx + 4, cy + 11)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_text_color(*INK)
        pdf.cell(cell_w - 8, 4, _truncate(pdf, _actor(actor), cell_w - 8))
        pdf.set_xy(cx + 4, cy + 17)
        pdf.set_font("Helvetica", "", 5.8)
        pdf.set_text_color(*SLATE)
        pdf.cell(cell_w - 8, 4, _truncate(pdf, _timestamp(timestamp), cell_w - 8))


def _data_note(pdf: FPDF, snapshot: dict[str, Any], x: float, y: float, w: float, h: float) -> None:
    manpower = snapshot.get("manpower") or {}
    ppe = snapshot.get("ppe") or {}
    notes = [
        "This report is a point-in-time audit snapshot generated from records available in ConScout.",
        "AI workforce and PPE values remain unavailable when a selected tour has not been analysed or reviewed.",
    ]
    warning = str(manpower.get("schedule_resource_warning") or "").strip()
    if warning:
        notes.append(warning)
    model_note = str(ppe.get("model_note") or "").strip()
    if model_note:
        notes.append(model_note)
    pdf.set_fill_color(*BLUE_SOFT)
    pdf.set_draw_color(*BORDER)
    pdf.rect(x, y, w, h, style="DF")
    pdf.set_xy(x + 5, y + 4)
    pdf.set_font("Helvetica", "B", 7)
    pdf.set_text_color(*NAVY)
    pdf.cell(w - 10, 5, "DATA QUALITY & AUDIT NOTE")
    pdf.set_xy(x + 6, y + 11)
    pdf.set_font("Helvetica", "", 6.2)
    pdf.set_text_color(*INK)
    for note in notes[:3]:
        pdf.cell(3, 5, "-")
        pdf.cell(w - 15, 5, _truncate(pdf, note, w - 16), ln=1)
        pdf.set_x(x + 6)


def build_daily_report_pdf(*, context: dict[str, Any], report: dict[str, Any]) -> bytes:
    snapshot = report.get("snapshot") or {}
    manpower = snapshot.get("manpower") or {}
    ppe = snapshot.get("ppe") or {}
    counts = snapshot.get("counts") or {}
    weather = snapshot.get("weather") or {}
    state = snapshot.get("work_state") or {}
    recent = snapshot.get("recent") or {}
    history = [item for item in (snapshot.get("manpower_history") or []) if isinstance(item, dict)]
    report_id = str(
        report.get("record_id")
        or f"SAF-{str(report.get('record_date') or '').replace('-', '')}-R{report.get('revision') or 1}"
    )
    issued_at = _timestamp(report.get("generated_at") or report.get("created_at"))

    logo_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as logo_file:
            logo_file.write(base64.b64decode(_CONSCOUT_LOGO_BASE64))
            logo_path = logo_file.name

        pdf = _SafetyDailyPdf(
            logo_path=logo_path,
            report_id=report_id,
            issued_at=issued_at,
        )
        pdf.set_title("Safety and Manpower Daily Report")
        pdf.set_author("ConScout")
        pdf.set_creator("ConScout Safety Workspace")

        pdf.add_page()
        _project_strip(pdf, context=context, report=report)
        _work_state_banner(pdf, state)
        _kpi_cards(pdf, manpower=manpower, ppe=ppe, counts=counts)

        chart_y = pdf.get_y() + 5
        chart_gap = 5.0
        chart_w = (186.0 - chart_gap) / 2
        _workforce_chart(pdf, history, 12, chart_y, chart_w, 62)
        _control_chart(pdf, counts, 12 + chart_w + chart_gap, chart_y, chart_w, 62)

        weather_y = chart_y + 67
        _weather_panel(pdf, weather, 12, weather_y, 186, 42)

        bottom_y = weather_y + 47
        _ppe_panel(pdf, ppe, 12, bottom_y, chart_w, 49)
        _actions_panel(
            pdf,
            state.get("reasons") or [],
            12 + chart_w + chart_gap,
            bottom_y,
            chart_w,
            49,
        )

        pdf.add_page()
        pdf.set_xy(12, 12)
        pdf.set_text_color(*NAVY)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 7, "AUDIT DETAIL & COMPLIANCE REGISTER", ln=1)
        pdf.set_text_color(*SLATE)
        pdf.set_font("Helvetica", "", 7)
        pdf.cell(
            0,
            5,
            "Records included in this revision at the time the daily snapshot was generated.",
            ln=1,
        )

        table_y = 27.0
        table_gap = 5.0
        table_w = (186.0 - table_gap) / 2
        table_h = 62.0
        _draw_record_table(
            pdf,
            title="Open hazards",
            records=[item for item in (recent.get("hazards") or []) if isinstance(item, dict)],
            x=12,
            y=table_y,
            w=table_w,
            h=table_h,
        )
        _draw_record_table(
            pdf,
            title="PPE & visual findings",
            records=[item for item in (recent.get("findings") or []) if isinstance(item, dict)],
            x=12 + table_w + table_gap,
            y=table_y,
            w=table_w,
            h=table_h,
        )
        second_y = table_y + table_h + 5
        _draw_record_table(
            pdf,
            title="Permit-to-Work controls",
            records=[item for item in (recent.get("permits") or []) if isinstance(item, dict)],
            x=12,
            y=second_y,
            w=table_w,
            h=table_h,
        )
        _draw_record_table(
            pdf,
            title="Project inspections",
            records=[item for item in (recent.get("checks") or []) if isinstance(item, dict)],
            x=12 + table_w + table_gap,
            y=second_y,
            w=table_w,
            h=table_h,
        )

        signoff_y = second_y + table_h + 6
        _signoff_panel(pdf, report, 12, signoff_y, 186, 45)
        _data_note(pdf, snapshot, 12, signoff_y + 50, 186, 34)

        output = pdf.output(dest="S")
        return output.encode("latin-1") if isinstance(output, str) else bytes(output)
    finally:
        if logo_path:
            try:
                os.unlink(logo_path)
            except OSError:
                pass
