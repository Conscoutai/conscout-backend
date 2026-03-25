import os
import re
from typing import Optional

from core.config import tour_storage_roots, user_tours_dir


def sanitize_tour_name(name: Optional[str]) -> str:
    raw = (name or "").strip().lower()
    if not raw:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug[:80]


def build_storage_key(tour_id: str, tour_name: Optional[str]) -> str:
    slug = sanitize_tour_name(tour_name)
    return f"{slug}__{tour_id}" if slug else tour_id


def resolve_storage_key_for_tour(tour_id: str, tour_doc: Optional[dict] = None) -> str:
    if tour_doc:
        existing = tour_doc.get("storage_key")
        if isinstance(existing, str) and existing.strip():
            return existing.strip()

    suffix = f"__{tour_id}"
    owner_email = (tour_doc or {}).get("owner_email")
    owner_user_id = (tour_doc or {}).get("owner_user_id")
    for root in tour_storage_roots(owner_email=owner_email, owner_user_id=owner_user_id):
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
    root = user_tours_dir(owner_email=owner_email, owner_user_id=owner_user_id)
    return os.path.join(root, resolve_storage_key_for_tour(tour_id, tour_doc))


def build_streetview_url(storage_key: str, subdir: str, filename: str) -> str:
    return f"/streetview/{storage_key}/{subdir}/{filename}"

