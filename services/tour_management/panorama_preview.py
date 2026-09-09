"""Cached display images; source evidence files are never modified."""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from pathlib import Path

from PIL import Image, ImageOps

_generation_slots = threading.Semaphore(2)


def panorama_preview(source: str, cache_dir: str, width: int) -> str:
    if width not in (2048, 4096):
        raise ValueError("Unsupported panorama display width")
    original = Path(source).resolve(strict=True)
    stat = original.stat()
    fingerprint = f"v1:{original}:{stat.st_mtime_ns}:{stat.st_size}:{width}"
    key = hashlib.sha256(fingerprint.encode()).hexdigest()
    destination = Path(cache_dir) / f"{key}.jpg"
    if destination.is_file():
        return str(destination)
    with _generation_slots:
        if destination.is_file():
            return str(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with Image.open(original) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((width, width // 2), Image.Resampling.LANCZOS)
                image = image.convert("RGB")
                with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".jpg", delete=False) as output:
                    temporary = output.name
                    image.save(output, format="JPEG", quality=88, optimize=True)
            os.replace(temporary, destination)
            temporary = None
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
    return str(destination)
