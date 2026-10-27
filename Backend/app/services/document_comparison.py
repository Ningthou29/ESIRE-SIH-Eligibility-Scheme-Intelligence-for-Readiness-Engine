
# Backend/app/services/document_comparison.py

import re
from decimal import Decimal, InvalidOperation


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def compare_documents(documents: list[dict]) -> dict:
    """
    Expected input:
    [
      {"doc_type": "income_certificate",
       "fields": {"names": ["A Person"], "income_amounts": ["180000"]}},
      ...
    ]
    """
    findings = []

    def collect(field_name: str, normalizer):
        values = []
        for doc in documents:
            fields = doc.get("fields") or {}
            raw_values = fields.get(field_name) or []
            for raw in raw_values:
                if raw is None or not str(raw).strip():
                    continue
                try:
                    normalized = normalizer(str(raw))
                except (ValueError, InvalidOperation):
                    continue
                values.append({
                    "doc_type": doc.get("doc_type", "unknown"),
                    "value": str(raw).strip(),
                    "normalized": normalized,
                })
        return values

    comparisons = [
        ("names", normalize_text, "Potential name mismatch"),
        ("addresses", normalize_text, "Potential address mismatch"),
        (
            "income_amounts",
            lambda value: Decimal(value.replace(",", "").replace("₹", "").strip()),
            "Potential income mismatch",
        ),
        (
            "certificate_numbers",
            lambda value: re.sub(r"[^a-z0-9]", "", value.casefold()),
            "Potential certificate-number mismatch",
        ),
    ]

    for field_name, normalizer, message in comparisons:
        values = collect(field_name, normalizer)
        distinct = {item["normalized"] for item in values}

        if len(distinct) > 1:
            findings.append({
                "field": field_name,
                "message": message,
                "values": [
                    {"doc_type": item["doc_type"], "value": item["value"]}
                    for item in values
                ],
                "severity": "review",
            })

    return {
        "documents_compared": len(documents),
        "findings": findings,
        "status": "flagged_for_review" if findings else "no_mismatch_detected",
        "note": (
            "Differences can arise from OCR errors, spelling variations, "
            "old addresses, or legitimate changes. Human review is required."
        ),
    }
