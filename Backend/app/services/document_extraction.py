
# Backend/app/services/document_extraction.py

from datetime import date
from io import BytesIO
import re

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader


MAX_FILE_SIZE = 10 * 1024 * 1024


def extract_document_text(data: bytes, content_type: str) -> tuple[str, list[str]]:
    """Extract text from a PDF or image. OCR requires Tesseract."""
    warnings = []

    if not data:
        raise ValueError("The uploaded file is empty")

    if len(data) > MAX_FILE_SIZE:
        raise ValueError("File exceeds the 10 MB limit")

    if content_type == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError("File content is not a valid PDF signature")

        try:
            reader = PdfReader(BytesIO(data), strict=True)

            if reader.is_encrypted:
                return "", ["PDF is encrypted; text could not be extracted"]

            if len(reader.pages) > 100:
                raise ValueError("PDF exceeds the 100-page limit")

            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            if not text.strip():
                warnings.append("No selectable text found; OCR may be needed")
            return text, warnings

        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Could not read the PDF") from exc

    if content_type not in {"image/png", "image/jpeg"}:
        raise ValueError("Only PDF, PNG, and JPEG files are supported")

    try:
        image = Image.open(BytesIO(data))
        image.verify()
        image = Image.open(BytesIO(data))

        if image.width * image.height > 25_000_000:
            raise ValueError("Image dimensions exceed the allowed limit")

        try:
            import pytesseract
            text = pytesseract.image_to_string(image)
        except ImportError:
            return "", ["OCR unavailable: install pytesseract and Tesseract"]
        except Exception:
            return "", ["OCR failed; check that Tesseract is installed"]

        if not text.strip():
            warnings.append("OCR did not find readable text")

        return text, warnings

    except UnidentifiedImageError as exc:
        raise ValueError("Invalid image file") from exc



import re


def extract_fields(text: str) -> dict:
    """Extract common document fields using conservative patterns.

    Results are suggestions only. They must be reviewed against the
    original document and must not be treated as proof of authenticity.
    """
    fields = {
        "names": [],
        "addresses": [],
        "income_amounts": [],
        "certificate_numbers": [],
        "dates": [],
    }

    if not text or not text.strip():
        return {
            "fields": fields,
            "extraction_method": "text_patterns",
            "review_required": True,
            "note": "No readable text was available for field extraction.",
        }

    # Normalize line endings and remove invisible/control characters
    # while preserving line boundaries used by label-based patterns.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[^\S\n]+", " ", text)

    def clean_values(values):
        """Clean extracted values and remove duplicates."""
        result = []
        seen = set()

        for value in values:
            value = re.sub(r"\s+", " ", value).strip(" \t:;-")

            if not value:
                continue

            key = value.casefold()
            if key not in seen:
                seen.add(key)
                result.append(value)

        return result

    # Labels with values on the same line or immediately below.
    patterns = {
        "names": (
            r"(?im)^\s*(?:applicant\s+name|"
            r"name\s+of\s+(?:the\s+)?applicant|"
            r"full\s+name|name)\s*[:\-]\s*([^\n]{2,100})"
            r"|^\s*(?:applicant\s+name|"
            r"name\s+of\s+(?:the\s+)?applicant|"
            r"full\s+name|name)\s*:\s*\n\s*([^\n]{2,100})"
        ),
        "addresses": (
            r"(?im)^\s*(?:residential\s+address|"
            r"permanent\s+address|present\s+address|"
            r"address)\s*[:\-]\s*([^\n]{3,250})"
        ),
        "certificate_numbers": (
            r"(?im)^\s*(?:certificate\s*(?:no|number|id)|"
            r"cert\s*(?:no|number))\s*[:\-]\s*"
            r"([A-Z0-9/-]+)"
        ),
    }

    for key, pattern in patterns.items():
        matches = re.findall(pattern, text)

        # The name pattern has two capture groups because it supports
        # both same-line and next-line values.
        if key == "names":
            matches = [
                next((group for group in match if group), "")
                if isinstance(match, tuple)
                else match
                for match in matches
            ]

        fields[key] = clean_values(matches)

    # Extract income only when an amount follows an income-related label.
    income_pattern = (
        r"(?i)\b(?:annual\s+income|family\s+income|"
        r"total\s+income|income)\b"
        r"\s*[:\-]?\s*(?:INR|Rs\.?|₹)?\s*"
        r"([\d,]+(?:\.\d{1,2})?)"
    )

    fields["income_amounts"] = clean_values([
        value.replace(",", "")
        for value in re.findall(income_pattern, text)
    ])

    # Support common numeric date formats.
    date_pattern = (
        r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
        r"\d{4}-\d{1,2}-\d{1,2})\b"
    )
    fields["dates"] = clean_values(re.findall(date_pattern, text))

    return {
        "fields": fields,
        "extraction_method": "text_patterns",
        "review_required": True,
        "note": (
            "Automated extraction may miss or misread fields. "
            "Verify all results against the original document. "
            "Extracted values do not establish document authenticity."
        ),
    }



def extract_document(data: bytes, content_type: str) -> dict:
    text, warnings = extract_document_text(data, content_type)
    result = extract_fields(text)
    result["warnings"] = warnings
    result["text_extracted"] = bool(text.strip())
    # Avoid returning or persisting an unlimited amount of raw document text.
    result["text_preview"] = text[:4000]
    return result
