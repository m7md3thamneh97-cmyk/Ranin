"""Durable learning receipts. Provider failures never discard a human turn."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

from fastapi import HTTPException


def now():
    return datetime.now(timezone.utc).isoformat()


class LearningJobs:
    def __init__(self,store,learning,on_result=None):
        self.store,self.learning,self.on_result=store,learning,on_result

    def enqueue(self,turn_id):
        turn=self.store.one('SELECT t.*,s.split,s.mode FROM conversation_turns t JOIN teaching_sessions s ON s.id=t.session_id WHERE t.id=?',(turn_id,))
        if not turn or turn['role']!='trainer' or turn['split']!='train':
            return None
        captured=json.loads(turn['provider_metadata_json']).get('capture_mode',turn['mode'])
        if captured!='teaching':
            return None
        stamp=now()
        self.store.execute("INSERT OR IGNORE INTO learning_jobs(id,profile_id,session_id,turn_id,status,created_at,updated_at) VALUES(?,?,?,?,'queued',?,?)",(uuid.uuid4().hex,turn['profile_id'],turn['session_id'],turn_id,stamp,stamp))
        return self.store.one('SELECT * FROM learning_jobs WHERE turn_id=?',(turn_id,))

    def run(self,job_id):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM learning_jobs WHERE id=?',(job_id,)).fetchone()
            if not row or row['status'] not in ('queued','failed'):
                return
            row=dict(row)
            db.execute("UPDATE learning_jobs SET status='running',attempts=attempts+1,error_json=NULL,updated_at=? WHERE id=?",(now(),job_id))
        try:
            result=self.learning.analyze_turn(row['turn_id'])
            if self.on_result and not result.get('idempotent'):
                self.on_result(row['session_id'],result)
            self.store.execute("UPDATE learning_jobs SET status='completed',error_json=NULL,updated_at=? WHERE id=?",(now(),job_id))
        except Exception as exc:
            code=getattr(exc,'code','learning_blocked' if isinstance(exc,HTTPException) else 'learning_failed')
            error={'code':str(code)[:80],'message':'Learning did not complete. The original turn is saved; check consent, provider configuration, or retry.'}
            self.store.execute("UPDATE learning_jobs SET status='failed',error_json=?,updated_at=? WHERE id=?",(json.dumps(error),now(),job_id))

    def recover(self,stopped=None):
        # One process/replica is the existing deployment contract. A crash may
        # leave a claimed job running; replay is safe because turn analysis is idempotent.
        self.store.execute("UPDATE learning_jobs SET status='queued',updated_at=? WHERE status='running'",(now(),))
        for row in self.store.all("SELECT id FROM learning_jobs WHERE status='queued' ORDER BY created_at LIMIT 100"):
            if stopped is not None and stopped.is_set():
                break
            self.run(row['id'])
