"""Durable custody for standalone development transport assistants.

The composed enrollment app uses its own bounded private-call pipeline instead.
No provider payloads, credentials, or prompts are persisted by this registry.
"""
import hashlib
import json

from fastapi import HTTPException

from .app import now, uid


class TransportRegistry:
    def __init__(self, store, provider_factory):
        self.store, self.provider_factory = store, provider_factory

    def _active(self, session_id):
        row = self.store.one("SELECT s.*,c.collection,c.withdrawn_at FROM teaching_sessions s JOIN consents c ON c.id=s.consent_id WHERE s.id=?", (session_id,))
        return bool(row and row['status']=='active' and row['collection'] and not row['withdrawn_at'])

    def provision(self, session, config):
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            fresh = db.execute("SELECT s.status,c.collection,c.withdrawn_at FROM teaching_sessions s JOIN consents c ON c.id=s.consent_id WHERE s.id=?", (session['id'],)).fetchone()
            if not fresh or fresh['status']!='active' or not fresh['collection'] or fresh['withdrawn_at']:
                raise HTTPException(409, 'Transport consent is inactive.')
            previous = db.execute('SELECT * FROM transport_operations WHERE session_id=? AND fingerprint=?', (session['id'],fingerprint)).fetchone()
            if previous:
                if previous['state']=='ready':
                    return previous['provider_id']
                raise HTTPException(409, 'Transport creation needs reconciliation; it will not be repeated.')
            blocked = db.execute("SELECT id FROM transport_operations WHERE session_id=? AND state IN ('dispatching','outcome_unknown','deletion_pending')",(session['id'],)).fetchone()
            if blocked:
                raise HTTPException(409, 'Resolve the pending transport operation before creating another assistant.')
            count = db.execute('SELECT COUNT(*) FROM transport_operations WHERE session_id=?',(session['id'],)).fetchone()[0]
            if count >= 3:
                raise HTTPException(429, 'Development assistant creation limit reached for this session.')
            ident, stamp = uid(), now()
            owner=db.execute('SELECT owner_id FROM profiles WHERE id=?',(session['profile_id'],)).fetchone()['owner_id']
            db.execute('INSERT INTO transport_operations VALUES(?,?,?,?,?,?,?,NULL,?,?)',(ident,session['id'],session['profile_id'],session['consent_id'],owner,fingerprint,'dispatching',stamp,stamp))
        outgoing = dict(config)
        outgoing['metadata'] = {**config.get('metadata',{}),'raneen_operation_id':ident}
        try:
            provider_id = self.provider_factory().create_assistant(outgoing)
        except Exception as error:
            # Definitive rejection can be recorded, but never automatically replayed.
            state = 'outcome_unknown' if getattr(error,'uncertain',True) else 'rejected'
            self.store.execute('UPDATE transport_operations SET state=?,updated_at=? WHERE id=?',(state,now(),ident))
            raise
        self.store.execute("UPDATE transport_operations SET provider_id=?,state='ready',updated_at=? WHERE id=?",(provider_id,now(),ident))
        if not self._active(session['id']):
            self.store.execute("UPDATE transport_operations SET state='deletion_pending',updated_at=? WHERE id=?",(now(),ident))
            self.cleanup(session['profile_id'])
            raise HTTPException(409,'Consent ended during assistant creation; cleanup was recorded.')
        return provider_id

    def reconcile(self, session_id, assistant_id, actor_id):
        receipt=self.store.one('SELECT owner_id FROM transport_operations WHERE session_id=? ORDER BY created_at DESC LIMIT 1',(session_id,))
        if not receipt:
            raise HTTPException(404,'Transport creation receipt not found.')
        if receipt['owner_id']!=actor_id:
            raise HTTPException(403,'Only the transport owner can reconcile this operation.')
        candidate = self.provider_factory().get_assistant(assistant_id)
        metadata = candidate.get('metadata',{})
        if not isinstance(metadata,dict) or metadata.get('raneen_session_id')!=session_id:
            raise HTTPException(422,'Assistant does not belong to this session.')
        operation = self.store.one('SELECT * FROM transport_operations WHERE id=? AND session_id=?',(metadata.get('raneen_operation_id'),session_id))
        if not operation or operation['state'] not in ('dispatching','outcome_unknown','ready') or candidate.get('id')!=assistant_id:
            raise HTTPException(409,'Assistant does not match a pending operation.')
        state = 'ready' if self._active(session_id) else 'deletion_pending'
        self.store.execute('UPDATE transport_operations SET provider_id=?,state=?,updated_at=? WHERE id=?',(assistant_id,state,now(),operation['id']))
        if state=='deletion_pending':
            self.cleanup(operation['profile_id'])
        return {'operation_id':operation['id'],'state':state,'assistant_id':assistant_id}

    def cleanup(self, profile_id=None):
        rows = self.store.all("SELECT * FROM transport_operations WHERE state IN ('ready','deletion_pending')" + (' AND profile_id=?' if profile_id else ''), (profile_id,) if profile_id else ())
        results=[]
        for row in rows:
            if not profile_id and row['state']=='ready' and self._active(row['session_id']):
                continue
            self.store.execute("UPDATE transport_operations SET state='deletion_pending',updated_at=? WHERE id=?",(now(),row['id']))
            try:
                self.provider_factory().delete_assistant(row['provider_id'])
            except Exception:
                results.append({'operation_id':row['id'],'state':'deletion_pending'})
                continue
            self.store.execute("UPDATE transport_operations SET state='deleted',updated_at=? WHERE id=?",(now(),row['id']))
            results.append({'operation_id':row['id'],'state':'deleted'})
        return results

    def recover(self):
        self.store.execute("UPDATE transport_operations SET state='outcome_unknown',updated_at=? WHERE state='dispatching'",(now(),))
        return self.cleanup()
