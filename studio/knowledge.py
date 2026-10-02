"""Provider-neutral knowledge ingestion and retrieval for Raneen.

Raw files remain in their source repository (for example Google Drive). This module
stores immutable document versions, extracted units, facts, chunk text and visual
asset references with provenance. It deliberately does not copy multi-GB source
files onto the small staging disk and does not couple retrieval to a vector vendor.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, Header, HTTPException
from pydantic import Field, model_validator

from .app import StrictModel, now, token_hash, uid


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w\s-]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def bounded_json(value: Any, limit: int = 12000) -> str:
    raw = canonical(value)
    if len(raw.encode("utf-8")) > limit:
        raise HTTPException(422, "Knowledge metadata is too large.")
    return raw


def apply_migration(store) -> None:
    migration = Path(__file__).parent / "sql" / "013_knowledge.sql"
    raw = migration.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS knowledge_schema_versions("
            "version INTEGER PRIMARY KEY, sha256 TEXT NOT NULL, applied TEXT NOT NULL)"
        )
        previous = db.execute(
            "SELECT sha256 FROM knowledge_schema_versions WHERE version=13"
        ).fetchone()
        if previous and previous["sha256"] != digest:
            raise RuntimeError("The applied knowledge migration checksum changed.")
        if previous:
            return
        for statement in raw.decode("utf-8").split(";"):
            if statement.strip():
                db.execute(statement)
        db.execute(
            "INSERT INTO knowledge_schema_versions VALUES(?,?,?)",
            (13, digest, now()),
        )


class DocumentInput(StrictModel):
    source_type: Literal["google_drive", "manual", "s3", "other"]
    external_id: str = Field(min_length=1, max_length=500)
    version_key: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=500)
    parent_external_id: str | None = Field(default=None, max_length=500)
    source_uri: str | None = Field(default=None, max_length=2000)
    mime_type: str | None = Field(default=None, max_length=200)
    size_bytes: int | None = Field(default=None, ge=0)
    modified_at: datetime | None = None
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_metadata(self):
        bounded_json(self.metadata)
        return self


class UnitInput(StrictModel):
    document_version_id: str = Field(min_length=1, max_length=32)
    unit_index: int = Field(ge=0)
    unit_type: Literal["page", "slide", "sheet", "section", "table", "image", "other"]
    label: str | None = Field(default=None, max_length=500)
    text_content: str = Field(default="", max_length=24000)
    visual_summary: str = Field(default="", max_length=8000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_metadata(self):
        bounded_json(self.metadata)
        return self


class EntityInput(StrictModel):
    entity_type: Literal[
        "developer", "project", "community", "property", "unit_type", "policy",
        "market", "agency", "document_subject", "other"
    ]
    canonical_name: str = Field(min_length=1, max_length=500)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload(self):
        bounded_json(self.metadata)
        for alias in self.aliases:
            if not alias.strip() or len(alias) > 500:
                raise ValueError("Aliases must be non-empty and at most 500 characters.")
        return self


class FactInput(StrictModel):
    entity_id: str = Field(min_length=1, max_length=32)
    field_key: str = Field(min_length=1, max_length=180, pattern=r"^[a-z0-9_.-]+$")
    value: Any
    normalized_value: str | None = Field(default=None, max_length=2000)
    document_version_id: str = Field(min_length=1, max_length=32)
    unit_id: str | None = Field(default=None, max_length=32)
    authority_rank: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0.0, le=1.0)
    effective_at: datetime | None = None
    observed_at: datetime | None = None
    supersedes_fact_id: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_value(self):
        bounded_json(self.value, 8000)
        return self


class ChunkInput(StrictModel):
    document_version_id: str = Field(min_length=1, max_length=32)
    unit_id: str | None = Field(default=None, max_length=32)
    chunk_index: int = Field(ge=0)
    text_content: str = Field(min_length=1, max_length=12000)
    token_estimate: int | None = Field(default=None, ge=0, le=20000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_metadata(self):
        bounded_json(self.metadata)
        return self


class AssetInput(StrictModel):
    document_version_id: str = Field(min_length=1, max_length=32)
    unit_id: str | None = Field(default=None, max_length=32)
    asset_type: Literal[
        "decorative", "property_render", "floor_plan", "location_map",
        "master_plan", "payment_plan", "pricing_table", "chart", "amenity", "other"
    ]
    source_locator: str = Field(min_length=1, max_length=2000)
    summary: str = Field(default="", max_length=8000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_metadata(self):
        bounded_json(self.metadata)
        return self


class RelationInput(StrictModel):
    from_document_id: str = Field(min_length=1, max_length=32)
    to_document_id: str = Field(min_length=1, max_length=32)
    relation_type: Literal["duplicate_of", "supersedes", "related_to"]
    reason: str = Field(default="", max_length=3000)


class RunInput(StrictModel):
    source_type: Literal["google_drive", "manual", "s3", "other"]
    root_external_id: str | None = Field(default=None, max_length=500)


class RunUpdate(StrictModel):
    status: Literal["running", "completed", "failed", "cancelled"]
    cursor: str | None = Field(default=None, max_length=2000)
    counts: dict[str, int] = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=3000)

    @model_validator(mode="after")
    def validate_counts(self):
        if len(self.counts) > 50 or any(v < 0 for v in self.counts.values()):
            raise ValueError("Invalid ingestion counts.")
        return self


class KnowledgeProvider:
    def search(self, query: str, constraints: dict[str, Any] | None = None, limit: int = 10):
        raise NotImplementedError

    def get_entity(self, entity_id: str):
        raise NotImplementedError

    def get_current_fact(self, entity_id: str, field_key: str):
        raise NotImplementedError

    def provenance(self, fact_id: str):
        raise NotImplementedError


class SqliteKnowledgeProvider(KnowledgeProvider):
    def __init__(self, store):
        self.store = store

    def _version(self, version_id: str, current_only: bool = False):
        row = self.store.one(
            "SELECT * FROM knowledge_document_versions WHERE id=?", (version_id,)
        )
        if not row:
            raise HTTPException(404, "Knowledge document version not found.")
        if current_only and row["status"] != "current":
            raise HTTPException(409, "Only the current document version accepts new extracted data.")
        return row

    def register_document(self, body: DocumentInput):
        stamp = now()
        metadata = bounded_json(body.metadata)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            document = db.execute(
                "SELECT * FROM knowledge_documents WHERE source_type=? AND external_id=?",
                (body.source_type, body.external_id),
            ).fetchone()
            if not document:
                document_id = uid()
                db.execute(
                    "INSERT INTO knowledge_documents("
                    "id,source_type,external_id,parent_external_id,name,source_uri,current_version_id,"
                    "metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,NULL,?,?,?)",
                    (
                        document_id, body.source_type, body.external_id, body.parent_external_id,
                        body.name, body.source_uri, metadata, stamp, stamp,
                    ),
                )
                current_version_id = None
            else:
                document_id = document["id"]
                current_version_id = document["current_version_id"]
                db.execute(
                    "UPDATE knowledge_documents SET parent_external_id=?,name=?,source_uri=?,"
                    "metadata_json=?,updated_at=? WHERE id=?",
                    (
                        body.parent_external_id, body.name, body.source_uri,
                        metadata, stamp, document_id,
                    ),
                )
            existing = db.execute(
                "SELECT * FROM knowledge_document_versions WHERE document_id=? AND version_key=?",
                (document_id, body.version_key),
            ).fetchone()
            if existing:
                expected_modified = body.modified_at.isoformat() if body.modified_at else None
                expected_sha = body.content_sha256.lower() if body.content_sha256 else None
                same_version = (
                    existing["mime_type"] == body.mime_type
                    and existing["size_bytes"] == body.size_bytes
                    and existing["modified_at"] == expected_modified
                    and existing["content_sha256"] == expected_sha
                    and existing["metadata_json"] == metadata
                )
                if not same_version:
                    raise HTTPException(
                        409,
                        "Source version key was reused with different immutable version metadata.",
                    )
                return {
                    "document_id": document_id,
                    "document_version_id": existing["id"],
                    "status": existing["status"],
                    "idempotent": True,
                }
            if current_version_id:
                db.execute(
                    "UPDATE knowledge_document_versions SET status='superseded' "
                    "WHERE id=? AND status='current'", (current_version_id,)
                )
                db.execute(
                    "UPDATE knowledge_facts SET state='superseded' "
                    "WHERE document_version_id=? AND state='active'", (current_version_id,)
                )
            version_id = uid()
            db.execute(
                "INSERT INTO knowledge_document_versions("
                "id,document_id,version_key,mime_type,size_bytes,modified_at,content_sha256,"
                "status,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, document_id, body.version_key, body.mime_type, body.size_bytes,
                    body.modified_at.isoformat() if body.modified_at else None,
                    body.content_sha256.lower() if body.content_sha256 else None,
                    "current", metadata, stamp,
                ),
            )
            db.execute(
                "UPDATE knowledge_documents SET current_version_id=?,updated_at=? WHERE id=?",
                (version_id, stamp, document_id),
            )
        return {
            "document_id": document_id,
            "document_version_id": version_id,
            "status": "current",
            "idempotent": False,
        }

    def add_unit(self, body: UnitInput):
        self._version(body.document_version_id, current_only=True)
        existing = self.store.one(
            "SELECT * FROM knowledge_units WHERE document_version_id=? AND unit_type=? AND unit_index=?",
            (body.document_version_id, body.unit_type, body.unit_index),
        )
        text_hash = hashlib.sha256(
            (body.text_content + "\n" + body.visual_summary).encode("utf-8")
        ).hexdigest()
        metadata = bounded_json(body.metadata)
        if existing:
            same = (
                existing["label"] == body.label
                and existing["text_content"] == body.text_content
                and existing["visual_summary"] == body.visual_summary
                and existing["metadata_json"] == metadata
            )
            if not same:
                raise HTTPException(409, "This immutable knowledge unit already exists with different content.")
            return {"id": existing["id"], "idempotent": True}
        ident = uid()
        self.store.execute(
            "INSERT INTO knowledge_units("
            "id,document_version_id,unit_index,unit_type,label,text_content,visual_summary,"
            "text_sha256,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                ident, body.document_version_id, body.unit_index, body.unit_type, body.label,
                body.text_content, body.visual_summary, text_hash, metadata, now(),
            ),
        )
        return {"id": ident, "idempotent": False}

    def upsert_entity(self, body: EntityInput):
        norm = normalized_text(body.canonical_name)
        if not norm:
            raise HTTPException(422, "Entity name has no searchable characters.")
        existing = self.store.one(
            "SELECT * FROM knowledge_entities WHERE entity_type=? AND normalized_name=?",
            (body.entity_type, norm),
        )
        stamp = now()
        if existing:
            ident = existing["id"]
        else:
            ident = uid()
            self.store.execute(
                "INSERT INTO knowledge_entities VALUES(?,?,?,?,?,?,?)",
                (
                    ident, body.entity_type, body.canonical_name, norm,
                    bounded_json(body.metadata), stamp, stamp,
                ),
            )
        aliases = {body.canonical_name, *body.aliases}
        for alias in aliases:
            normalized_alias = normalized_text(alias)
            if normalized_alias:
                self.store.execute(
                    "INSERT OR IGNORE INTO knowledge_aliases VALUES(?,?,?,?)",
                    (ident, alias, normalized_alias, stamp),
                )
        return {"id": ident, "created": not bool(existing)}

    def add_fact(self, body: FactInput):
        self._version(body.document_version_id, current_only=True)
        entity = self.store.one("SELECT id FROM knowledge_entities WHERE id=?", (body.entity_id,))
        if not entity:
            raise HTTPException(404, "Knowledge entity not found.")
        if body.unit_id:
            unit = self.store.one(
                "SELECT id,document_version_id FROM knowledge_units WHERE id=?", (body.unit_id,)
            )
            if not unit or unit["document_version_id"] != body.document_version_id:
                raise HTTPException(422, "Fact unit must belong to the same document version.")
        value_json = canonical(body.value)
        normalized = body.normalized_value
        if normalized is None and isinstance(body.value, (str, int, float, bool)):
            normalized = normalized_text(str(body.value))
        existing = self.store.one(
            "SELECT id FROM knowledge_facts WHERE entity_id=? AND field_key=? AND value_json=? "
            "AND document_version_id=? AND COALESCE(unit_id,'')=COALESCE(?, '') AND state='active'",
            (body.entity_id, body.field_key, value_json, body.document_version_id, body.unit_id),
        )
        if existing:
            return {"id": existing["id"], "idempotent": True}
        if body.supersedes_fact_id:
            prior = self.store.one(
                "SELECT entity_id,field_key,state FROM knowledge_facts WHERE id=?",
                (body.supersedes_fact_id,),
            )
            if not prior:
                raise HTTPException(404, "Superseded fact not found.")
            if prior["entity_id"] != body.entity_id or prior["field_key"] != body.field_key:
                raise HTTPException(422, "A fact can supersede only the same entity field.")
        ident = uid()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if body.supersedes_fact_id:
                db.execute(
                    "UPDATE knowledge_facts SET state='superseded' WHERE id=? AND state='active'",
                    (body.supersedes_fact_id,),
                )
            db.execute(
                "INSERT INTO knowledge_facts("
                "id,entity_id,field_key,value_json,normalized_value,document_version_id,unit_id,"
                "authority_rank,confidence,effective_at,observed_at,state,supersedes_fact_id,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ident, body.entity_id, body.field_key, value_json, normalized,
                    body.document_version_id, body.unit_id, body.authority_rank, body.confidence,
                    body.effective_at.isoformat() if body.effective_at else None,
                    body.observed_at.isoformat() if body.observed_at else None,
                    "active", body.supersedes_fact_id, now(),
                ),
            )
        return {"id": ident, "idempotent": False}

    def add_chunk(self, body: ChunkInput):
        self._version(body.document_version_id, current_only=True)
        if body.unit_id:
            unit = self.store.one(
                "SELECT document_version_id FROM knowledge_units WHERE id=?", (body.unit_id,)
            )
            if not unit or unit["document_version_id"] != body.document_version_id:
                raise HTTPException(422, "Chunk unit must belong to the same document version.")
        existing = self.store.one(
            "SELECT * FROM knowledge_chunks WHERE document_version_id=? "
            "AND COALESCE(unit_id,'')=COALESCE(?, '') AND chunk_index=?",
            (body.document_version_id, body.unit_id, body.chunk_index),
        )
        metadata = bounded_json(body.metadata)
        if existing:
            if existing["text_content"] != body.text_content or existing["metadata_json"] != metadata:
                raise HTTPException(409, "This immutable knowledge chunk already exists with different content.")
            return {"id": existing["id"], "idempotent": True}
        ident = uid()
        self.store.execute(
            "INSERT INTO knowledge_chunks VALUES(?,?,?,?,?,?,?,?)",
            (
                ident, body.document_version_id, body.unit_id, body.chunk_index,
                body.text_content, body.token_estimate, metadata, now(),
            ),
        )
        return {"id": ident, "idempotent": False}

    def add_asset(self, body: AssetInput):
        self._version(body.document_version_id, current_only=True)
        if body.unit_id:
            unit = self.store.one(
                "SELECT document_version_id FROM knowledge_units WHERE id=?", (body.unit_id,)
            )
            if not unit or unit["document_version_id"] != body.document_version_id:
                raise HTTPException(422, "Asset unit must belong to the same document version.")
        metadata = bounded_json(body.metadata)
        existing = self.store.one(
            "SELECT * FROM knowledge_assets WHERE document_version_id=? "
            "AND COALESCE(unit_id,'')=COALESCE(?, '') AND asset_type=? AND source_locator=?",
            (body.document_version_id, body.unit_id, body.asset_type, body.source_locator),
        )
        if existing:
            if existing["summary"] != body.summary or existing["metadata_json"] != metadata:
                raise HTTPException(
                    409,
                    "This immutable visual asset locator already exists with different content.",
                )
            return {"id": existing["id"], "idempotent": True}
        ident = uid()
        self.store.execute(
            "INSERT INTO knowledge_assets VALUES(?,?,?,?,?,?,?,?)",
            (
                ident, body.document_version_id, body.unit_id, body.asset_type,
                body.source_locator, body.summary, metadata, now(),
            ),
        )
        return {"id": ident, "idempotent": False}

    def add_relation(self, body: RelationInput):
        if body.from_document_id == body.to_document_id:
            raise HTTPException(422, "A document cannot relate to itself.")
        for ident in (body.from_document_id, body.to_document_id):
            if not self.store.one("SELECT id FROM knowledge_documents WHERE id=?", (ident,)):
                raise HTTPException(404, "Knowledge document not found.")
        existing = self.store.one(
            "SELECT id FROM knowledge_document_relations WHERE from_document_id=? "
            "AND to_document_id=? AND relation_type=?",
            (body.from_document_id, body.to_document_id, body.relation_type),
        )
        if existing:
            return {"id": existing["id"], "idempotent": True}
        ident = uid()
        self.store.execute(
            "INSERT INTO knowledge_document_relations VALUES(?,?,?,?,?,?)",
            (
                ident, body.from_document_id, body.to_document_id,
                body.relation_type, body.reason, now(),
            ),
        )
        return {"id": ident, "idempotent": False}

    def get_entity(self, entity_id: str):
        entity = self.store.one("SELECT * FROM knowledge_entities WHERE id=?", (entity_id,))
        if not entity:
            raise HTTPException(404, "Knowledge entity not found.")
        entity["metadata"] = json.loads(entity.pop("metadata_json"))
        entity["aliases"] = [
            r["alias"] for r in self.store.all(
                "SELECT alias FROM knowledge_aliases WHERE entity_id=? ORDER BY alias",
                (entity_id,),
            )
        ]
        return entity

    def get_current_fact(self, entity_id: str, field_key: str):
        rows = self.store.all(
            "SELECT * FROM knowledge_facts WHERE entity_id=? AND field_key=? AND state='active' "
            "ORDER BY authority_rank DESC, COALESCE(effective_at,observed_at,created_at) DESC, "
            "confidence DESC, created_at DESC",
            (entity_id, field_key),
        )
        if not rows:
            return {"selected": None, "conflict": False, "candidates": []}
        top_rank = rows[0]["authority_rank"]
        top = [r for r in rows if r["authority_rank"] == top_rank]
        values = {r["normalized_value"] or r["value_json"] for r in top}
        candidates = [self._fact_with_provenance(r) for r in top]
        if len(values) > 1:
            return {"selected": None, "conflict": True, "candidates": candidates}
        return {"selected": candidates[0], "conflict": False, "candidates": candidates}

    def provenance(self, fact_id: str):
        fact = self.store.one("SELECT * FROM knowledge_facts WHERE id=?", (fact_id,))
        if not fact:
            raise HTTPException(404, "Knowledge fact not found.")
        return self._fact_with_provenance(fact)

    def _fact_with_provenance(self, fact: dict[str, Any]):
        fact = dict(fact)
        fact["value"] = json.loads(fact.pop("value_json"))
        version = self.store.one(
            "SELECT * FROM knowledge_document_versions WHERE id=?",
            (fact["document_version_id"],),
        )
        document = self.store.one(
            "SELECT id,source_type,external_id,name,source_uri FROM knowledge_documents WHERE id=?",
            (version["document_id"],),
        ) if version else None
        unit = self.store.one(
            "SELECT id,unit_index,unit_type,label FROM knowledge_units WHERE id=?",
            (fact["unit_id"],),
        ) if fact["unit_id"] else None
        fact["provenance"] = {
            "document": document,
            "version": {
                "id": version["id"],
                "version_key": version["version_key"],
                "modified_at": version["modified_at"],
                "content_sha256": version["content_sha256"],
                "status": version["status"],
            } if version else None,
            "unit": unit,
        }
        return fact

    def search(self, query: str, constraints: dict[str, Any] | None = None, limit: int = 10):
        constraints = constraints or {}
        limit = max(1, min(50, int(limit)))
        q = normalized_text(query)
        if not q:
            return []
        words = [w for w in q.split() if len(w) >= 2][:8] or [q]
        clauses = []
        args: list[Any] = []
        for word in words:
            clauses.append("LOWER(c.text_content) LIKE ?")
            args.append("%" + word.lower() + "%")
        where = " AND ".join(clauses)
        if constraints.get("document_id"):
            where += " AND d.id=?"
            args.append(constraints["document_id"])
        sql = (
            "SELECT c.*,d.id AS document_id,d.name,d.source_type,d.external_id,d.source_uri,"
            "v.version_key,v.modified_at,u.unit_index,u.unit_type,u.label "
            "FROM knowledge_chunks c "
            "JOIN knowledge_document_versions v ON v.id=c.document_version_id "
            "JOIN knowledge_documents d ON d.id=v.document_id "
            "LEFT JOIN knowledge_units u ON u.id=c.unit_id "
            f"WHERE v.status='current' AND {where} LIMIT ?"
        )
        args.append(limit * 4)
        rows = self.store.all(sql, tuple(args))
        result = []
        for row in rows:
            hay = normalized_text(row["text_content"])
            score = sum(hay.count(word) for word in words)
            result.append({
                "type": "chunk",
                "score": score,
                "text": row["text_content"],
                "document": {
                    "id": row["document_id"], "name": row["name"],
                    "source_type": row["source_type"], "external_id": row["external_id"],
                    "source_uri": row["source_uri"], "version_key": row["version_key"],
                    "modified_at": row["modified_at"],
                },
                "unit": {
                    "id": row["unit_id"], "index": row["unit_index"],
                    "type": row["unit_type"], "label": row["label"],
                } if row["unit_id"] else None,
            })
        aliases = self.store.all(
            "SELECT e.id,e.entity_type,e.canonical_name,a.alias FROM knowledge_aliases a "
            "JOIN knowledge_entities e ON e.id=a.entity_id WHERE a.normalized_alias LIKE ? LIMIT ?",
            ("%" + q + "%", limit),
        )
        for row in aliases:
            result.append({
                "type": "entity",
                "score": 100 if normalized_text(row["alias"]) == q else 25,
                "entity": {
                    "id": row["id"], "entity_type": row["entity_type"],
                    "canonical_name": row["canonical_name"], "matched_alias": row["alias"],
                },
            })
        result.sort(key=lambda item: item["score"], reverse=True)
        return result[:limit]


def install(app) -> None:
    store = app.state.store
    apply_migration(store)
    provider = SqliteKnowledgeProvider(store)
    app.state.knowledge = provider

    def actor(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 520:
            raise HTTPException(401, "Sign in first.")
        user = store.one(
            "SELECT id,role FROM users WHERE token_hash=?",
            (token_hash(authorization[7:]),),
        )
        if not user:
            raise HTTPException(401, "Invalid access token.")
        return user

    def admin(user=Depends(actor)):
        if user["role"] != "admin":
            raise HTTPException(403, "Owner access required.")
        return user

    @app.get("/api/knowledge/status")
    def status(user=Depends(admin)):
        counts = {}
        for key, table in (
            ("documents", "knowledge_documents"),
            ("versions", "knowledge_document_versions"),
            ("units", "knowledge_units"),
            ("entities", "knowledge_entities"),
            ("facts", "knowledge_facts"),
            ("chunks", "knowledge_chunks"),
            ("assets", "knowledge_assets"),
        ):
            counts[key] = store.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        return {
            "schema_version": "raneen-knowledge-v1",
            "raw_file_storage": "external",
            "retrieval": "lexical_metadata_foundation",
            "vector_index": False,
            "counts": counts,
        }

    @app.post("/api/knowledge/ingestions", status_code=201)
    def start_run(body: RunInput, user=Depends(admin)):
        ident = uid()
        store.execute(
            "INSERT INTO knowledge_ingestion_runs VALUES(?,?,?,?,?,?,?,?,?)",
            (ident, body.source_type, body.root_external_id, "running", None, "{}", None, now(), None),
        )
        store.audit(user["id"], "knowledge_ingestion_started", ident, body.model_dump())
        return {"id": ident, "status": "running"}

    @app.patch("/api/knowledge/ingestions/{ident}")
    def update_run(ident: str, body: RunUpdate, user=Depends(admin)):
        if not store.one("SELECT id FROM knowledge_ingestion_runs WHERE id=?", (ident,)):
            raise HTTPException(404, "Knowledge ingestion run not found.")
        completed = now() if body.status in {"completed", "failed", "cancelled"} else None
        store.execute(
            "UPDATE knowledge_ingestion_runs SET status=?,cursor=?,counts_json=?,error=?,completed_at=? WHERE id=?",
            (body.status, body.cursor, canonical(body.counts), body.error, completed, ident),
        )
        store.audit(user["id"], "knowledge_ingestion_updated", ident, {"status": body.status})
        return {"id": ident, "status": body.status}

    @app.post("/api/knowledge/documents", status_code=201)
    def register_document(body: DocumentInput, user=Depends(admin)):
        result = provider.register_document(body)
        store.audit(user["id"], "knowledge_document_registered", result["document_version_id"], {
            "document_id": result["document_id"], "source_type": body.source_type,
            "external_id": body.external_id, "version_key": body.version_key,
            "idempotent": result["idempotent"],
        })
        return result

    @app.get("/api/knowledge/documents")
    def list_documents(limit: int = 100, user=Depends(admin)):
        limit = max(1, min(500, limit))
        rows = store.all(
            "SELECT d.*,v.version_key,v.mime_type,v.size_bytes,v.modified_at,v.status "
            "FROM knowledge_documents d LEFT JOIN knowledge_document_versions v "
            "ON v.id=d.current_version_id ORDER BY d.updated_at DESC LIMIT ?",
            (limit,),
        )
        for row in rows:
            row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
        return {"items": rows}

    @app.post("/api/knowledge/units", status_code=201)
    def add_unit(body: UnitInput, user=Depends(admin)):
        result = provider.add_unit(body)
        store.audit(user["id"], "knowledge_unit_recorded", result["id"], {
            "document_version_id": body.document_version_id, "unit_type": body.unit_type,
            "unit_index": body.unit_index, "idempotent": result["idempotent"],
        })
        return result

    @app.post("/api/knowledge/entities", status_code=201)
    def add_entity(body: EntityInput, user=Depends(admin)):
        result = provider.upsert_entity(body)
        store.audit(user["id"], "knowledge_entity_upserted", result["id"], {
            "entity_type": body.entity_type, "created": result["created"],
        })
        return result

    @app.get("/api/knowledge/entities/{ident}")
    def get_entity(ident: str, user=Depends(admin)):
        return provider.get_entity(ident)

    @app.post("/api/knowledge/facts", status_code=201)
    def add_fact(body: FactInput, user=Depends(admin)):
        result = provider.add_fact(body)
        store.audit(user["id"], "knowledge_fact_recorded", result["id"], {
            "entity_id": body.entity_id, "field_key": body.field_key,
            "document_version_id": body.document_version_id,
            "idempotent": result["idempotent"],
        })
        return result

    @app.get("/api/knowledge/entities/{ident}/facts/{field_key}")
    def current_fact(ident: str, field_key: str, user=Depends(admin)):
        return provider.get_current_fact(ident, field_key)

    @app.get("/api/knowledge/facts/{ident}/provenance")
    def provenance(ident: str, user=Depends(admin)):
        return provider.provenance(ident)

    @app.post("/api/knowledge/chunks", status_code=201)
    def add_chunk(body: ChunkInput, user=Depends(admin)):
        result = provider.add_chunk(body)
        store.audit(user["id"], "knowledge_chunk_recorded", result["id"], {
            "document_version_id": body.document_version_id,
            "unit_id": body.unit_id, "chunk_index": body.chunk_index,
            "idempotent": result["idempotent"],
        })
        return result

    @app.post("/api/knowledge/assets", status_code=201)
    def add_asset(body: AssetInput, user=Depends(admin)):
        result = provider.add_asset(body)
        store.audit(user["id"], "knowledge_asset_recorded", result["id"], {
            "document_version_id": body.document_version_id,
            "asset_type": body.asset_type,
            "idempotent": result["idempotent"],
        })
        return result

    @app.post("/api/knowledge/relations", status_code=201)
    def add_relation(body: RelationInput, user=Depends(admin)):
        result = provider.add_relation(body)
        store.audit(user["id"], "knowledge_document_relation_recorded", result["id"], {
            "relation_type": body.relation_type,
            "from_document_id": body.from_document_id,
            "to_document_id": body.to_document_id,
        })
        return result

    @app.get("/api/knowledge/search")
    def search(q: str, limit: int = 10, document_id: str | None = None, user=Depends(admin)):
        return {
            "query": q,
            "items": provider.search(q, {"document_id": document_id} if document_id else {}, limit),
            "retrieval": "lexical_metadata_foundation",
        }
