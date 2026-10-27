
# Backend/app/services/document_screening.py

from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

MAX_FILE_SIZE = 10 * 1024 * 1024

SUPPORTED_TYPES = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}


def detect_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"%PDF-"):
        return "application/pdf", ".pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    raise ValueError("Unsupported file format or invalid file signature")


def screen_document(
    data: bytes,
    original_name: str = "document",
    claimed_content_type: str | None = None,
) -> dict:
    if not data:
        raise ValueError("Uploaded file is empty")
    if len(data) > MAX_FILE_SIZE:
        raise ValueError("File exceeds the 10 MB limit")

    detected_type, extension = detect_type(data)
    indicators = []
    warnings = []

    if claimed_content_type and claimed_content_type not in {
        detected_type,
        "application/octet-stream",
    }:
        indicators.append("Declared MIME type differs from detected file type")

    if detected_type == "application/pdf":
        try:
            reader = PdfReader(BytesIO(data), strict=True)

            if reader.is_encrypted:
                warnings.append("PDF is encrypted; detailed screening was limited")
            else:
                metadata = reader.metadata
                if metadata:
                    creator = str(metadata.get("/Creator") or "").strip()
                    producer = str(metadata.get("/Producer") or "").strip()

                    if not creator and not producer:
                        warnings.append("PDF creation metadata is absent")

                    # Metadata may be edited or removed, so this is not proof.
                    if creator or producer:
                        warnings.append(
                            "PDF metadata is descriptive and is not proof of authenticity"
                        )

            if len(reader.pages) > 100:
                raise ValueError("PDF exceeds the 100-page limit")

        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("PDF could not be parsed") from exc

    else:
        try:
            image = Image.open(BytesIO(data))
            image.verify()
            image = Image.open(BytesIO(data))

            if image.width * image.height > 25_000_000:
                raise ValueError("Image dimensions exceed the allowed limit")

            # EXIF data is contextual only. Its presence or absence cannot
            # establish whether an image has been manipulated.
            exif = image.getexif()
            if not exif:
                warnings.append("No EXIF metadata found; this is not proof of tampering")

        except UnidentifiedImageError as exc:
            raise ValueError("Invalid image file") from exc

    status = "flagged_for_review" if indicators else "screened_no_basic_indicators"

    return {
        "status": status,
        "original_name": Path(original_name).name,
        "detected_type": detected_type,
        "extension": extension,
        "indicators": indicators,
        "warnings": warnings,
        "authenticity_confirmed": False,
        "note": (
            "This is a basic screening result, not a forensic determination. "
            "A clean result does not prove that the document is genuine."
        ),
    }
