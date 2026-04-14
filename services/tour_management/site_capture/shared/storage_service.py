import os
import re
from typing import Optional

from core.config import DATA_DIR, tour_storage_roots, user_tours_dir


def sanitize_tour_name(name: Optional[str]) -> str:
    raw = (name or "").strip().lower()
    if not raw:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug[:80]


def build_storage_key(tour_id: str, tour_name: Optional[str]) -> str:
    slug = sanitize_tour_name(tour_name)
    return f"{slug}__{tour_id}" if slug else tour_id


def _site_name_for_tour_doc(tour_doc: Optional[dict]) -> Optional[str]:
    if not tour_doc:
        return None
    for key in ("site_name", "site", "project_id"):
        value = tour_doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _all_tour_storage_roots() -> list[str]:
    roots: list[str] = []
    try:
        for entry in os.listdir(DATA_DIR):
            user_root = os.path.join(DATA_DIR, entry)
            legacy = os.path.join(user_root, "tours")
            if os.path.isdir(legacy) and legacy not in roots:
                roots.append(legacy)
            sites_root = os.path.join(user_root, "sites")
            if not os.path.isdir(sites_root):
                continue
            for site_name in os.listdir(sites_root):
                candidate = os.path.join(sites_root, site_name, "tours")
                if os.path.isdir(candidate) and candidate not in roots:
                    roots.append(candidate)
    except FileNotFoundError:
        pass
    return roots


def resolve_storage_key_for_tour(tour_id: str, tour_doc: Optional[dict] = None) -> str:
    if tour_doc:
        existing = tour_doc.get("storage_key")
        if isinstance(existing, str) and existing.strip():
            return existing.strip()

    suffix = f"__{tour_id}"
    owner_email = (tour_doc or {}).get("owner_email")
    owner_user_id = (tour_doc or {}).get("owner_user_id")
    site_name = _site_name_for_tour_doc(tour_doc)
    for root in [
        *tour_storage_roots(
            owner_email=owner_email,
            owner_user_id=owner_user_id,
            site_name=site_name,
        ),
        *_all_tour_storage_roots(),
    ]:
        try:
            for entry in os.listdir(root):
                candidate = os.path.join(root, entry)
                if os.path.isdir(candidate) and entry.endswith(suffix):
                    return entry
        except FileNotFoundError:
            pass

    return tour_id


def resolve_storage_dir_for_tour(tour_id: str, tour_doc: Optional[dict] = None) -> str:
    owner_email = (tour_doc or {}).get("owner_email")
    owner_user_id = (tour_doc or {}).get("owner_user_id")
    site_name = _site_name_for_tour_doc(tour_doc)
    root = user_tours_dir(
        owner_email=owner_email,
        owner_user_id=owner_user_id,
        site_name=site_name,
    )
    return os.path.join(root, resolve_storage_key_for_tour(tour_id, tour_doc))


def build_streetview_url(storage_key: str, subdir: str, filename: str) -> str:
    return f"/streetview/{storage_key}/{subdir}/{filename}"

