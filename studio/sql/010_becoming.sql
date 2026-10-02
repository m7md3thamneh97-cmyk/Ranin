CREATE TABLE IF NOT EXISTS becoming_schema_versions(version INTEGER PRIMARY KEY, sha256 TEXT NOT NULL, applied TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS becoming_sessions(
 id TEXT PRIMARY KEY, capability_hash TEXT NOT NULL UNIQUE, webhook_hash TEXT NOT NULL,
 client_hash TEXT NOT NULL, consent_version TEXT NOT NULL, consent_json TEXT NOT NULL,
 state TEXT NOT NULL, total_bytes INTEGER NOT NULL DEFAULT 0, eligible_ms INTEGER NOT NULL DEFAULT 0,
 minimum_ms INTEGER NOT NULL, voice_id TEXT, voice_ready INTEGER NOT NULL DEFAULT 0,
 call_id TEXT, call_state TEXT NOT NULL DEFAULT 'not_started', call_config TEXT,
 control_url TEXT, bootstrap_config TEXT, telemetry TEXT NOT NULL DEFAULT '{}',
 failure TEXT, revoked_at TEXT, ended_at TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
 expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS becoming_chunks(
 session_id TEXT NOT NULL REFERENCES becoming_sessions(id) ON DELETE CASCADE,
 seq INTEGER NOT NULL, sha256 TEXT NOT NULL, byte_count INTEGER NOT NULL,
 duration_ms INTEGER NOT NULL, eligible_ms INTEGER NOT NULL, capture_json TEXT NOT NULL,
 path TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(session_id,seq)
);
CREATE TABLE IF NOT EXISTS becoming_operations(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES becoming_sessions(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, op_key TEXT NOT NULL, state TEXT NOT NULL, provider_id TEXT,
 detail TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL, updated TEXT NOT NULL,
 UNIQUE(session_id,kind,op_key)
);
CREATE TABLE IF NOT EXISTS becoming_events(
 session_id TEXT NOT NULL REFERENCES becoming_sessions(id) ON DELETE CASCADE,
 event_id TEXT NOT NULL, source TEXT NOT NULL, type TEXT NOT NULL,
 payload TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(session_id,event_id)
);
CREATE INDEX IF NOT EXISTS becoming_session_created ON becoming_sessions(created);
CREATE INDEX IF NOT EXISTS becoming_event_source ON becoming_events(session_id,source,type);
