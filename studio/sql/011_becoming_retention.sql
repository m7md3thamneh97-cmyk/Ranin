ALTER TABLE becoming_sessions ADD COLUMN retention_expires_at TEXT;
UPDATE becoming_sessions SET retention_expires_at=expires_at WHERE retention_expires_at IS NULL;
