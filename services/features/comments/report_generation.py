# Report generation: build PDF issue reports.
# Used by the comments service.

import base64
import os
import re
import urllib.request
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

from core.config import (
    DATA_DIR,
    SITE_FLOORPLAN_DIRNAME,
    SITES_DIR,
    TOUR_DETECT_DIRNAME,
    TOUR_DETECT_SEG_DIRNAME,
    TOUR_COMMENTS_DIRNAME,
    TOUR_RAW_DIRNAME,
    TOURS_DIR,
    site_storage_roots,
    tour_storage_roots,
)


def _latin1(text: Optional[str]) -> str:
    safe_text = text if text is not None else "N/A"
    return str(safe_text).encode("latin-1", errors="replace").decode("latin-1")


def _format_timestamp(ms: Optional[int]) -> str:
    if not ms:
        return "N/A"
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "N/A"


def _resolve_local_image_path(url_or_path: Optional[str]) -> Optional[str]:
    if not url_or_path:
        return None

    raw_path = url_or_path.strip()
    if raw_path.startswith("http://") or raw_path.startswith("https://"):
        raw_path = urlparse(raw_path).path

    raw_path = raw_path.replace("\\", "/")

    if raw_path.startswith("/streetview/"):
        rel = raw_path.replace("/streetview/", "").lstrip("/")
        candidate = os.path.join(TOURS_DIR, rel)
        if os.path.exists(candidate):
            return candidate
        parts = rel.split("/", 1)
        if len(parts) == 2 and parts[1].lower().endswith((".jpg", ".jpeg", ".png")):
            tour_id = parts[0]
            filename = parts[1]
            for subdir in (TOUR_RAW_DIRNAME, TOUR_DETECT_DIRNAME, TOUR_DETECT_SEG_DIRNAME):
                alt = os.path.join(TOURS_DIR, tour_id, subdir, filename)
                if os.path.exists(alt):
                    return alt
        return None

    if raw_path.startswith("/sites/"):
        rel = raw_path.replace("/sites/", "").lstrip("/")
        candidate = os.path.join(SITES_DIR, rel)
        return candidate if os.path.exists(candidate) else None

    if raw_path.startswith("/floorplans/"):
        rel = raw_path.replace("/floorplans/", "").lstrip("/")
        candidate = os.path.join(SITES_DIR, rel)
        if os.path.exists(candidate):
            return candidate
        filename = os.path.basename(rel)
        sites_root = os.path.join(SITES_DIR)
        if os.path.isdir(sites_root):
            for site_name in os.listdir(sites_root):
                site_path = os.path.join(sites_root, site_name)
                if not os.path.isdir(site_path):
                    continue
                alt = os.path.join(site_path, SITE_FLOORPLAN_DIRNAME, filename)
                if os.path.exists(alt):
                    return alt
        return None

    if os.path.exists(raw_path):
        return raw_path

    return None


def _all_scoped_storage_roots(kind: str) -> list[str]:
    roots: list[str] = []
    base_root = TOURS_DIR if kind == "tours" else SITES_DIR
    if base_root not in roots:
        roots.append(base_root)
    if os.path.isdir(DATA_DIR):
        try:
            for entry in os.listdir(DATA_DIR):
                user_root = os.path.join(DATA_DIR, entry)
                scoped_root = os.path.join(user_root, kind)
                if os.path.isdir(scoped_root) and scoped_root not in roots:
                    roots.append(scoped_root)
                if kind == "tours":
                    sites_root = os.path.join(user_root, "sites")
                    if not os.path.isdir(sites_root):
                        continue
                    for site_name in os.listdir(sites_root):
                        nested = os.path.join(sites_root, site_name, "tours")
                        if os.path.isdir(nested) and nested not in roots:
                            roots.append(nested)
        except Exception:
            pass
    return roots


