# Config module: reads environment and app settings.
# Single source of paths, defaults, and feature flags.

import os
import re
from typing import Optional

from core.auth_context import get_current_user

# ---------------------------------------------------------
# Base directory
# ---------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
def _env(key: str, default: str) -> str:
    value = os.getenv(key)
    return value if value is not None and value != "" else default

def _env_int(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default

def _env_float(key: str, default: float) -> float:
    value = os.getenv(key)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default

def _env_bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

def _port(default_key: str, default_value: int) -> int:
    port = _env_int("PORT", 0)
    if port > 0:
        return port
    return _env_int(default_key, default_value)

# ---------------------------------------------------------
# Storage paths
# ---------------------------------------------------------
MODEL_DIR = _env("MODEL_DIR", os.path.join(BASE_DIR, "models"))

DATA_DIR = _env("DATA_DIR", os.path.join(BASE_DIR, "data"))
SITES_DIR = _env("SITES_DIR", os.path.join(DATA_DIR, "sites"))
TOURS_DIR = _env("TOURS_DIR", os.path.join(DATA_DIR, "tours"))

SITE_FLOORPLAN_DIRNAME = _env("SITE_FLOORPLAN_DIRNAME", "floorplan")
SITE_DXF_DIRNAME = _env("SITE_DXF_DIRNAME", "dxf")
SITE_BASELINE_DIRNAME = _env("SITE_BASELINE_DIRNAME", "baseline")
DEFAULT_SITE_NAME = _env("DEFAULT_SITE_NAME", "site_unknown")

TOUR_RAW_DIRNAME = _env("TOUR_RAW_DIRNAME", "raw")
TOUR_DETECT_DIRNAME = _env("TOUR_DETECT_DIRNAME", "detect")
TOUR_DETECT_SEG_DIRNAME = _env("TOUR_DETECT_SEG_DIRNAME", "detect+seg")
TOUR_COMMENTS_DIRNAME = _env("TOUR_COMMENTS_DIRNAME", "comments")


def _sanitize_user_segment(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return "anonymous"
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug or "anonymous"


def _owner_storage_segment(
    *,
    owner_email: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> Optional[str]:
    email = (owner_email or "").strip().lower()
    if email:
        username = email.split("@", 1)[0]
        return _sanitize_user_segment(username)

    user_id = (owner_user_id or "").strip()
    if user_id:
        return _sanitize_user_segment(user_id)

    current_user = get_current_user()
    if current_user:
        username = (current_user.email or "").split("@", 1)[0]
        return _sanitize_user_segment(username or current_user.user_id)

    return None


def user_data_dir(*, owner_email: Optional[str] = None, owner_user_id: Optional[str] = None) -> str:
    segment = _owner_storage_segment(owner_email=owner_email, owner_user_id=owner_user_id)
    if not segment:
        return DATA_DIR
    return os.path.join(DATA_DIR, segment)


def user_sites_dir(*, owner_email: Optional[str] = None, owner_user_id: Optional[str] = None) -> str:
    return os.path.join(
        user_data_dir(owner_email=owner_email, owner_user_id=owner_user_id),
        "sites",
    )


def user_tours_dir(*, owner_email: Optional[str] = None, owner_user_id: Optional[str] = None) -> str:
    return os.path.join(
        user_data_dir(owner_email=owner_email, owner_user_id=owner_user_id),
        "tours",
    )


def site_storage_roots(*, owner_email: Optional[str] = None, owner_user_id: Optional[str] = None) -> list[str]:
    roots: list[str] = []
    scoped = user_sites_dir(owner_email=owner_email, owner_user_id=owner_user_id)
    if scoped not in roots:
        roots.append(scoped)
    if SITES_DIR not in roots:
        roots.append(SITES_DIR)
    return roots


def tour_storage_roots(*, owner_email: Optional[str] = None, owner_user_id: Optional[str] = None) -> list[str]:
    roots: list[str] = []
    scoped = user_tours_dir(owner_email=owner_email, owner_user_id=owner_user_id)
    if scoped not in roots:
        roots.append(scoped)
    if TOURS_DIR not in roots:
        roots.append(TOURS_DIR)
    return roots

def site_dir(site_name: str, *, owner_email: Optional[str] = None, owner_user_id: Optional[str] = None) -> str:
    return os.path.join(
        user_sites_dir(owner_email=owner_email, owner_user_id=owner_user_id),
        site_name,
    )

def site_floorplan_dir(site_name: str, *, owner_email: Optional[str] = None, owner_user_id: Optional[str] = None) -> str:
    return os.path.join(
        site_dir(site_name, owner_email=owner_email, owner_user_id=owner_user_id),
        SITE_FLOORPLAN_DIRNAME,
    )

def site_dxf_dir(site_name: str, *, owner_email: Optional[str] = None, owner_user_id: Optional[str] = None) -> str:
    return os.path.join(
        site_dir(site_name, owner_email=owner_email, owner_user_id=owner_user_id),
        SITE_DXF_DIRNAME,
    )

def site_baseline_dir(site_name: str, *, owner_email: Optional[str] = None, owner_user_id: Optional[str] = None) -> str:
    return os.path.join(
        site_dir(site_name, owner_email=owner_email, owner_user_id=owner_user_id),
        SITE_BASELINE_DIRNAME,
    )

def tour_dir(
    tour_id: str,
    *,
    owner_email: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> str:
    suffix = f"__{tour_id}"
    for root in tour_storage_roots(owner_email=owner_email, owner_user_id=owner_user_id):
        direct = os.path.join(root, tour_id)
        if os.path.isdir(direct):
            return direct
        try:
            for entry in os.listdir(root):
                candidate = os.path.join(root, entry)
                if os.path.isdir(candidate) and entry.endswith(suffix):
                    return candidate
        except FileNotFoundError:
            pass
    return os.path.join(
        user_tours_dir(owner_email=owner_email, owner_user_id=owner_user_id),
        tour_id,
    )

def tour_raw_dir(tour_id: str, *, owner_email: Optional[str] = None, owner_user_id: Optional[str] = None) -> str:
    return os.path.join(
        tour_dir(tour_id, owner_email=owner_email, owner_user_id=owner_user_id),
        TOUR_RAW_DIRNAME,
    )

def tour_detect_dir(tour_id: str, *, owner_email: Optional[str] = None, owner_user_id: Optional[str] = None) -> str:
    return os.path.join(
        tour_dir(tour_id, owner_email=owner_email, owner_user_id=owner_user_id),
        TOUR_DETECT_DIRNAME,
    )

def tour_detect_seg_dir(tour_id: str, *, owner_email: Optional[str] = None, owner_user_id: Optional[str] = None) -> str:
    return os.path.join(
        tour_dir(tour_id, owner_email=owner_email, owner_user_id=owner_user_id),
        TOUR_DETECT_SEG_DIRNAME,
    )

def tour_comments_dir(tour_id: str, *, owner_email: Optional[str] = None, owner_user_id: Optional[str] = None) -> str:
    return os.path.join(
        tour_dir(tour_id, owner_email=owner_email, owner_user_id=owner_user_id),
        TOUR_COMMENTS_DIRNAME,
    )

# ---------------------------------------------------------
# Mongo
# ---------------------------------------------------------
MONGO_URI = _env("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = _env("DB_NAME", "construction_ai")

# ---------------------------------------------------------
# CORS
# ---------------------------------------------------------
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in _env("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

# ---------------------------------------------------------
# AI Service
# ---------------------------------------------------------
AI_SERVICE_URL = _env("AI_SERVICE_URL", "http://localhost:8001")
AI_SYNC_TIMEOUT_SECONDS = _env_int("AI_SYNC_TIMEOUT_SECONDS", 300)
AI_PROCESS_TIMEOUT_SECONDS = _env_int("AI_PROCESS_TIMEOUT_SECONDS", 900)

# ---------------------------------------------------------
# AI Inference
# ---------------------------------------------------------
SEG_IMGSZ = _env_int("SEG_IMGSZ", 1280)
SEG_CONF = _env_float("SEG_CONF", 0.25)
SEG_IOU = _env_float("SEG_IOU", 0.7)

COUNT_IMGSZ = _env_int("COUNT_IMGSZ", 1280)
COUNT_CONF = _env_float("COUNT_CONF", 0.25)
COUNT_IOU = _env_float("COUNT_IOU", 0.7)
COUNT_PRE_RESIZE_WIDTH = _env_int("COUNT_PRE_RESIZE_WIDTH", 2048)

AI_DEVICE = _env("AI_DEVICE", "")

# ---------------------------------------------------------
# Feature flags
# ---------------------------------------------------------
ENABLE_DXF_PROCESSING = _env_bool("ENABLE_DXF_PROCESSING", True)

# ---------------------------------------------------------
# Ports
# ---------------------------------------------------------
API_PORT = _port("API_PORT", 8000)
AI_PORT = _port("AI_PORT", 8001)

# ---------------------------------------------------------
# App
# ---------------------------------------------------------
APP_TITLE = _env("APP_TITLE", "Construction Monitor Stitching Service")
APP_VERSION = _env("APP_VERSION", "1.3")

