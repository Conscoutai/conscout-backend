# normalize floorplan response data for API output.

import os

from core.config import DEFAULT_SITE_NAME, site_dir


def normalize_floorplan(fp: dict) -> dict:
    image_url = fp.get("imageUrl") or ""
    site_name = fp.get("site_name") or fp.get("dxf_project_id") or DEFAULT_SITE_NAME
    project_storage_key = fp.get("id") or fp.get("project_id") or site_name

    if image_url.startswith("/floorplans/"):
        filename = os.path.basename(image_url)
        fp["imageUrl"] = f"/sites/{project_storage_key}/floorplan/{filename}"

    fp["site_config_exists"] = os.path.isfile(
        os.path.join(site_dir(project_storage_key), "site_config.json")
    )
    stakeholder_emails = fp.get("stakeholder_emails")
    if isinstance(stakeholder_emails, list):
        fp["stakeholder_emails"] = [
            str(email).strip().lower()
            for email in stakeholder_emails
            if str(email).strip()
        ]
    else:
        fp["stakeholder_emails"] = []
    if fp.get("capture_mode") not in {"outdoor", "indoor"}:
        fp["capture_mode"] = "outdoor"
    owner_user_id = str(fp.get("owner_user_id") or fp.get("owner_id") or "").strip()
    owner_email = str(
        fp.get("owner_email") or fp.get("created_by_email") or ""
    ).strip().lower()
    owner_name = str(fp.get("owner_name") or fp.get("created_by") or "").strip()
    fp["owner_user_id"] = owner_user_id
    fp["owner_id"] = owner_user_id
    fp["owner_email"] = owner_email
    fp["owner_name"] = owner_name
    fp["created_by_email"] = owner_email
    fp["created_by"] = owner_name
    return fp
