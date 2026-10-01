CREATE TABLE simulation_runs (
 id TEXT PRIMARY KEY,
 profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
 session_id TEXT NOT NULL REFERENCES teaching_sessions(id) ON DELETE CASCADE,
 consent_id TEXT NOT NULL REFERENCES consents(id),
 scenario_id TEXT NOT NULL,
 profile_version_id TEXT REFERENCES agent_profile_versions(id) ON DELETE CASCADE,
 voice_version_id TEXT,
 parent_run_id TEXT REFERENCES simulation_runs(id) ON DELETE CASCADE,
 caller_text TEXT NOT NULL,
 response_text TEXT NOT NULL,
 turn_id TEXT NOT NULL REFERENCES conversation_turns(id) ON DELETE CASCADE,
 context_json TEXT NOT NULL,
 provider TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX simulation_session_index ON simulation_runs(session_id,created_at);
CREATE TRIGGER immutable_simulation_run
BEFORE UPDATE ON simulation_runs
BEGIN SELECT RAISE(ABORT,'Simulation runs are immutable'); END;
CREATE TABLE evaluation_runs (
 id TEXT PRIMARY KEY,
 profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
 consent_id TEXT NOT NULL REFERENCES consents(id),
 profile_version_id TEXT REFERENCES agent_profile_versions(id) ON DELETE CASCADE,
 suite_version TEXT NOT NULL,
 provider TEXT NOT NULL,
 context_json TEXT NOT NULL,
 results_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pass','fail','partial')),
 created_at TEXT NOT NULL
);
CREATE INDEX evaluation_profile_index ON evaluation_runs(profile_id,created_at);
CREATE TRIGGER immutable_evaluation_run
BEFORE UPDATE ON evaluation_runs
BEGIN SELECT RAISE(ABORT,'Evaluation runs are immutable'); END;
