"""Tests for OCR text extraction from image files."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def white_tif(tmp_path: Path) -> Path:
    """Create a blank white TIF image for basic OCR testing."""
    path = tmp_path / "blank.tif"
    img = Image.new("RGB", (200, 50), color="white")
    img.save(path, format="TIFF")
    return path


class TestIsOcrAvailable:
    def test_returns_bool(self) -> None:
        from famed.ocr import is_ocr_available

        result = is_ocr_available()
        assert isinstance(result, bool)


class TestOcrImageFile:
    def test_returns_string_for_blank_image(self, white_tif: Path) -> None:
        from famed.ocr import ocr_image_file

        text = ocr_image_file(white_tif)
        assert isinstance(text, str)
        # A blank white image may produce empty text or whitespace
        assert text is not None

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        from famed.ocr import ocr_image_file

        with pytest.raises(OSError):
            ocr_image_file(tmp_path / "nonexistent.tif")
