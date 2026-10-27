
import uuid
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import mongo
from app.db.postgres import get_db
from app.deps import get_current_user
from app.models import User, UserDocument
from app.services.catalog import all_schemes
from app.services.matching import evaluate_user
from app.services.document_screening import MAX_FILE_SIZE, screen_document
from app.services.document_extraction import extract_document
from app.services.document_comparison import compare_documents
from app.services.document_completeness import (
    assess_completeness,
    scheme_document_requirements,
)

router = APIRouter(prefix="/api/documents", tags=["documents"])
settings = get_settings()

# The application accepts only these document formats.
ALLOWED_EXTENSIONS = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}

# Fields allowed when comparing extracted document data.
ALLOWED_COMPARISON_FIELDS = {
    "names",
    "addresses",
    "income_amounts",
    "certificate_numbers",
    "dates",
}


def _catalogue_docs() -> list[dict[str, Any]]:
    """Return a deduplicated list of document types from the scheme catalogue."""
    seen: dict[str, dict[str, Any]] = {}

    for scheme in all_schemes():
        for doc in scheme.get("documents") or []:
            if not isinstance(doc, dict) or not doc.get("id"):
                continue

            seen[doc["id"]] = {
                "id": doc["id"],
                "name": doc.get("name"),
                "note": doc.get("note") or "",
                "proves": doc.get("proves") or [],
            }

    return list(seen.values())


def _allowed_doc_ids() -> set[str]:
    return {item["id"] for item in _catalogue_docs()}


def _get_user_document(
    db: Session,
    user_id: Any,
    doc_type: str,
) -> UserDocument | None:
    """Look up a document belonging to this user only."""
    return (
        db.query(UserDocument)
        .filter(
            UserDocument.user_id == user_id,
            UserDocument.doc_type == doc_type,
        )
        .first()
    )


def _get_upload_extension(detected_type: str) -> str:
    """Return a safe extension based on the detected file type."""
    extension = ALLOWED_EXTENSIONS.get(detected_type)

    if extension is None:
        raise HTTPException(
            status_code=400,
            detail="Unsupported document file type",
        )

    return extension


@router.get("")
def list_documents(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the document types required by the catalogue and their user status."""
    existing = {
        row.doc_type: row
        for row in (
            db.query(UserDocument)
            .filter(UserDocument.user_id == user.id)
            .all()
        )
    }

    items = []

    for spec in _catalogue_docs():
        row = existing.get(spec["id"])

        items.append(
            {
                **spec,
                "status": row.status if row else "missing",
                "original_name": row.original_name if row else "",
                "updated_at": (
                    row.updated_at.isoformat()
                    if row and row.updated_at
                    else None
                ),
            }
        )

    return {"documents": items}


@router.post("/upload")
async def upload_document(
    doc_type: str = Form(...),
    file: UploadFile | None = File(default=None),
    mark_ready: bool = Form(default=True),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Record a document as missing/uploaded, or upload and screen a real file.

    Screening and extraction are automated assistance only. They do not
    establish authenticity or replace review by an authorized person.
    """
    if doc_type not in _allowed_doc_ids():
        raise HTTPException(
            status_code=400,
            detail="Unknown document type",
        )

    # Preserve support for marking a document as missing/uploaded without
    # attaching a file. This does not count as an actual file upload.
    if file is None:
        return set_status(
            doc_type=doc_type,
            status="uploaded" if mark_ready else "missing",
            user=user,
            db=db,
        )

    original_name = Path(file.filename or "document").name

    try:
        data = await file.read(MAX_FILE_SIZE + 1)
    finally:
        await file.close()

    if not data:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is empty",
        )

    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="File exceeds the 10 MB limit",
        )

    # Check the file's actual contents before saving it.
    try:
        report = await run_in_threadpool(
            screen_document,
            data=data,
            original_name=original_name,
            claimed_content_type=file.content_type,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    detected_type = report.get("detected_type")
    extension = _get_upload_extension(detected_type)

    # Generate the stored name ourselves; do not use the submitted filename
    # as the path on disk.
    upload_root = Path(settings.upload_dir) / str(user.id)

    try:
        upload_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not prepare document storage",
        ) from exc

    stored_name = f"{doc_type}_{uuid.uuid4().hex}{extension}"
    dest = upload_root / stored_name

    try:
        await run_in_threadpool(dest.write_bytes, data)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not save uploaded document",
        ) from exc

    # Keep the previous file until the database update succeeds.
    row = _get_user_document(db, user.id, doc_type)
    previous_path = row.stored_path if row else None

    if row is None:
        row = UserDocument(user_id=user.id, doc_type=doc_type)
        db.add(row)

    row.original_name = original_name
    row.stored_path = str(dest)
    row.status = "uploaded"

    # Run text extraction against the detected file type, not the submitted
    # MIME type. The extraction service should return fields, warnings,
    # text_extracted and text_preview.
    try:
        extraction = await run_in_threadpool(
            extract_document,
            data=data,
            content_type=detected_type,
        )
    except (ValueError, OSError) as exc:
        # The upload can still be retained when extraction cannot read it.
        extraction = {
            "fields": {},
            "warnings": [f"Text extraction could not be completed: {exc}"],
            "text_extracted": False,
            "text_preview": "",
        }

    # Store screening separately from the document's review status.
    screening_record = {
        "doc_type": doc_type,
        "status": report.get("status", "screened"),
        "detected_type": detected_type,
        "text_extracted": bool(extraction.get("text_extracted", False)),
        "indicators": report.get("indicators", []),
        "warnings": report.get("warnings", []),
        "automated_decision": report.get("automated_decision"),
        "authenticity_confirmed": False,
        "original_name": original_name,
        "review_required": True,
    }

    extraction_record = {
        "doc_type": doc_type,
        "status": (
            "extracted"
            if extraction.get("text_extracted")
            else "uploaded"
        ),
        "fields": extraction.get("fields", {}),
        "warnings": extraction.get("warnings", []),
        "text_extracted": bool(extraction.get("text_extracted", False)),
        "text_preview": extraction.get("text_preview", ""),
        "review_required": True,
    }

    try:
        mongo.upsert(
            "document_screenings",
            f"screen-{user.id}-{doc_type}",
            screening_record,
        )

        mongo.upsert(
            "extractions",
            f"doc-{user.id}-{doc_type}",
            extraction_record,
        )

        db.commit()
        db.refresh(row)

    except Exception as exc:
        db.rollback()

        # Remove the new file if the record update failed.
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass

        raise HTTPException(
            status_code=500,
            detail="Could not save document records",
        ) from exc

    # Remove the old file only after the replacement record was committed.
    if previous_path and previous_path != str(dest):
        try:
            old_file = Path(previous_path)
            old_file.unlink(missing_ok=True)
        except OSError:
            # The record was saved; an old-file cleanup failure should not
            # invalidate the successful upload.
            pass

    evaluate_user(db, user)

    return {
        "doc_type": doc_type,
        "status": row.status,
        "file_attached": True,
        "screening": screening_record,
        "extraction": extraction_record,
        "message": (
            "Document uploaded for automated screening and extraction. "
            "Extracted information and screening indicators require review."
        ),
    }


