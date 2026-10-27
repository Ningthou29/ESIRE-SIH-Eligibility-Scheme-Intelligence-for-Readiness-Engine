from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db import mongo, neo4j_db
from app.models import SchemeRecord

logger = logging.getLogger("PAANGBA.catalog")

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "seed_schemes.json"


def load_seed_schemes() -> list[dict[str, Any]]:
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


def seed_catalog(db: Session) -> None:
    schemes = load_seed_schemes()
    loaded = 0
    for scheme in schemes:
        try:
            if not isinstance(scheme, dict) or not scheme.get("id") or not scheme.get("name"):
                raise ValueError("scheme is missing a required 'id' or 'name' field")
            scheme_id = scheme["id"]
            mongo.upsert("schemes", scheme_id, scheme)
            record = db.get(SchemeRecord, scheme_id)
            if record is None:
                record = SchemeRecord(
                    id=scheme_id,
                    code=scheme.get("code") or scheme_id,
                    name=scheme["name"],
                    department=scheme.get("department") or "",
                    source=scheme.get("source") or "prototype_seed",
                    is_seed=True,
                )
                db.add(record)
            else:
                record.name = scheme["name"]
                record.department = scheme.get("department") or ""
            _graph_scheme(scheme)
            db.commit()
            loaded += 1
        except Exception as exc:  # noqa: BLE001 - one malformed scheme in the
            # 100-scheme dataset must never prevent the app from starting or
            # the other 99 schemes from loading.
            db.rollback()
            bad_id = scheme.get("id") if isinstance(scheme, dict) else scheme
            logger.error("Skipping malformed scheme during seeding (id=%r): %s", bad_id, exc)
    logger.info("Seeded %d/%d schemes into the catalog", loaded, len(schemes))


def _graph_scheme(scheme: dict[str, Any]) -> None:
    scheme_id = scheme["id"]
    neo4j_db.merge_node("Scheme", scheme_id, {"name": scheme.get("name"), "code": scheme.get("code")})

    for category in scheme.get("categories") or []:
        try:
            neo4j_db.merge_node("Category", category, {"name": category})
            neo4j_db.merge_rel("Scheme", scheme_id, "BELONGS_TO", "Category", category)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping malformed category for scheme %s: %r (%s)", scheme_id, category, exc)

    for group in scheme.get("target_groups") or []:
        try:
            neo4j_db.merge_node("Group", group, {"name": group})
            neo4j_db.merge_rel("Scheme", scheme_id, "TARGETS_GROUP", "Group", group)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping malformed group for scheme %s: %r (%s)", scheme_id, group, exc)

    for location in scheme.get("locations") or []:
        try:
            neo4j_db.merge_node("Location", location, {"name": location})
            neo4j_db.merge_rel("Scheme", scheme_id, "AVAILABLE_IN", "Location", location)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping malformed location for scheme %s: %r (%s)", scheme_id, location, exc)

    for benefit in scheme.get("benefits") or []:
        try:
            neo4j_db.merge_node("Benefit", benefit, {"name": benefit})
            neo4j_db.merge_rel("Scheme", scheme_id, "PROVIDES", "Benefit", benefit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping malformed benefit for scheme %s: %r (%s)", scheme_id, benefit, exc)

    for spec in scheme.get("constraints") or []:
        try:
            neo4j_db.merge_node("Criterion", spec["id"], {"explanation": spec.get("explanation")})
            neo4j_db.merge_rel("Criterion", spec["id"], "APPLIES_TO", "Scheme", scheme_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping malformed constraint for scheme %s: %s", scheme_id, exc)

    for document in scheme.get("documents") or []:
        try:
            neo4j_db.merge_node("DocumentType", document["id"], {"name": document.get("name")})
            neo4j_db.merge_rel("Scheme", scheme_id, "REQUIRES_DOCUMENT", "DocumentType", document["id"])
            for proves in document.get("proves") or []:
                neo4j_db.merge_node("Criterion", proves, {"name": proves})
                neo4j_db.merge_rel("DocumentType", document["id"], "PROVES", "Criterion", proves)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping malformed document for scheme %s: %s", scheme_id, exc)


def all_schemes() -> list[dict[str, Any]]:
    stored = mongo.find_all("schemes")
    if stored:
        cleaned = []
        for item in stored:
            row = dict(item)
            row.pop("_id", None)
            cleaned.append(row)
        return cleaned
    return load_seed_schemes()


def get_scheme(scheme_id: str) -> dict[str, Any] | None:
    row = mongo.find_one("schemes", scheme_id)
    if row:
        row = dict(row)
        row.pop("_id", None)
        return row
    for item in load_seed_schemes():
        if item["id"] == str(scheme_id):
            return item
    return None


def upsert_scheme(db: Session, scheme: dict[str, Any]) -> dict[str, Any]:
    mongo.upsert("schemes", scheme["id"], scheme)
    record = db.get(SchemeRecord, scheme["id"])
    if record is None:
        db.add(
            SchemeRecord(
                id=scheme["id"],
                code=scheme.get("code") or scheme["id"],
                name=scheme["name"],
                department=scheme.get("department") or "",
                source=scheme.get("source") or "admin",
                is_seed=False,
            )
        )
    else:
        record.name = scheme["name"]
        record.department = scheme.get("department") or ""
        record.source = scheme.get("source") or record.source
    _graph_scheme(scheme)
    db.commit()
    return scheme
