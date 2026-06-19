from __future__ import annotations

from pathlib import Path
from typing import IO

from PIL import Image


SAFE_PIL_IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP", "AVIF")


def safe_image_open(source: str | Path | IO[bytes]) -> Image.Image:
    return Image.open(source, formats=SAFE_PIL_IMAGE_FORMATS)
