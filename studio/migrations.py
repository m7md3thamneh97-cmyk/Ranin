"""Checksummed, transactional schema upgrades; existing evidence is preserved."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3


def migrate(store):
    directory = Path(__file__).parent / 'sql'
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        for path in sorted(directory.glob('*.sql')):
            sql = path.read_text(encoding='utf-8')
            digest = hashlib.sha256(sql.encode()).hexdigest()
            previous = db.execute('SELECT sha256 FROM schema_migrations WHERE name=?', (path.name,)).fetchone()
            if previous:
                if previous['sha256'] != digest:
                    raise RuntimeError(f'Applied migration was modified: {path.name}')
                continue
            statement = ''
            for line in sql.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ''
            if statement.strip():
                raise RuntimeError(f'Incomplete SQL migration: {path.name}')
            db.execute('INSERT INTO schema_migrations(name,sha256) VALUES(?,?)', (path.name, digest))
