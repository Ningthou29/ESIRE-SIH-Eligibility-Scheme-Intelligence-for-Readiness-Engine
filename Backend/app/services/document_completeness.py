
# Backend/app/services/document_completeness.py

from app.services.catalog import all_schemes


def assess_completeness(
    required_doc_ids: list[str],
    uploaded_doc_ids: list[str],
) -> dict:
    uploaded = set(uploaded_doc_ids)
    required = set(required_doc_ids)

    missing = sorted(required - uploaded)
    unexpected = sorted(uploaded - required)

    return {
        "complete": not missing,
        "required_count": len(required),
        "uploaded_required_count": len(required & uploaded),
        "missing_document_ids": missing,
        "unexpected_document_ids": unexpected,
    }


def scheme_document_requirements(scheme_id: str) -> dict | None:
    for scheme in all_schemes():
        if scheme.get("id") == scheme_id or scheme.get("scheme_id") == scheme_id:
            documents = scheme.get("documents") or []
            return {
                "scheme_id": scheme_id,
                "required_documents": [
                    {
                        "id": doc["id"],
                        "name": doc.get("name", doc["id"]),
                        "note": doc.get("note", ""),
                    }
                    for doc in documents
                    if doc.get("id")
                ],
            }

    return None
