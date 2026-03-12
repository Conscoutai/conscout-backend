# AI label mappings: class names and colors.
# Shared helpers for AI outputs.

from typing import Optional

from core.site_config import get_ai_class_aliases


def normalize_ai_class(name: str, site_name: Optional[str] = None) -> str:
    aliases = get_ai_class_aliases(site_name)
    return aliases.get(name, name)