def _resolve_local_image_path_for_owner(
    url_or_path: Optional[str],
    *,
    owner_email: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> Optional[str]:
    if not url_or_path:
        return None

    raw_path = url_or_path.strip()
    if raw_path.startswith("http://") or raw_path.startswith("https://"):
        raw_path = urlparse(raw_path).path

    raw_path = raw_path.replace("\\", "/")

    if raw_path.startswith("/streetview/"):
        rel = raw_path.replace("/streetview/", "").lstrip("/").replace("/", os.sep)
        for root in [*tour_storage_roots(owner_email=owner_email, owner_user_id=owner_user_id), *_all_scoped_storage_roots("tours")]:
            candidate = os.path.join(root, rel)
            if os.path.exists(candidate):
                return candidate

        parts = rel.split(os.sep, 1)
        if len(parts) == 2 and parts[1].lower().endswith((".jpg", ".jpeg", ".png")):
            tour_id = parts[0]
            filename = parts[1]
            for root in [*tour_storage_roots(owner_email=owner_email, owner_user_id=owner_user_id), *_all_scoped_storage_roots("tours")]:
                for subdir in (TOUR_RAW_DIRNAME, TOUR_DETECT_DIRNAME, TOUR_DETECT_SEG_DIRNAME):
                    alt = os.path.join(root, tour_id, subdir, filename)
                    if os.path.exists(alt):
                        return alt
                try:
                    for entry in os.listdir(root):
                        candidate_dir = os.path.join(root, entry)
                        if not os.path.isdir(candidate_dir) or not entry.endswith(f"__{tour_id}"):
                            continue
                        for subdir in (TOUR_RAW_DIRNAME, TOUR_DETECT_DIRNAME, TOUR_DETECT_SEG_DIRNAME):
                            alt = os.path.join(candidate_dir, subdir, filename)
                            if os.path.exists(alt):
                                return alt
                except Exception:
                    continue
        return None

    if raw_path.startswith("/sites/"):
        rel = raw_path.replace("/sites/", "").lstrip("/").replace("/", os.sep)
        for root in [*site_storage_roots(owner_email=owner_email, owner_user_id=owner_user_id), *_all_scoped_storage_roots("sites")]:
            candidate = os.path.join(root, rel)
            if os.path.exists(candidate):
                return candidate
        return None

    if raw_path.startswith("/floorplans/"):
        rel = raw_path.replace("/floorplans/", "").lstrip("/").replace("/", os.sep)
        filename = os.path.basename(rel)
        for root in [*site_storage_roots(owner_email=owner_email, owner_user_id=owner_user_id), *_all_scoped_storage_roots("sites")]:
            candidate = os.path.join(root, rel)
            if os.path.exists(candidate):
                return candidate
            if os.path.isdir(root):
                for site_name in os.listdir(root):
                    site_path = os.path.join(root, site_name)
                    if not os.path.isdir(site_path):
                        continue
                    alt = os.path.join(site_path, SITE_FLOORPLAN_DIRNAME, filename)
                    if os.path.exists(alt):
                        return alt
        return None

    if os.path.exists(raw_path):
        return raw_path

    return _resolve_local_image_path(url_or_path)


def _resolve_existing_tour_dir(
    tour_id: str,
    *,
    owner_email: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> str:
    suffix = f"__{tour_id}"
    for root in [*tour_storage_roots(owner_email=owner_email, owner_user_id=owner_user_id), *_all_scoped_storage_roots("tours")]:
        direct = os.path.join(root, tour_id)
        if os.path.isdir(direct):
            return direct
        try:
            for entry in os.listdir(root):
                candidate = os.path.join(root, entry)
                if os.path.isdir(candidate) and entry.endswith(suffix):
                    return candidate
        except Exception:
            continue
    fallback_root = tour_storage_roots(owner_email=owner_email, owner_user_id=owner_user_id)[0]
    return os.path.join(fallback_root, tour_id)


def _sanitize_filename(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", text.strip())
    cleaned = cleaned.strip("_")
    return cleaned or "issue"


def _format_view(issue: dict) -> Optional[str]:
    try:
        yaw = float(issue.get("yaw"))
        pitch = float(issue.get("pitch"))
    except (TypeError, ValueError):
        return None
    return f"Yaw {yaw:.2f}, Pitch {pitch:.2f}"


def _normalize_issue_type(issue: dict) -> str:
    raw = (issue.get("issue_type") or issue.get("reference_type") or issue.get("type") or "").strip().lower()
    if "ncr" in raw:
        return "NCR-type"
    if "rfi" in raw:
        return "RFI-type"
    if "safety" in raw:
        return "Safety"
    if "quality" in raw:
        return "Quality"

    dept = (issue.get("department") or "").strip().lower()
    if "safety" in dept:
        return "Safety"
    if "quality" in dept:
        return "Quality"

    return "Quality"


def _normalize_status(status: Optional[str]) -> str:
    if not status:
        return "Open"
    normalized = str(status).strip().lower()
    if normalized in {"fixed", "resolved"}:
        return "Fixed"
    if normalized in {"verified"}:
        return "Verified"
    if normalized in {"closed"}:
        return "Closed"
    return normalized.title()


def _normalize_verification_status(status: Optional[str]) -> str:
    normalized = _normalize_status(status)
    if normalized in {"Verified", "Closed"}:
        return "Verified"
    return "Not Verified"


def _priority_color(priority: str) -> tuple[int, int, int]:
    text = (priority or "").strip().lower()
    if text.isdigit():
        value = int(text)
        if value <= 1:
            return (200, 0, 0)
        if value == 2:
            return (230, 80, 0)
        if value == 3:
            return (240, 170, 0)
        if value == 4:
            return (60, 140, 0)
        return (90, 90, 90)
    if any(word in text for word in ("critical", "urgent", "high")):
        return (200, 0, 0)
    if "medium" in text:
        return (230, 120, 0)
    if "low" in text:
        return (60, 140, 0)
    return (120, 120, 120)


def _normalize_impact(issue: dict) -> str:
    impact = (issue.get("impact") or issue.get("impact_area") or "").strip()
    if impact:
        return impact
    issue_type = _normalize_issue_type(issue).lower()
    if "safety" in issue_type:
        return "Safety"
    if "quality" in issue_type:
        return "Quality"
    return "Progress"


def _decode_data_url_image(data_url: str, output_dir: str) -> Optional[str]:
    if not data_url:
        return None
    if not data_url.startswith("data:image/"):
        return None
    try:
        header, encoded = data_url.split(",", 1)
        if ";base64" not in header:
            return None
        ext = header.split("/")[1].split(";")[0]
        image_bytes = base64.b64decode(encoded)
    except Exception:
        return None
    os.makedirs(output_dir, exist_ok=True)
    filename = f"attachment_{datetime.now().strftime('%Y%m%d_%H%M%S%f')}.{ext}"
    path = os.path.join(output_dir, filename)
    with open(path, "wb") as handle:
        handle.write(image_bytes)
    return path


def _download_remote_image(url: str, output_dir: str) -> Optional[str]:
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        parsed = urlparse(url)
        ext = os.path.splitext(parsed.path)[1].lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            ext = ".jpg"
        os.makedirs(output_dir, exist_ok=True)
        filename = f"remote_attachment_{datetime.now().strftime('%Y%m%d_%H%M%S%f')}{ext}"
        path = os.path.join(output_dir, filename)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "conscout-report-generator/1.0"},
        )
        with urllib.request.urlopen(request, timeout=10) as response, open(path, "wb") as handle:
            handle.write(response.read())
        return path if os.path.exists(path) else None
    except Exception:
        return None


def _resolve_report_image_path(
    candidate: Optional[str],
    *,
    output_dir: str,
    owner_email: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> Optional[str]:
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    decoded = _decode_data_url_image(candidate, output_dir)
    if decoded:
        return decoded
    resolved = _resolve_local_image_path_for_owner(
        candidate,
        owner_email=owner_email,
        owner_user_id=owner_user_id,
    )
    if resolved:
        return resolved
    return _download_remote_image(candidate, output_dir)


def _resolve_first_attachment_image(
    issue: dict,
    output_dir: str,
    *,
    owner_email: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> Optional[str]:
    attachments = issue.get("image_attachments") or []
    direct_candidates = [
        issue.get("attachment_url"),
        issue.get("attachmentUrl"),
        issue.get("image_url"),
        issue.get("imageUrl"),
        issue.get("evidence_image_url"),
        issue.get("evidenceImageUrl"),
    ]
    raw_visual_evidence = issue.get("visual_evidence") or issue.get("visualEvidence")
    if isinstance(raw_visual_evidence, list):
        direct_candidates.extend(raw_visual_evidence)
    elif raw_visual_evidence:
        direct_candidates.append(raw_visual_evidence)
    raw_attachments = issue.get("attachments")
    if isinstance(raw_attachments, list):
        direct_candidates.extend(raw_attachments)
    elif raw_attachments:
        direct_candidates.append(raw_attachments)

    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            data_url = attachment.get("data_url") or attachment.get("url")
            if not data_url:
                continue
            resolved = _resolve_report_image_path(
                data_url,
                output_dir=output_dir,
                owner_email=owner_email,
                owner_user_id=owner_user_id,
            )
            if resolved:
                return resolved

    for candidate in direct_candidates:
        resolved = _resolve_report_image_path(
            candidate,
            output_dir=output_dir,
            owner_email=owner_email,
            owner_user_id=owner_user_id,
        )
        if resolved:
            return resolved

    return None


def _ensure_png(image_path: str, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(image_path))[0]
    png_path = os.path.join(output_dir, f"tmp_{base_name}.png")

    with Image.open(image_path) as img:
        if img.format and img.format.upper() == "PNG":
            return image_path
        img.convert("RGB").save(png_path, format="PNG")

    return png_path


def _annotate_pano_image(image_path: str, issue: dict, output_dir: str) -> str:
    safe_path = _ensure_png(image_path, output_dir)
    with Image.open(safe_path) as img:
        img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        width, height = img.size

        try:
            yaw = float(issue.get("yaw"))
            pitch = float(issue.get("pitch"))
        except (TypeError, ValueError):
            yaw = None
            pitch = None

        if yaw is not None and pitch is not None:
            x = int(((yaw + 180.0) / 360.0) * width)
            y = int((0.5 - (pitch / 180.0)) * height)
            x = max(0, min(width - 1, x))
            y = max(0, min(height - 1, y))

            radius = max(10, int(min(width, height) * 0.02))
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                outline=(255, 0, 0),
                width=4,
            )
            draw.ellipse(
                (x - 3, y - 3, x + 3, y + 3),
                fill=(255, 0, 0),
            )
            draw.rectangle((x + radius + 6, y - 12, x + radius + 80, y + 8), fill=(255, 255, 255))
            draw.text((x + radius + 10, y - 10), "COMMENT", fill=(220, 0, 0))

        out_path = os.path.join(output_dir, f"annotated_pano_{os.path.basename(safe_path)}")
        img.save(out_path, format="PNG")
        return out_path


def _crop_issue_area(image_path: str, issue: dict, output_dir: str, crop_ratio: float = 0.14) -> Optional[str]:
    safe_path = _ensure_png(image_path, output_dir)
    with Image.open(safe_path) as img:
        img = img.convert("RGB")
        width, height = img.size

        try:
            yaw = float(issue.get("yaw"))
            pitch = float(issue.get("pitch"))
        except (TypeError, ValueError):
            yaw = None
            pitch = None

        if yaw is not None and pitch is not None:
            x = int(((yaw + 180.0) / 360.0) * width)
            y = int((0.5 - (pitch / 180.0)) * height)
        else:
            x, y = width // 2, height // 2

        crop_w = max(120, int(width * crop_ratio))
        crop_h = max(90, int(height * crop_ratio))
        crop_w = min(crop_w, width)
        crop_h = min(crop_h, height)
        half_w = max(1, crop_w // 2)
        half_h = max(1, crop_h // 2)

        left = max(0, x - half_w)
        upper = max(0, y - half_h)
        right = min(width, x + half_w)
        lower = min(height, y + half_h)

        cropped = img.crop((left, upper, right, lower))
        out_path = os.path.join(output_dir, f"crop_issue_{os.path.basename(safe_path)}")
        cropped.save(out_path, format="PNG")
        return out_path


def _annotate_floorplan_image(
    image_path: str,
    node: Optional[dict],
    tour: dict,
    floorplan: Optional[dict],
    output_dir: str,
) -> str:
    safe_path = _ensure_png(image_path, output_dir)
    with Image.open(safe_path) as img:
        img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        width, height = img.size

        bounds = floorplan.get("bounds") if floorplan else None
        bounds_w = float(bounds.get("width")) if bounds and bounds.get("width") else width
        bounds_h = float(bounds.get("height")) if bounds and bounds.get("height") else height

        scale_x = width / bounds_w if bounds_w else 1.0
        scale_y = height / bounds_h if bounds_h else 1.0

        def load_font(size: int) -> ImageFont.ImageFont:
            try:
                return ImageFont.truetype("C:\\Windows\\Fonts\\arial.ttf", size=size)
            except Exception:
                return ImageFont.load_default()

        label_font = load_font(14)
        legend_font = load_font(12)
        legend_title_font = load_font(13)

        # Draw DXF objects (site_objects) as small markers
        site_objects = floorplan.get("site_objects") if floorplan else None
        if isinstance(site_objects, list):
            for obj in site_objects:
                ox = obj.get("x")
                oy = obj.get("y")
                if not isinstance(ox, (int, float)) or not isinstance(oy, (int, float)):
                    continue
                px = int(ox * scale_x)
                py = int(oy * scale_y)
                draw.ellipse((px - 3, py - 3, px + 3, py + 3), fill=(110, 110, 110))

        # Draw all pano nodes (small dots) + highlight current node
        nodes = tour.get("nodes") or []
        for n in nodes:
            nx = n.get("x")
            ny = n.get("y")
            if not isinstance(nx, (int, float)) or not isinstance(ny, (int, float)):
                continue
            px = int(nx * scale_x)
            py = int(ny * scale_y)
            px = max(0, min(width - 1, px))
            py = max(0, min(height - 1, py))
            draw.ellipse((px - 3, py - 3, px + 3, py + 3), fill=(0, 90, 200))

        node_x = node.get("x") if node else None
        node_y = node.get("y") if node else None

        if isinstance(node_x, (int, float)) and isinstance(node_y, (int, float)):
            x = int(node_x * scale_x)
            y = int(node_y * scale_y)
            x = max(0, min(width - 1, x))
            y = max(0, min(height - 1, y))

            coverage = tour.get("coverage") or {}
            radius_px = coverage.get("radius_px")
            if isinstance(radius_px, (int, float)) and radius_px > 0:
                radius = int(radius_px * (scale_x + scale_y) / 2.0)
                draw.ellipse(
                    (x - radius, y - radius, x + radius, y + radius),
                    outline=(0, 128, 255),
                    width=3,
                )

            polygon = coverage.get("polygon") or []
            if polygon:
                points = []
                for pt in polygon:
                    px = pt.get("x")
                    py = pt.get("y")
                    if isinstance(px, (int, float)) and isinstance(py, (int, float)):
                        points.append((px * scale_x, py * scale_y))
                if len(points) >= 3:
                    draw.line(points + [points[0]], fill=(0, 160, 120), width=2)

            # Mark current node (comment location)
            # Strong, high-visibility marker (filled halo + label)
            draw.ellipse((x - 38, y - 38, x + 38, y + 38), fill=(255, 80, 0, 70))
            draw.ellipse((x - 24, y - 24, x + 24, y + 24), fill=(255, 80, 0), outline=(255, 255, 255), width=4)
            draw.ellipse((x - 44, y - 44, x + 44, y + 44), outline=(255, 80, 0), width=4)
            draw.line((x - 16, y, x + 16, y), fill=(255, 255, 255), width=2)
            draw.line((x, y - 16, x, y + 16), fill=(255, 255, 255), width=2)
            draw.rectangle((x + 32, y - 30, x + 190, y + 14), fill=(255, 255, 255))
            draw.text((x + 38, y - 28), "CURRENT NODE", fill=(220, 80, 0), font=label_font)

        legend_x = 12
        legend_y = 12
        draw.rectangle((legend_x, legend_y, legend_x + 240, legend_y + 110), fill=(255, 255, 255))
        draw.text((legend_x + 8, legend_y + 8), "Legend", fill=(40, 40, 40), font=legend_title_font)
        draw.ellipse((legend_x + 8, legend_y + 24, legend_x + 18, legend_y + 34), fill=(255, 80, 0))
        draw.text((legend_x + 26, legend_y + 22), "Current node", fill=(40, 40, 40), font=legend_font)
        draw.ellipse((legend_x + 8, legend_y + 44, legend_x + 18, legend_y + 54), outline=(0, 128, 255), width=2)
        draw.text((legend_x + 26, legend_y + 42), "Coverage radius", fill=(40, 40, 40), font=legend_font)
        draw.ellipse((legend_x + 8, legend_y + 62, legend_x + 14, legend_y + 68), fill=(110, 110, 110))
        draw.text((legend_x + 26, legend_y + 60), "DXF objects", fill=(40, 40, 40), font=legend_font)
        draw.ellipse((legend_x + 8, legend_y + 80, legend_x + 14, legend_y + 86), fill=(0, 90, 200))
        draw.text((legend_x + 26, legend_y + 78), "Pano nodes", fill=(40, 40, 40), font=legend_font)

        out_path = os.path.join(output_dir, f"annotated_floorplan_{os.path.basename(safe_path)}")
        img.save(out_path, format="PNG")
        return out_path


def _add_section_title(pdf: FPDF, title: str) -> None:
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, _latin1(title), ln=True)


def _add_paragraph(pdf: FPDF, text: str) -> None:
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, _latin1(text))


def _add_page_title(pdf: FPDF, title: str, subtitle: Optional[str] = None) -> None:
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 9, _latin1(title), ln=True)
    if subtitle:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 5, _latin1(subtitle), ln=True)
    pdf.ln(1)