@router.post("/{doc_type}/status")
def set_status(
    doc_type: str,
    status: str = Query(default="uploaded"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Let users record missing/uploaded only.

    Verification and invalidation must be performed through a separate,
    authenticated reviewer/admin workflow.
    """
    if doc_type not in _allowed_doc_ids():
        raise HTTPException(
            status_code=400,
            detail="Unknown document type",
        )

    if status not in {"missing", "uploaded"}:
        raise HTTPException(
            status_code=400,
            detail=(
                "Users can set only missing or uploaded. "
                "Verification requires an authorized reviewer."
            ),
        )

    row = _get_user_document(db, user.id, doc_type)

    if status == "missing":
        if row is not None:
            # Delete the row and its stored file, if any.
            stored_path = row.stored_path
            db.delete(row)

            try:
                db.commit()
            except Exception as exc:
                db.rollback()
                raise HTTPException(
                    status_code=500,
                    detail="Could not update document status",
                ) from exc

            if stored_path:
                try:
                    Path(stored_path).unlink(missing_ok=True)
                except OSError:
                    pass

        evaluate_user(db, user)
        return {"doc_type": doc_type, "status": "missing"}

    if row is None:
        row = UserDocument(user_id=user.id, doc_type=doc_type)
        db.add(row)

    # Do not downgrade a previously reviewed document by changing status
    # through this endpoint. Only reviewer workflows should change that state.
    if row.status not in {"verified", "invalid"}:
        row.status = "uploaded"

    try:
        db.commit()
        db.refresh(row)
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Could not update document status",
        ) from exc

    evaluate_user(db, user)

    return {
        "doc_type": doc_type,
        "status": row.status,
        "file_attached": bool(row.stored_path),
    }


@router.get("/assessment/completeness")
def document_completeness(
    scheme_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check which documents for a scheme have actual stored files."""
    requirements = scheme_document_requirements(scheme_id)

    if requirements is None:
        raise HTTPException(
            status_code=404,
            detail="Scheme not found",
        )

    required_ids = [
        doc["id"]
        for doc in requirements["required_documents"]
    ]

    rows = (
        db.query(UserDocument)
        .filter(UserDocument.user_id == user.id)
        .all()
    )

    uploaded_ids = [
        row.doc_type
        for row in rows
        if row.status in {"uploaded", "extracted", "verified"}
        and row.stored_path
        and Path(row.stored_path).is_file()
    ]

    result = assess_completeness(required_ids, uploaded_ids)

    return {
        "scheme_id": scheme_id,
        "required_documents": requirements["required_documents"],
        **result,
    }


@router.post("/assessment/compare")
def compare_extracted_documents(
    documents: list[dict] = Body(...),
    user: User = Depends(get_current_user),
):
    """
    Compare extracted fields supplied in the request.

    This is useful for testing or for a trusted client that loads the user's
    saved extraction data. It does not authenticate field values or load
    stored extraction records itself.
    """
    if len(documents) < 2:
        raise HTTPException(
            status_code=400,
            detail="Provide at least two documents to compare",
        )

    if len(documents) > 20:
        raise HTTPException(
            status_code=400,
            detail="Compare no more than 20 documents at a time",
        )

    cleaned = []

    for item in documents:
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail="Each document must be an object",
            )

        fields = item.get("fields", {})

        if not isinstance(fields, dict):
            raise HTTPException(
                status_code=400,
                detail="Each document's fields must be an object",
            )

        cleaned.append(
            {
                "doc_type": str(item.get("doc_type", "unknown"))[:100],
                "fields": {
                    key: value
                    for key, value in fields.items()
                    if key in ALLOWED_COMPARISON_FIELDS
                    and isinstance(value, list)
                },
            }
        )

    return compare_documents(cleaned)
