CREATE TABLE teaching_sessions (
 id TEXT PRIMARY KEY,
 profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
 consent_id TEXT NOT NULL REFERENCES consents(id),
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','ended')),
 mode TEXT NOT NULL DEFAULT 'teaching' CHECK(mode IN ('teaching','simulation')),
 split TEXT NOT NULL DEFAULT 'train' CHECK(split IN ('train','holdout')),
 scenario_id TEXT,
 realtime_provider TEXT NOT NULL DEFAULT 'local',
 provider_call_id TEXT UNIQUE,
 active_profile_version_id TEXT,
 active_voice_version_id TEXT,
 started_at TEXT NOT NULL,
 ended_at TEXT,
 created_at TEXT NOT NULL,
 idempotency_key TEXT,
 UNIQUE(profile_id,idempotency_key)
);
CREATE TABLE conversation_turns (
 id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES teaching_sessions(id) ON DELETE CASCADE,
 profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
 consent_id TEXT NOT NULL REFERENCES consents(id),
 turn_index INTEGER NOT NULL CHECK(turn_index >= 0),
 role TEXT NOT NULL CHECK(role IN ('trainer','raneen')),
 transcript TEXT NOT NULL,
 transcript_state TEXT NOT NULL CHECK(transcript_state IN ('final','verified')),
 audio_id TEXT REFERENCES audio(id),
 started_at TEXT NOT NULL,
 ended_at TEXT NOT NULL,
 provider_metadata_json TEXT NOT NULL DEFAULT '{}',
 external_id TEXT,
 created_at TEXT NOT NULL,
 UNIQUE(session_id,turn_index),
 UNIQUE(session_id,external_id)
);
CREATE INDEX turns_profile_index ON conversation_turns(profile_id,session_id,turn_index);
CREATE TABLE session_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT NOT NULL UNIQUE,
 session_id TEXT NOT NULL REFERENCES teaching_sessions(id) ON DELETE CASCADE,
 type TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX events_session_index ON session_events(session_id,sequence);
CREATE TABLE provider_authorizations (
 id TEXT PRIMARY KEY,
 profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
 consent_id TEXT NOT NULL REFERENCES consents(id),
 provider TEXT NOT NULL CHECK(provider IN ('openai','elevenlabs','vapi')),
 scope TEXT NOT NULL CHECK(scope IN ('text_learning','voice_clone','realtime_voice')),
 self_attestation INTEGER NOT NULL CHECK(self_attestation=1),
 created_at TEXT NOT NULL,
 withdrawn_at TEXT
);
CREATE TABLE realtime_receipts (
 id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES teaching_sessions(id) ON DELETE CASCADE,
 event_id TEXT NOT NULL,
 payload_hash TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(session_id,event_id)
);
