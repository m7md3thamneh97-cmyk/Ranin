-- Provider creation receipts intentionally survive local profile/version deletion.
-- An unknown remote outcome must remain available for operator reconciliation.
CREATE TABLE IF NOT EXISTS voice_provider_operations (
    id TEXT PRIMARY KEY,
    voice_version_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('prepared','dispatching','outcome_unknown','succeeded','rejected','reconciled_not_created','cleanup_deleted')),
    provider_voice_id TEXT,
    provider_name TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reconciled_at TEXT,
    reconciliation_notes TEXT NOT NULL DEFAULT '',
    UNIQUE (voice_version_id, attempt)
);
CREATE INDEX IF NOT EXISTS voice_provider_operations_profile ON voice_provider_operations(profile_id,state);
