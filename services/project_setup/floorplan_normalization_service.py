# normalize floorplan response data for API output.

import os

from core.config import DEFAULT_SITE_NAME, site_dir


def normalize_floorplan(fp: dict) -> dict:
    image_url = fp.get("imageUrl") or ""
    site_name = fp.get("site_name") or fp.get("dxf_project_id") or DEFAULT_SITE_NAME

    if image_url.startswith("/floorplans/"):
        filename = os.path.basename(image_url)
        fp["imageUrl"] = f"/sites/{site_name}/floorplan/{filename}"

    fp["site_config_exists"] = os.path.isfile(
        os.path.join(site_dir(site_name), "site_config.json")
    )
    if fp.get("capture_mode") not in {"outdoor", "indoor"}:
        fp["capture_mode"] = "outdoor"
    return fp
