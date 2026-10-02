CREATE TABLE IF NOT EXISTS knowledge_schema_versions(
 version INTEGER PRIMARY KEY, sha256 TEXT NOT NULL, applied TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_documents(
 id TEXT PRIMARY KEY,
 source_type TEXT NOT NULL,
 external_id TEXT NOT NULL,
 parent_external_id TEXT,
 name TEXT NOT NULL,
 source_uri TEXT,
 current_version_id TEXT,
 metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(source_type, external_id)
);
CREATE TABLE IF NOT EXISTS knowledge_document_versions(
 id TEXT PRIMARY KEY,
 document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
 version_key TEXT NOT NULL,
 mime_type TEXT,
 size_bytes INTEGER,
 modified_at TEXT,
 content_sha256 TEXT,
 status TEXT NOT NULL DEFAULT 'current',
 metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,
 UNIQUE(document_id, version_key)
);
CREATE INDEX IF NOT EXISTS knowledge_version_document ON knowledge_document_versions(document_id,status);
CREATE TABLE IF NOT EXISTS knowledge_ingestion_runs(
 id TEXT PRIMARY KEY,
 source_type TEXT NOT NULL,
 root_external_id TEXT,
 status TEXT NOT NULL,
 cursor TEXT,
 counts_json TEXT NOT NULL DEFAULT '{}',
 error TEXT,
 started_at TEXT NOT NULL,
 completed_at TEXT
);
CREATE TABLE IF NOT EXISTS knowledge_units(
 id TEXT PRIMARY KEY,
 document_version_id TEXT NOT NULL REFERENCES knowledge_document_versions(id) ON DELETE CASCADE,
 unit_index INTEGER NOT NULL,
 unit_type TEXT NOT NULL,
 label TEXT,
 text_content TEXT NOT NULL DEFAULT '',
 visual_summary TEXT NOT NULL DEFAULT '',
 text_sha256 TEXT,
 metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,
 UNIQUE(document_version_id, unit_type, unit_index)
);
CREATE INDEX IF NOT EXISTS knowledge_unit_version ON knowledge_units(document_version_id,unit_index);
CREATE TABLE IF NOT EXISTS knowledge_entities(
 id TEXT PRIMARY KEY,
 entity_type TEXT NOT NULL,
 canonical_name TEXT NOT NULL,
 normalized_name TEXT NOT NULL,
 metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(entity_type, normalized_name)
);
CREATE TABLE IF NOT EXISTS knowledge_aliases(
 entity_id TEXT NOT NULL REFERENCES knowledge_entities(id) ON DELETE CASCADE,
 alias TEXT NOT NULL,
 normalized_alias TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(entity_id, normalized_alias)
);
CREATE INDEX IF NOT EXISTS knowledge_alias_lookup ON knowledge_aliases(normalized_alias);
CREATE TABLE IF NOT EXISTS knowledge_facts(
 id TEXT PRIMARY KEY,
 entity_id TEXT NOT NULL REFERENCES knowledge_entities(id) ON DELETE CASCADE,
 field_key TEXT NOT NULL,
 value_json TEXT NOT NULL,
 normalized_value TEXT,
 document_version_id TEXT NOT NULL REFERENCES knowledge_document_versions(id) ON DELETE CASCADE,
 unit_id TEXT REFERENCES knowledge_units(id) ON DELETE SET NULL,
 authority_rank INTEGER NOT NULL,
 confidence REAL NOT NULL,
 effective_at TEXT,
 observed_at TEXT,
 state TEXT NOT NULL DEFAULT 'active',
 supersedes_fact_id TEXT REFERENCES knowledge_facts(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS knowledge_fact_lookup ON knowledge_facts(entity_id,field_key,state,authority_rank);
CREATE INDEX IF NOT EXISTS knowledge_fact_source ON knowledge_facts(document_version_id,state);
CREATE TABLE IF NOT EXISTS knowledge_chunks(
 id TEXT PRIMARY KEY,
 document_version_id TEXT NOT NULL REFERENCES knowledge_document_versions(id) ON DELETE CASCADE,
 unit_id TEXT REFERENCES knowledge_units(id) ON DELETE CASCADE,
 chunk_index INTEGER NOT NULL,
 text_content TEXT NOT NULL,
 token_estimate INTEGER,
 metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,
 UNIQUE(document_version_id, unit_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS knowledge_chunk_version ON knowledge_chunks(document_version_id,chunk_index);
CREATE TABLE IF NOT EXISTS knowledge_assets(
 id TEXT PRIMARY KEY,
 document_version_id TEXT NOT NULL REFERENCES knowledge_document_versions(id) ON DELETE CASCADE,
 unit_id TEXT REFERENCES knowledge_units(id) ON DELETE SET NULL,
 asset_type TEXT NOT NULL,
 source_locator TEXT NOT NULL,
 summary TEXT NOT NULL DEFAULT '',
 metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS knowledge_asset_source ON knowledge_assets(document_version_id,asset_type);
CREATE TABLE IF NOT EXISTS knowledge_document_relations(
 id TEXT PRIMARY KEY,
 from_document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
 to_document_id TEXT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
 relation_type TEXT NOT NULL,
 reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 UNIQUE(from_document_id,to_document_id,relation_type)
);
