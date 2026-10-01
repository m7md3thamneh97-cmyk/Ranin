-- Enrollment authorization remains separate from research/export permissions.
-- Optional enrollment-table references are checked by the server bridge rather
-- than FKs, so the research-only foundation can run without those tables.
CREATE TABLE enrollment_learning_bindings (
 enrollment_id TEXT PRIMARY KEY,
 profile_id TEXT NOT NULL UNIQUE REFERENCES profiles(id) ON DELETE CASCADE,
 consent_id TEXT NOT NULL UNIQUE REFERENCES consents(id),
 teaching_session_id TEXT NOT NULL UNIQUE REFERENCES teaching_sessions(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL,
 revoked_at TEXT
);
CREATE TABLE enrollment_learning_audio_modes (
 source_ordinal INTEGER PRIMARY KEY,
 enrollment_id TEXT NOT NULL,
 call_id TEXT NOT NULL,
 item_id TEXT NOT NULL,
 capture_mode TEXT NOT NULL CHECK(capture_mode IN ('teaching','simulation')),
 created_at TEXT NOT NULL,
 UNIQUE(enrollment_id,call_id,item_id)
);
CREATE TABLE enrollment_learning_evidence (
 evidence_id TEXT PRIMARY KEY,
 enrollment_id TEXT NOT NULL,
 profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
 source_turn_id TEXT NOT NULL REFERENCES conversation_turns(id) ON DELETE CASCADE,
 confirmation_turn_id TEXT NOT NULL REFERENCES conversation_turns(id) ON DELETE CASCADE,
 observation_id TEXT NOT NULL UNIQUE REFERENCES observations(id) ON DELETE CASCADE,
 category TEXT NOT NULL,
 rule_key TEXT NOT NULL,
 root_evidence_id TEXT NOT NULL,
 replaces_id TEXT,
 imported_at TEXT NOT NULL
);
CREATE TABLE enrollment_learning_correction_targets (
 evidence_id TEXT PRIMARY KEY,
 enrollment_id TEXT NOT NULL,
 simulation_id TEXT NOT NULL REFERENCES simulation_runs(id) ON DELETE CASCADE,
 target_turn_id TEXT NOT NULL REFERENCES conversation_turns(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL
);
CREATE INDEX enrollment_learning_evidence_profile ON enrollment_learning_evidence(profile_id,enrollment_id);
CREATE TRIGGER immutable_enrollment_learning_capture
BEFORE UPDATE ON enrollment_learning_audio_modes
BEGIN SELECT RAISE(ABORT,'Enrollment capture mode is immutable'); END;
CREATE TRIGGER immutable_enrollment_learning_provenance
BEFORE UPDATE ON enrollment_learning_evidence
BEGIN SELECT RAISE(ABORT,'Enrollment learning provenance is immutable'); END;
CREATE TRIGGER immutable_enrollment_correction_target
BEFORE UPDATE ON enrollment_learning_correction_targets
BEGIN SELECT RAISE(ABORT,'Enrollment correction target is immutable'); END;
