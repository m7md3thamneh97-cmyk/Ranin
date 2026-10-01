CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    source_turn_id TEXT REFERENCES conversation_turns(id) ON DELETE CASCADE,
    source_example_id TEXT REFERENCES examples(id) ON DELETE CASCADE,
    consent_id TEXT NOT NULL REFERENCES consents(id),
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    state TEXT NOT NULL CHECK(state IN ('observed','confirmed','locked','rejected')),
    evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('inference','observation','demonstration','confirmation','correction','explicit_rule')),
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    CHECK((source_turn_id IS NOT NULL) != (source_example_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS observations_profile_key ON observations(profile_id,category,key);
CREATE INDEX IF NOT EXISTS observations_source_turn ON observations(source_turn_id);
CREATE UNIQUE INDEX IF NOT EXISTS observation_turn_once ON observations(source_turn_id,category,key,value_json,evidence_kind) WHERE source_turn_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS observation_example_once ON observations(source_example_id,category,key,value_json,evidence_kind) WHERE source_example_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_count INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('tentative','confirmed','locked','rejected')),
    precedence INTEGER NOT NULL,
    conflict INTEGER NOT NULL DEFAULT 0,
    last_tested_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(profile_id,category,key,value_json)
);
CREATE TABLE IF NOT EXISTS hypothesis_observations (
    hypothesis_id TEXT NOT NULL REFERENCES hypotheses(id) ON DELETE CASCADE,
    observation_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    PRIMARY KEY(hypothesis_id,observation_id)
);
CREATE TABLE IF NOT EXISTS corrections (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES teaching_sessions(id) ON DELETE CASCADE,
    target_turn_id TEXT NOT NULL REFERENCES conversation_turns(id) ON DELETE CASCADE,
    source_turn_id TEXT NOT NULL REFERENCES conversation_turns(id) ON DELETE CASCADE,
    consent_id TEXT NOT NULL REFERENCES consents(id),
    correction_transcript TEXT NOT NULL,
    normalized_rule_json TEXT NOT NULL,
    observation_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    UNIQUE(source_turn_id,target_turn_id,normalized_rule_json)
);
CREATE TABLE IF NOT EXISTS agent_profile_versions (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','approved','published','archived')),
    parent_version_id TEXT REFERENCES agent_profile_versions(id) ON DELETE SET NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    UNIQUE(profile_id,version_number)
);
CREATE TRIGGER IF NOT EXISTS immutable_personal_profile_snapshot
BEFORE UPDATE OF profile_id,version_number,snapshot_json,created_at ON agent_profile_versions
BEGIN SELECT RAISE(ABORT,'Personal profile snapshots are immutable'); END;
CREATE TABLE IF NOT EXISTS learning_analyses (
    source_turn_id TEXT PRIMARY KEY REFERENCES conversation_turns(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
