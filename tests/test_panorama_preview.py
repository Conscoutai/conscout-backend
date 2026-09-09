import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from services.tour_management.panorama_preview import panorama_preview


class PanoramaPreviewTests(unittest.TestCase):
    def test_bounded_cached_preview_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "evidence.png"
            Image.new("RGB", (5888, 2944), "blue").save(source)
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            preview = Path(panorama_preview(str(source), str(root / "cache"), 4096))
            with Image.open(preview) as image:
                self.assertEqual(image.size, (4096, 2048))
                self.assertEqual(image.format, "JPEG")
            generated_at = preview.stat().st_mtime_ns
            self.assertEqual(panorama_preview(str(source), str(root / "cache"), 4096), str(preview))
            self.assertEqual(preview.stat().st_mtime_ns, generated_at)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            Image.new("RGB", (2048, 1024), "red").save(source)
            refreshed = panorama_preview(str(source), str(root / "cache"), 4096)
            self.assertNotEqual(refreshed, str(preview))
            with Image.open(refreshed) as image:
                self.assertEqual(image.size, (2048, 1024))

    def test_rejects_unbounded_width_and_invalid_images(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.txt"
            source.write_text("not an image")
            with self.assertRaises(ValueError):
                panorama_preview(str(source), directory, 16384)
            with self.assertRaises(OSError):
                panorama_preview(str(source), directory, 4096)
            self.assertFalse(list(Path(directory).glob("*.jpg")))


if __name__ == "__main__":
    unittest.main()
