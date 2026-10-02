CREATE TABLE tester_invitations (
 id TEXT PRIMARY KEY,
 owner_id TEXT NOT NULL REFERENCES users(id),
 code_hash TEXT NOT NULL UNIQUE,
 created TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 redeemed_at TEXT,
 revoked_at TEXT,
 user_id TEXT UNIQUE REFERENCES users(id)
);
CREATE TABLE tester_grants (
 user_id TEXT PRIMARY KEY REFERENCES users(id),
 invitation_id TEXT NOT NULL UNIQUE REFERENCES tester_invitations(id),
 created TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 revoked_at TEXT
);
CREATE TABLE tester_redemption_limits (
 bucket TEXT PRIMARY KEY,
 window_started REAL NOT NULL,
 attempts INTEGER NOT NULL
);
