# converts project DXF files into usable site objects for your floorplan/tour progress flow.

import ezdxf
import hashlib
from pathlib import Path
from pyproj import Transformer
from typing import List, Dict, Optional

from core.config import site_dxf_dir
from core.site_config import get_dxf_blocks
from core.database import floorplans_collection
from utils.geo import gps_to_xy


class DXFService:
    """
    DXF -> Geo -> Unique -> Floorplan-stitched site objects
    (Offline / truth source)
    """

    def __init__(self):
        self._cache: Dict[str, List[dict]] = {}

        # EPSG:32639 (UTM 39N) -> WGS84
        self.transformer = Transformer.from_crs(
            "EPSG:32639", "EPSG:4326", always_xy=True
        )

    # =========================================================
    # PUBLIC API
    # =========================================================

    def process_project_dxfs(
        self,
        project_id: str,
        floorplan: Optional[Dict] = None,
    ) -> List[dict]:
        """
        Full DXF pipeline for a project (one-time truth ingestion)
        """
        dxf_dir = Path(
            site_dxf_dir(
                project_id,
                owner_email=(floorplan or {}).get("owner_email"),
                owner_user_id=(floorplan or {}).get("owner_user_id"),
            )
        )
        if not dxf_dir.exists():
            raise FileNotFoundError(f"DXF folder not found: {dxf_dir}")

        # 1) Extract ALL site objects from DXF
        raw_objects = self._extract_site_objects(dxf_dir, project_id)

        # 2) Convert UTM -> lat/lon
        geo_objects = self._convert_to_geo(raw_objects)

        # 3) Remove exact duplicates (lat/lon)
        unique_objects = self._remove_exact_duplicates(geo_objects)

        # 4) Stitch lat/lon -> floorplan XY
        stitched = self._stitch_to_floorplan(unique_objects, floorplan)

        # 5) Normalize for Mongo + frontend
        normalized = [
            {
                "id": obj["id"],
                "type": obj["class"],
                "x": obj["x"],
                "y": obj["y"],
                "source": "DXF",
                "verified": False,
                "covered": False,
            }
            for obj in stitched
        ]

        # 6) Cache
        self._cache[project_id] = normalized
        return normalized

    def get_cached_objects(self, project_id: str) -> List[dict]:
        """
        Used by frontend overlays
        """
        return self._cache.get(project_id, [])

    # =========================================================
    # INTERNAL PIPELINE STEPS
    # =========================================================

    def _make_object_id(self, cls: str, x: float, y: float) -> str:
        """
        Stable DXF object ID (class + position hash)
        Ensures SAME ID across re-processing
        """
        key = f"{cls}:{round(x,3)}:{round(y,3)}"
        digest = hashlib.md5(key.encode()).hexdigest()[:8]
        return f"{cls.upper()}_{digest}"

    def _extract_site_objects(self, dxf_dir: Path, site_name: str) -> List[dict]:
        """
        Extract site objects from DXF based on site config
        """
        objects = []

        # Build block -> class lookup
        block_to_class = {}
        dxf_blocks = get_dxf_blocks(site_name)
        for cls, blocks in dxf_blocks.items():
            for block in blocks:
                if not isinstance(block, str):
                    continue
                block_to_class[block.upper()] = cls

        for dxf_file in dxf_dir.glob("*.dxf"):
            try:
                doc = ezdxf.readfile(dxf_file)
                msp = doc.modelspace()
            except Exception:
                continue

            for entity in msp.query("INSERT"):
                block_name = entity.dxf.name.upper()

                # Skip anonymous/system blocks
                if block_name.startswith("*"):
                    continue

                if block_name not in block_to_class:
                    continue

                cls = block_to_class[block_name]
                x, y = entity.dxf.insert.x, entity.dxf.insert.y

                objects.append(
                    {
                        "id": self._make_object_id(cls, x, y),
                        "class": cls,
                        "x": round(x, 3),
                        "y": round(y, 3),
                        "block": block_name,
                        "source_dxf": dxf_file.name,
                    }
                )

        return objects

    def _convert_to_geo(self, objects: List[dict]) -> List[dict]:
        """
        Convert UTM -> lat/lon
        """
        converted = []

        for obj in objects:
            lon, lat = self.transformer.transform(obj["x"], obj["y"])
            converted.append(
                {
                    **obj,
                    "lat": round(lat, 8),
                    "lon": round(lon, 8),
                }
            )

        return converted

    def _remove_exact_duplicates(self, objects: List[dict]) -> List[dict]:
        """
        Remove exact lat/lon duplicates
        """
        seen = set()
        unique = []

        for obj in objects:
            key = (obj["lat"], obj["lon"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(obj)

        return unique

    def _stitch_to_floorplan(
        self,
        objects: List[dict],
        floorplan: Optional[Dict] = None,
    ) -> List[dict]:
        """
        Convert lat/lon -> floorplan XY using latest calibrated floorplan
        """
        stitched = []

        floorplan = floorplan or floorplans_collection.find_one(
            sort=[("_id", -1)]
        )
        if not floorplan:
            return stitched

        for obj in objects:
            x, y = gps_to_xy(obj["lat"], obj["lon"], floorplan)
            stitched.append(
                {
                    **obj,
                    "x": round(x, 2),
                    "y": round(y, 2),
                }
            )

        return stitched

