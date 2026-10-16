"""OCR (optical character recognition) support for image-based documents.

Provides text extraction from TIF, TIFF, and other image formats using
Tesseract OCR via pytesseract.  Tesseract must be installed on the system
(``brew install tesseract`` on macOS).

Public surface::

    ocr_image_file(path) → str
    is_ocr_available() → bool
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image


def is_ocr_available() -> bool:
    """Return True when both pytesseract and the tesseract binary are usable."""
    try:
        import pytesseract  # noqa: F401

        pytesseract.get_tesseract_version()  # type: ignore[union-attr]
        return True
    except Exception:
        return False


def ocr_image_file(path: Path) -> str:
    """Extract text from an image file using Tesseract OCR.

    Raises OSError if the file cannot be read.
    Raises ImportError if pytesseract is not installed.
    Raises RuntimeError if the tesseract binary is not found.
    """
    import pytesseract

    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError(
            "Tesseract OCR is not installed or not on PATH. Install with: brew install tesseract"
        ) from exc

    image = Image.open(path)
    try:
        return pytesseract.image_to_string(image)  # type: ignore[no-any-return]
    finally:
        image.close()