def _add_kv_row(pdf: FPDF, label: str, value: str, label_w: int = 56) -> None:
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(label_w, 5, _latin1(label))
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 5, _latin1(value))


def _add_priority_row(pdf: FPDF, label: str, value: str, label_w: int = 56) -> None:
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(label_w, 5, _latin1(label))
    color = _priority_color(value)
    pdf.set_font("Helvetica", "B", 9)
    badge_text = value or "N/A"
    badge_w = pdf.get_string_width(badge_text) + 8
    pdf.set_fill_color(*color)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(badge_w, 5, _latin1(badge_text), border=0, ln=False, fill=True, align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(5)


def _add_caption(pdf: FPDF, text: str) -> None:
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 4, _latin1(text))
    pdf.ln(1)


def _begin_section(pdf: FPDF, title: str) -> float:
    start_y = pdf.get_y()
    pdf.set_fill_color(245, 245, 245)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 7, _latin1(title), ln=True, fill=True)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(2)
    return start_y


def _end_section(pdf: FPDF, start_y: float, pad_bottom: float = 2.0) -> None:
    end_y = pdf.get_y() + pad_bottom
    pdf.set_draw_color(210, 210, 210)
    pdf.rect(
        pdf.l_margin,
        start_y,
        pdf.w - pdf.l_margin - pdf.r_margin,
        end_y - start_y,
    )
    pdf.ln(pad_bottom)


