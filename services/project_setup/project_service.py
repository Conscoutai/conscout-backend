"""
Compatibility module for project setup services.

New code should import from focused modules:
- floorplan_normalization_service
- site_config_service
- project_assets_service
- project_lifecycle_service
"""

from .floorplan_normalization_service import normalize_floorplan
from .project_assets_service import (
    persist_project_assets_update,
    replace_site_dxfs_from_zip,
    save_baseline_xer,
)
from .project_lifecycle_service import delete_floorplan_image, delete_project
from .site_config_service import (
    save_site_config_and_try_parse,
    save_site_config_strict,
    upsert_floorplan_site_config,
)

__all__ = [
    "normalize_floorplan",
    "save_site_config_and_try_parse",
    "save_site_config_strict",
    "replace_site_dxfs_from_zip",
    "save_baseline_xer",
    "upsert_floorplan_site_config",
    "persist_project_assets_update",
    "delete_floorplan_image",
    "delete_project",
]
