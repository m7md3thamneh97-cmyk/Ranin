CREATE TABLE learning_jobs (
 id TEXT PRIMARY KEY,
 profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
 session_id TEXT NOT NULL REFERENCES teaching_sessions(id) ON DELETE CASCADE,
 turn_id TEXT NOT NULL UNIQUE REFERENCES conversation_turns(id) ON DELETE CASCADE,
 status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
 attempts INTEGER NOT NULL DEFAULT 0,
 error_json TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE turn_audio_attachments (
 id TEXT PRIMARY KEY,
 profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
 turn_id TEXT NOT NULL UNIQUE REFERENCES conversation_turns(id) ON DELETE CASCADE,
 audio_id TEXT NOT NULL REFERENCES audio(id),
 consent_id TEXT NOT NULL REFERENCES consents(id),
 created_at TEXT NOT NULL
);
CREATE TRIGGER immutable_conversation_evidence
BEFORE UPDATE ON conversation_turns
BEGIN SELECT RAISE(ABORT,'Conversation evidence is immutable'); END;
CREATE TRIGGER immutable_turn_audio_attachment
BEFORE UPDATE ON turn_audio_attachments
BEGIN SELECT RAISE(ABORT,'Turn audio evidence is immutable'); END;
