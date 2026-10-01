CREATE TABLE IF NOT EXISTS voice_samples (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    audio_id TEXT NOT NULL REFERENCES audio(id) ON DELETE CASCADE,
    source_turn_id TEXT NOT NULL REFERENCES conversation_turns(id) ON DELETE CASCADE,
    eligibility TEXT NOT NULL DEFAULT 'pending' CHECK (eligibility IN ('pending','eligible','rejected')),
    rejection_reason TEXT,
    quality_json TEXT NOT NULL,
    reviewed_by TEXT REFERENCES users(id),
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (profile_id, audio_id),
    UNIQUE (source_turn_id)
);
CREATE INDEX IF NOT EXISTS voice_samples_profile ON voice_samples(profile_id, eligibility);
CREATE TABLE IF NOT EXISTS voice_versions (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    provider TEXT NOT NULL,
    provider_voice_id TEXT,
    source_manifest_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('building','ready','approved','rejected','failed')),
    idempotency_key TEXT NOT NULL,
    preview_text TEXT NOT NULL,
    error_json TEXT,
    rejection_notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT,
    UNIQUE (profile_id, version_number),
    UNIQUE (profile_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS voice_versions_profile ON voice_versions(profile_id, status);
-- Cleanup custody survives local profile deletion so a provider outage does not
-- erase the only pointer to a remote voice that still needs removal.
CREATE TABLE IF NOT EXISTS voice_cleanup_jobs (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_voice_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('retained','pending','deleted')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (provider, provider_voice_id)
);
