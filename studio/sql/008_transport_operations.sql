CREATE TABLE transport_operations (
 id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL,
 profile_id TEXT NOT NULL,
 consent_id TEXT NOT NULL,
 owner_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('dispatching','ready','outcome_unknown','rejected','deletion_pending','deleted')),
 provider_id TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(session_id,fingerprint)
);
CREATE INDEX transport_cleanup_index ON transport_operations(state,profile_id);