def _add_metadata_table(pdf: FPDF, rows: list[tuple[str, str]]) -> None:
    label_w = 52
    value_w = pdf.w - pdf.l_margin - pdf.r_margin - label_w
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(245, 245, 245)
    for label, value in rows:
        pdf.cell(label_w, 7, _latin1(label), border=1, fill=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(value_w, 7, _latin1(value), border=1)
        pdf.set_font("Helvetica", "B", 9)


def _add_image_block(pdf: FPDF, title: str, image_path: Optional[str], max_height_ratio: float, output_dir: str) -> None:
    _add_section_title(pdf, title)
    if not image_path:
        _add_paragraph(pdf, "Image not available.")
        pdf.ln(2)
        return

    max_width = pdf.w - pdf.l_margin - pdf.r_margin
    max_height = (pdf.h - pdf.b_margin) * max_height_ratio

    width, height = max_width, max_width * 0.6
    try:
        with Image.open(image_path) as img:
            img_w, img_h = img.size
            if img_w > 0 and img_h > 0:
                ratio = img_h / img_w
                width = max_width
                height = width * ratio
    except Exception:
        pass

    if height > max_height:
        scale = max_height / height if height else 1
        width = width * scale
        height = height * scale

    if pdf.get_y() + height > pdf.page_break_trigger:
        pdf.add_page()

    safe_path = _ensure_png(image_path, output_dir)
    pdf.image(safe_path, x=pdf.l_margin, y=pdf.get_y(), w=width)
    pdf.ln(height + 4)


def _add_image_with_caption(
    pdf: FPDF,
    title: str,
    image_path: Optional[str],
    caption: Optional[str],
    max_height_ratio: float,
    output_dir: str,
) -> None:
    if title:
        _add_section_title(pdf, title)
    if not image_path:
        _add_paragraph(pdf, "Image not available.")
        pdf.ln(2)
        return

    max_width = pdf.w - pdf.l_margin - pdf.r_margin
    max_height = (pdf.h - pdf.b_margin) * max_height_ratio

    width, height = max_width, max_width * 0.6
    try:
        with Image.open(image_path) as img:
            img_w, img_h = img.size
            if img_w > 0 and img_h > 0:
                ratio = img_h / img_w
                width = max_width
                height = width * ratio
    except Exception:
        pass

    if height > max_height:
        scale = max_height / height if height else 1
        width = width * scale
        height = height * scale

    if pdf.get_y() + height > pdf.page_break_trigger:
        pdf.add_page()

    safe_path = _ensure_png(image_path, output_dir)
    pdf.image(safe_path, x=pdf.l_margin, y=pdf.get_y(), w=width)
    pdf.ln(height + 1)
    if caption:
        _add_caption(pdf, caption)


def _first_text(*values: Optional[str], default: str = "N/A") -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() != "null":
            return text
    return default


def _format_role(value: Optional[str]) -> str:
    text = _first_text(value, default="N/A")
    return text.replace("_", " ").title()


def _days_open(issue: dict) -> str:
    raw_created = issue.get("created_at")
    if not raw_created:
        return "0"
    try:
        created = datetime.fromtimestamp(int(raw_created) / 1000)
    except Exception:
        return "0"
    return str(max(0, (datetime.now() - created).days))


def _add_report_header(
    pdf: FPDF,
    *,
    report_id: str,
    report_timestamp: str,
) -> None:
    page_w = pdf.w - pdf.l_margin - pdf.r_margin
    left_w = 54
    right_w = 64
    center_w = page_w - left_w - right_w
    y = pdf.get_y()

    mark_x = pdf.l_margin + 3
    mark_y = y + 2
    pdf.set_draw_color(15, 72, 128)
    pdf.set_fill_color(20, 88, 170)
    pdf.rect(mark_x, mark_y, 13, 13, style="F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_xy(mark_x, mark_y + 4)
    pdf.cell(13, 4, "CM", align="C")

    pdf.set_text_color(12, 29, 55)
    pdf.set_font("Helvetica", "B", 7.5)
    pdf.set_xy(pdf.l_margin + 18, y + 3)
    pdf.cell(left_w - 18, 4, _latin1("CONSTRUCTION"), ln=2)
    pdf.cell(left_w - 18, 4, _latin1("MONITOR"))

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_xy(pdf.l_margin + left_w, y + 4)
    pdf.cell(center_w, 7, _latin1("CONSTRUCTION ISSUE REPORT"), align="C")

    pdf.set_draw_color(190, 196, 206)
    pdf.set_text_color(0, 0, 0)
    pdf.set_xy(pdf.l_margin + left_w + center_w, y + 1)
    label_w = 25
    pdf.set_font("Helvetica", "B", 6.8)
    pdf.cell(label_w, 6, _latin1("Report ID"), border=1)
    pdf.set_font("Helvetica", "", 6.8)
    pdf.cell(right_w - label_w, 6, _latin1(report_id), border=1, ln=2)
    pdf.set_x(pdf.l_margin + left_w + center_w)
    pdf.set_font("Helvetica", "B", 6.8)
    pdf.cell(label_w, 6, _latin1("Date Generated"), border=1)
    pdf.set_font("Helvetica", "", 6.8)
    pdf.cell(right_w - label_w, 6, _latin1(report_timestamp), border=1)

    pdf.set_text_color(0, 0, 0)
    pdf.set_y(y + 20)


def _add_info_row(pdf: FPDF, items: list[tuple[str, str]]) -> None:
    available_w = pdf.w - pdf.l_margin - pdf.r_margin
    cell_w = available_w / max(1, len(items))
    start_y = pdf.get_y()
    heights: list[float] = []
    for label, value in items:
        lines = _estimate_line_count(pdf, value, cell_w - 4)
        heights.append(9 + (lines * 4))
    row_h = max(18, max(heights))

    pdf.set_draw_color(215, 220, 230)
    for index, (label, value) in enumerate(items):
        x = pdf.l_margin + (cell_w * index)
        pdf.rect(x, start_y, cell_w, row_h)
        pdf.set_xy(x + 2, start_y + 2)
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_text_color(94, 105, 124)
        pdf.cell(cell_w - 4, 4, _latin1(label.upper()), ln=2)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(25, 32, 45)
        pdf.multi_cell(cell_w - 4, 4, _latin1(value))
    pdf.set_text_color(0, 0, 0)
    pdf.set_y(start_y + row_h + 4)


def _ensure_space(pdf: FPDF, required_h: float) -> None:
    if pdf.get_y() + required_h > pdf.page_break_trigger:
        pdf.add_page()


def _add_card_title(pdf: FPDF, title: str, height: float = 7) -> None:
    pdf.set_fill_color(239, 243, 248)
    pdf.set_draw_color(207, 215, 226)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(30, 41, 59)
    pdf.cell(0, height, _latin1(title.upper()), border=1, ln=True, fill=True)
    pdf.set_text_color(0, 0, 0)


def _estimate_line_count(pdf: FPDF, text: str, width: float) -> int:
    pdf.set_font("Helvetica", "", 9)
    total = 0
    for paragraph in _latin1(text).replace("\r", "").split("\n"):
        words = paragraph.split()
        if not words:
            total += 1
            continue
        lines = 1
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if pdf.get_string_width(candidate) <= width:
                current = candidate
                continue
            lines += 1
            current = word
        total += lines
    return max(1, total)


def _add_text_card(pdf: FPDF, title: str, text: str, min_h: float = 20) -> None:
    usable_w = pdf.w - pdf.l_margin - pdf.r_margin
    lines = _estimate_line_count(pdf, text, usable_w - 6)
    card_h = max(min_h, 8 + (lines * 5))
    _ensure_space(pdf, card_h + 7)
    start_y = pdf.get_y()
    _add_card_title(pdf, title)
    pdf.set_xy(pdf.l_margin, start_y + 7)
    pdf.set_draw_color(207, 215, 226)
    pdf.rect(pdf.l_margin, start_y + 7, usable_w, card_h - 7)
    pdf.set_xy(pdf.l_margin + 3, start_y + 10)
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(usable_w - 6, 5, _latin1(text))
    pdf.set_y(start_y + card_h + 4)


def _add_issue_summary_card(
    pdf: FPDF,
    *,
    issue_title: str,
    issue_type: str,
    severity: str,
    priority: str,
    days_open: str,
) -> None:
    usable_w = pdf.w - pdf.l_margin - pdf.r_margin
    _ensure_space(30)
    y = pdf.get_y()
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(15, 72, 128)
    pdf.cell(0, 5, _latin1("1. ISSUE SUMMARY"), ln=True)
    y = pdf.get_y()
    row_h = 21
    pdf.set_draw_color(207, 215, 226)
    pdf.rect(pdf.l_margin, y, usable_w, row_h)

    icon_w = 18
    x = pdf.l_margin + 3
    pdf.set_fill_color(225, 112, 36)
    pdf.ellipse(x, y + 5, 10, 10, style="F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_xy(x, y + 7)
    pdf.cell(10, 4, "!", align="C")

    items = [
        ("Issue Title", issue_title, 50),
        ("Issue Type", issue_type, 36),
        ("Severity", severity, 32),
        ("Priority", priority, 32),
        ("Days Open", days_open, usable_w - icon_w - 150),
    ]
    x = pdf.l_margin + icon_w
    for index, (label, value, width) in enumerate(items):
        if index > 0:
            pdf.set_draw_color(207, 215, 226)
            pdf.line(x, y + 3, x, y + row_h - 3)
        pdf.set_xy(x + 3, y + 5)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(35, 45, 60)
        pdf.cell(width - 6, 4, _latin1(label), ln=2)
        pdf.set_font("Helvetica", "B", 8.5)
        if label in {"Severity", "Priority"}:
            pdf.set_text_color(225, 112, 36)
        else:
            pdf.set_text_color(12, 29, 55)
        pdf.cell(width - 6, 5, _latin1(value))
        x += width

    pdf.set_text_color(0, 0, 0)
    pdf.set_y(y + row_h + 5)


def _add_two_column_cards(
    pdf: FPDF,
    left: tuple[str, str],
    right: tuple[str, str],
    min_h: float = 22,
) -> None:
    usable_w = pdf.w - pdf.l_margin - pdf.r_margin
    gap = 4
    card_w = (usable_w - gap) / 2
    left_lines = _estimate_line_count(pdf, left[1], card_w - 6)
    right_lines = _estimate_line_count(pdf, right[1], card_w - 6)
    card_h = max(min_h, 8 + (max(left_lines, right_lines) * 5))
    _ensure_space(pdf, card_h + 4)
    y = pdf.get_y()

    for x, (title, text) in ((pdf.l_margin, left), (pdf.l_margin + card_w + gap, right)):
        pdf.set_xy(x, y)
        pdf.set_fill_color(239, 243, 248)
        pdf.set_draw_color(207, 215, 226)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(card_w, 7, _latin1(title.upper()), border=1, ln=True, fill=True)
        pdf.set_xy(x, y + 7)
        pdf.rect(x, y + 7, card_w, card_h - 7)
        pdf.set_xy(x + 3, y + 10)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(card_w - 6, 5, _latin1(text))

    pdf.set_y(y + card_h + 4)


def _image_dims_for_box(image_path: Optional[str], box_w: float, box_h: float) -> tuple[float, float]:
    if not image_path:
        return box_w, box_h
    try:
        with Image.open(image_path) as img:
            img_w, img_h = img.size
            if img_w <= 0 or img_h <= 0:
                return box_w, box_h
            ratio = min(box_w / img_w, box_h / img_h)
            return img_w * ratio, img_h * ratio
    except Exception:
        return box_w, box_h


def _add_visual_evidence_row(
    pdf: FPDF,
    left_image: Optional[str],
    right_image: Optional[str],
    *,
    output_dir: str,
) -> None:
    usable_w = pdf.w - pdf.l_margin - pdf.r_margin
    gap = 4
    card_w = (usable_w - gap) / 2
    title_h = 7
    box_h = 54
    card_h = title_h + box_h + 9
    _ensure_space(card_h + 4)
    y = pdf.get_y()

    for x, title, image_path, caption in (
        (pdf.l_margin, "Visual Evidence", left_image, "360 capture with issue marker"),
        (pdf.l_margin + card_w + gap, "Issue Area", right_image, "Focused view of issue area"),
    ):
        pdf.set_xy(x, y)
        pdf.set_fill_color(239, 243, 248)
        pdf.set_draw_color(207, 215, 226)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(card_w, title_h, _latin1(title.upper()), border=1, ln=True, fill=True)
        pdf.set_xy(x, y + title_h)
        pdf.rect(x, y + title_h, card_w, box_h + 9)
        if image_path:
            safe_path = _ensure_png(image_path, output_dir)
            img_w, img_h = _image_dims_for_box(safe_path, card_w - 6, box_h - 4)
            pdf.image(safe_path, x=x + (card_w - img_w) / 2, y=y + title_h + 2, w=img_w, h=img_h)
        else:
            pdf.set_xy(x + 3, y + title_h + 20)
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(card_w - 6, 5, _latin1("Image not available."), align="C")
        pdf.set_xy(x + 3, y + title_h + box_h + 2)
        pdf.set_font("Helvetica", "I", 7.5)
        pdf.set_text_color(94, 105, 124)
        pdf.cell(card_w - 6, 4, _latin1(caption), align="C")
        pdf.set_text_color(0, 0, 0)

    pdf.set_y(y + card_h + 4)


def _add_location_details(
    pdf: FPDF,
    *,
    issue: dict,
    floorplan_annotated: Optional[str],
    output_dir: str,
) -> None:
    _ensure_space(62)
    usable_w = pdf.w - pdf.l_margin - pdf.r_margin
    y = pdf.get_y()
    _add_card_title(pdf, "Location Details")
    body_y = y + 7
    body_h = 55
    left_w = 72
    pdf.rect(pdf.l_margin, body_y, usable_w, body_h)
    pdf.set_xy(pdf.l_margin + 3, body_y + 4)
    pdf.set_font("Helvetica", "B", 8)
    pdf.cell(28, 5, _latin1("Area"), ln=0)
    pdf.set_font("Helvetica", "", 8)
    pdf.multi_cell(left_w - 31, 5, _latin1(_first_text(issue.get("area"), issue.get("location_area"), issue.get("locationArea"))))
    pdf.set_x(pdf.l_margin + 3)
    pdf.set_font("Helvetica", "B", 8)
    pdf.cell(28, 5, _latin1("Pano ID"), ln=0)
    pdf.set_font("Helvetica", "", 8)
    pdf.multi_cell(left_w - 31, 5, _latin1(_first_text(issue.get("pano_id"))))
    pdf.set_x(pdf.l_margin + 3)
    pdf.set_font("Helvetica", "B", 8)
    pdf.cell(28, 5, _latin1("Orientation"), ln=0)
    pdf.set_font("Helvetica", "", 8)
    pdf.multi_cell(left_w - 31, 5, _latin1(_format_view(issue) or "N/A"))

    img_x = pdf.l_margin + left_w + 3
    img_w = usable_w - left_w - 6
    if floorplan_annotated:
        safe_path = _ensure_png(floorplan_annotated, output_dir)
        draw_w, draw_h = _image_dims_for_box(safe_path, img_w, body_h - 6)
        pdf.image(safe_path, x=img_x + (img_w - draw_w) / 2, y=body_y + 3, w=draw_w, h=draw_h)
    else:
        pdf.set_xy(img_x, body_y + 20)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(img_w, 5, _latin1("Floorplan not available."), align="C")
    pdf.set_y(body_y + body_h + 4)


def _add_activity_log(pdf: FPDF, rows: list[tuple[str, str, str]]) -> None:
    _ensure_space(40)
    _add_card_title(pdf, "Activity Log")
    usable_w = pdf.w - pdf.l_margin - pdf.r_margin
    col_event = 48
    col_user = 60
    col_time = usable_w - col_event - col_user
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(248, 250, 252)
    pdf.cell(col_event, 6, _latin1("Event"), border=1, fill=True)
    pdf.cell(col_user, 6, _latin1("User"), border=1, fill=True)
    pdf.cell(col_time, 6, _latin1("Timestamp"), border=1, fill=True, ln=True)
    pdf.set_font("Helvetica", "", 8)
    for event, user, timestamp in rows:
        pdf.cell(col_event, 6, _latin1(event), border=1)
        pdf.cell(col_user, 6, _latin1(user or "N/A"), border=1)
        pdf.cell(col_time, 6, _latin1(timestamp), border=1, ln=True)
    pdf.ln(4)


def generate_issue_report_pdf(*, issue: dict, tour: dict, node: Optional[dict], floorplan: Optional[dict]) -> str:
    tour_id = tour.get("tour_id") or "tour_unknown"
    output_dir = os.path.join(
        _resolve_existing_tour_dir(
            tour_id,
            owner_email=tour.get("owner_email"),
            owner_user_id=tour.get("owner_user_id"),
        ),
        TOUR_COMMENTS_DIRNAME,
    )
    os.makedirs(output_dir, exist_ok=True)

    issue_name = issue.get("title") or "Comment"
    extraction_time = datetime.now()
    filename = f"{_sanitize_filename(issue_name)}_{extraction_time.strftime('%Y%m%d_%H%M%S')}.pdf"
    pdf_path = os.path.join(output_dir, filename)

    pdf = FPDF("P", "mm", "A4")
    pdf.set_margins(12, 12, 12)
    pdf.set_auto_page_break(auto=True, margin=12)

    created_by = issue.get("created_by") or "N/A"
    created_by_dept = issue.get("created_by_department") or issue.get("department") or "N/A"
    priority = issue.get("priority") or issue.get("severity") or "N/A"
    assigned_to = issue.get("assigned_to") or "N/A"
    issue_id = issue.get("id") or "N/A"
    status = _normalize_status(issue.get("status"))
    issue_type = _normalize_issue_type(issue)
    report_timestamp = extraction_time.strftime("%Y-%m-%d %H:%M")

    pano_url = node.get("detectedImageUrl") if node else None
    if not pano_url and node:
        pano_url = node.get("imageUrl")
    pano_path = _resolve_local_image_path_for_owner(
        pano_url,
        owner_email=tour.get("owner_email"),
        owner_user_id=tour.get("owner_user_id"),
    )
    if not pano_path:
        pano_path = _download_remote_image(pano_url, output_dir)

    floorplan_url = floorplan.get("imageUrl") if floorplan else None
    floorplan_path = _resolve_local_image_path_for_owner(
        floorplan_url,
        owner_email=(floorplan or {}).get("owner_email"),
        owner_user_id=(floorplan or {}).get("owner_user_id"),
    )
    if not floorplan_path:
        floorplan_path = _download_remote_image(floorplan_url, output_dir)

    pano_annotated = _annotate_pano_image(pano_path, issue, output_dir) if pano_path else None
    floorplan_annotated = None
    if floorplan_path:
        floorplan_annotated = _annotate_floorplan_image(floorplan_path, node, tour, floorplan, output_dir)

    pano_crop = None
    if pano_annotated:
        pano_crop = _crop_issue_area(pano_annotated, issue, output_dir)
    elif pano_path:
        pano_crop = _crop_issue_area(pano_path, issue, output_dir)

    attachment_image = _resolve_first_attachment_image(
        issue,
        output_dir,
        owner_email=tour.get("owner_email"),
        owner_user_id=tour.get("owner_user_id"),
    )
    zoomed_issue_image = pano_crop
    if not zoomed_issue_image and attachment_image:
        zoomed_issue_image = _crop_issue_area(attachment_image, issue, output_dir)
    if not zoomed_issue_image and pano_path:
        zoomed_issue_image = _crop_issue_area(pano_path, issue, output_dir)

    project_name = _first_text(
        tour.get("site_name"),
        tour.get("siteName"),
        (floorplan or {}).get("site_name"),
        (floorplan or {}).get("siteName"),
        tour.get("project_name"),
        default="N/A",
    )
    location_name = _first_text(
        issue.get("area"),
        issue.get("location_area"),
        issue.get("locationArea"),
        issue.get("location"),
        default="N/A",
    )
    generated_by = _first_text(
        issue.get("report_generated_by"),
        issue.get("reportGeneratedBy"),
        issue.get("generated_by"),
        issue.get("generatedBy"),
        issue.get("created_by"),
        issue.get("author"),
        created_by,
    )
    company = _first_text(issue.get("company"), issue.get("company_name"), issue.get("companyName"))
    company_role = _format_role(
        issue.get("company_role")
        or issue.get("companyRole")
        or issue.get("stakeholder_role")
        or issue.get("stakeholderRole")
        or issue.get("role")
    )
    schedule = _first_text(
        issue.get("target_completion_date"),
        issue.get("target_completion"),
        issue.get("due_date"),
        issue.get("dueDate"),
        issue.get("completion_date"),
    )
    description = _first_text(
        issue.get("problem_description"),
        issue.get("description"),
        issue.get("comment"),
        default="No issue description provided.",
    )
    action_required = _first_text(
        issue.get("action_required"),
        issue.get("actionRequired"),
        issue.get("action_request"),
        issue.get("actionRequest"),
        issue.get("response"),
    )
    action_taken = _first_text(
        issue.get("action_taken"),
        issue.get("actionTaken"),
        issue.get("action_description"),
        issue.get("actionDescription"),
        issue.get("response"),
        default="No action recorded.",
    )
    action_taken_by = _first_text(
        issue.get("action_taken_by"),
        issue.get("actionTakenBy"),
        issue.get("updated_by"),
        issue.get("updatedBy"),
        issue.get("response_by"),
        assigned_to,
    )
    action_updated_at = issue.get("action_updated_at") or issue.get("actionUpdatedAt") or issue.get("response_at") or issue.get("updated_at")
    closure_notes = _first_text(
        issue.get("verification_notes"),
        issue.get("verification_note"),
        issue.get("notes"),
        issue.get("note"),
    )

    pdf.add_page()
    _add_report_header(pdf, report_id=issue_id, report_timestamp=report_timestamp)
    _add_info_row(
        pdf,
        [
            ("Project Name", project_name),
            ("Location", location_name),
            ("Report Generated By", generated_by),
            ("Status", status),
        ],
    )
    _add_info_row(
        pdf,
        [
            ("Issue ID", issue_id),
            ("Issue Type", issue_type),
            ("Priority", priority),
            ("Discipline", _first_text(issue.get("department"), issue.get("discipline"))),
        ],
    )

    _add_issue_summary_card(
        pdf,
        issue_title=issue_name,
        issue_type=issue_type,
        severity=str(issue.get("severity") or priority),
        priority=str(priority),
        days_open=_days_open(issue),
    )
    _add_text_card(pdf, "Issue Description", description, min_h=26)
    _add_two_column_cards(
        pdf,
        ("Action Required", action_required),
        ("Responsibility", f"Assigned To: {assigned_to}\nCompany: {company}\nRole: {company_role}"),
        min_h=30,
    )
    _add_text_card(pdf, "Schedule", schedule, min_h=18)
    _add_visual_evidence_row(
        pdf,
        pano_annotated or attachment_image,
        zoomed_issue_image,
        output_dir=output_dir,
    )
    _add_location_details(
        pdf,
        issue=issue,
        floorplan_annotated=floorplan_annotated,
        output_dir=output_dir,
    )

    timeline_rows = [
        ("Issue Created", created_by, _format_timestamp(issue.get("created_at"))),
        ("Assigned", assigned_to, _format_timestamp(issue.get("assigned_at"))),
        ("Action Updated", action_taken_by or "N/A", _format_timestamp(action_updated_at)),
        ("Verified", issue.get("verified_by") or "N/A", _format_timestamp(issue.get("verified_at"))),
        ("Closed", issue.get("closed_by") or "N/A", _format_timestamp(issue.get("closed_at"))),
    ]
    _add_two_column_cards(
        pdf,
        (
            "Action & Closure",
            (
                f"Action Taken: {action_taken}\n"
                f"Verification Status: {_normalize_verification_status(issue.get('status'))}\n"
                f"Verified By: {_first_text(issue.get('verified_by'), action_taken_by)}\n"
                f"Verification Date: {_format_timestamp(issue.get('verified_at') or action_updated_at)}"
            ),
        ),
        ("Notes", closure_notes),
        min_h=35,
    )
    _add_activity_log(pdf, timeline_rows)
    _add_text_card(
        pdf,
        "Notes",
        "This report is system-generated from Construction Monitor using time-stamped site imagery and recorded user actions.",
        min_h=18,
    )
    pdf.output(pdf_path)
    return pdf_path
