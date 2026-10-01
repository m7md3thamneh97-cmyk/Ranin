"""Conversation API atop the original, consent-scoped evidence store."""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .scenarios import BY_ID


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def enrollment_binding(store, profile_id):
    if not store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='enrollment_learning_bindings'"):
        return None
    return store.one('SELECT * FROM enrollment_learning_bindings WHERE profile_id=?',(profile_id,))


def reject_legacy_write(store, profile_id):
    if enrollment_binding(store, profile_id):
        raise HTTPException(409,'Enrollment evidence is changed only through its trusted voice workflow.')


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class SessionInput(Strict):
    profile_id: str = Field(min_length=1, max_length=32)
    scenario_id: str | None = Field(default=None, max_length=80)
    mode: Literal['teaching', 'simulation'] = 'teaching'
    split: Literal['train', 'holdout'] = 'train'
    realtime_provider: Literal['local', 'vapi'] = 'local'
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)


class TurnInput(Strict):
    role: Literal['trainer', 'raneen']
    transcript: str = Field(min_length=1, max_length=6000)
    transcript_state: Literal['final', 'verified'] = 'final'
    audio_id: str | None = Field(default=None, max_length=32)
    turn_index: int | None = Field(default=None, ge=0)
    external_id: str | None = Field(default=None, min_length=1, max_length=160)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    analyze: bool = True

    @model_validator(mode='after')
    def valid_metadata(self):
        if len(json.dumps(self.provider_metadata)) > 8000:
            raise ValueError('Turn metadata exceeds 8000 bytes.')
        if any(t is not None and t.utcoffset() is None for t in (self.started_at,self.ended_at)):
            raise ValueError('Turn timestamps must include a timezone.')
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValueError('Turn end must follow its start.')
        return self


class RuleInput(Strict):
    category: Literal['language', 'delivery', 'conversation', 'behavior', 'avoid']
    key: str = Field(min_length=1, max_length=160)
    value: dict[str, Any] | str | list[Any] | bool | int | float

    @model_validator(mode='after')
    def bounded_value(self):
        if len(json.dumps(self.value)) > 6000:
            raise ValueError('Rule value exceeds 6000 bytes.')
        return self


class CorrectionInput(Strict):
    source_turn_id: str = Field(min_length=1, max_length=32)
    rule: RuleInput | None = None
    lock: bool = False


class ConfirmationInput(Strict):
    source_turn_id: str = Field(min_length=1, max_length=32)
    lock: bool = False


class SimulationInput(Strict):
    scenario_id: str | None = Field(default=None, max_length=80)
    caller_text: str | None = Field(default=None, min_length=1, max_length=6000)


class RetryInput(Strict):
    caller_text: str | None = Field(default=None, min_length=1, max_length=6000)


class AuthorizationInput(Strict):
    provider: Literal['openai', 'elevenlabs', 'vapi']
    scope: Literal['text_learning', 'voice_clone', 'realtime_voice']
    self_attestation: bool

    @model_validator(mode='after')
    def valid_scope(self):
        if {'openai':'text_learning','elevenlabs':'voice_clone','vapi':'realtime_voice'}[self.provider] != self.scope:
            raise ValueError('Scope does not match provider.')
        if not self.self_attestation:
            raise ValueError('Separate voluntary provider authorization is required.')
        return self


class SampleReviewInput(Strict):
    trainer_only: bool
    clean_speech: bool
    desired_style: bool = True
    notes: str = Field(default='', max_length=2000)


class CandidateInput(Strict):
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)


class RejectInput(Strict):
    notes: str = Field(default='', max_length=2000)


class VersionInput(Strict):
    profile_version_id: str | None = Field(default=None, max_length=32)


class AudioAttachmentInput(Strict):
    audio_id: str = Field(min_length=1,max_length=32)


def record_event(store, session_id, event_type, payload=None, db=None):
    values = (uid(), session_id, event_type, json.dumps(payload or {}, ensure_ascii=False), now())
    sql = 'INSERT INTO session_events(id,session_id,type,payload_json,created_at) VALUES(?,?,?,?,?)'
    if db is not None:
        db.execute(sql, values)
    else:
        store.execute(sql, values)


def decode_turn(row):
    item = dict(row)
    item['provider_metadata'] = json.loads(item.pop('provider_metadata_json', '{}'))
    return item


def consent_active(store, consent_id):
    row = store.one('SELECT collection,withdrawn_at FROM consents WHERE id=?', (consent_id,))
    return bool(row and row['collection'] and not row['withdrawn_at'])


def require_audio_partition(db,sha256,split):
    turns=db.execute('SELECT s.split FROM conversation_turns t LEFT JOIN turn_audio_attachments ta ON ta.turn_id=t.id JOIN audio a ON a.id=COALESCE(t.audio_id,ta.audio_id) JOIN teaching_sessions s ON s.id=t.session_id WHERE a.sha256=?',(sha256,)).fetchall()
    if any(t['split']!=split for t in turns):
        raise HTTPException(409,'Exact audio cannot cross training and holdout partitions.')
    for row in db.execute('SELECT e.payload FROM examples e JOIN audio a ON a.id=json_extract(e.payload,\'$.audio_id\') WHERE a.sha256=?',(sha256,)):
        if BY_ID[json.loads(row['payload'])['scenario_id']]['split']!=split:
            raise HTTPException(409,'Exact audio cannot cross training and holdout partitions.')


def require_authorization(store, profile_id, provider, scope, consent_id=None):
    current = store.one('SELECT * FROM consents WHERE profile_id=? ORDER BY rowid DESC LIMIT 1', (profile_id,))
    if not current or not current['collection'] or current['withdrawn_at']:
        raise HTTPException(409, 'Active contributor consent is required.')
    cid = consent_id or current['id']
    if not consent_active(store, cid):
        raise HTTPException(409, 'Source consent has been withdrawn.')
    authorization = store.one('SELECT id FROM provider_authorizations WHERE profile_id=? AND consent_id=? AND provider=? AND scope=? AND self_attestation=1 AND withdrawn_at IS NULL', (profile_id, cid, provider, scope))
    if not authorization:
        raise HTTPException(403, f'Separate {provider} {scope} authorization is required for this evidence.')


class AuthorizedLearningProvider:
    def __init__(self, store, provider):
        self.store, self.provider = store, provider

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def _check(self, context):
        if getattr(self.provider, 'mode', 'local_rules') not in ('local', 'local_rules'):
            profile = context.get('profile', {})
            pid = profile.get('id') or context.get('profile_id')
            if not pid:
                raise HTTPException(409, 'Provider context must identify its profile.')
            if enrollment_binding(self.store,pid):
                from .enrollment_learning import authorize_learning_context
                if authorize_learning_context(self.store,pid,context):
                    return
            require_authorization(self.store, pid, 'openai', 'text_learning')
            # Authorization covers only consented evidence, including older consent epochs.
            cids = {r['consent_id'] for r in self.store.all('SELECT DISTINCT consent_id FROM observations WHERE profile_id=? AND state!=?', (pid, 'rejected')) if consent_active(self.store,r['consent_id'])}
            if context.get('source_consent_id'):
                cids.add(context['source_consent_id'])
            for rule in context.get('personal_rules',[]):
                cids.update(rule.get('consent_ids',[]))
            for example in context.get('representative_examples',[]):
                if example.get('consent_id'):
                    cids.add(example['consent_id'])
            source = context.get('source_turn_id')
            if source:
                row = self.store.one('SELECT consent_id FROM conversation_turns WHERE id=? AND profile_id=?',(source,pid))
                if row:
                    cids.add(row['consent_id'])
            for cid in cids:
                require_authorization(self.store, pid, 'openai', 'text_learning', cid)

    def analyze(self, transcript, context):
        self._check(context)
        return self.provider.analyze(transcript, context)

    def respond(self, context, messages):
        self._check(context)
        return self.provider.respond(context, messages)


def install_backend(app, store, actor, get_profile, current_consent):
    from .learning import LearningEngine
    from .providers import get_learning_provider, get_provider_status, ElevenLabsVoiceProvider, ProviderError, build_vapi_transport_config
    from .simulation import SimulationEngine
    from .voice import VoiceEngine

    model = AuthorizedLearningProvider(store, get_learning_provider())
    learning = LearningEngine(store, model)
    voice = VoiceEngine(store, ElevenLabsVoiceProvider())
    simulation = SimulationEngine(store, learning, model)
    app.state.learning = learning
    app.state.voice = voice
    app.state.simulation = simulation
    from .transport import TransportRegistry
    from .providers import VapiProvider
    transports=TransportRegistry(store,VapiProvider)
    app.state.transport_registry=transports

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return Response(json.dumps({'detail':exc.message,'code':exc.code}), status_code=exc.status_code, media_type='application/json')

    def access_profile(pid, user, write=False):
        row = get_profile(pid, user, owner_only=write)
        binding=enrollment_binding(store,pid)
        if binding:
            enrolled=store.one('SELECT owner_id,revoked_at FROM enrollment_sessions WHERE id=?',(binding['enrollment_id'],))
            if not enrolled or enrolled['owner_id']!=user['id']:
                raise HTTPException(403,'Enrollment learning is owner-only.')
            if enrolled['revoked_at'] or binding.get('revoked_at'):
                raise HTTPException(410,'Enrollment consent was withdrawn.')
        if row['owner_id'] != user['id']:
            current_consent(pid)
        return row

    def session(sid, user, write=False):
        row = store.one('SELECT * FROM teaching_sessions WHERE id=?',(sid,))
        if not row:
            raise HTTPException(404, 'Teaching session not found.')
        access_profile(row['profile_id'], user, write)
        owner=store.one('SELECT owner_id FROM profiles WHERE id=?',(row['profile_id'],))
        if owner['owner_id']!=user['id'] and not consent_active(store,row['consent_id']):
            raise HTTPException(409,'Session source consent is withdrawn.')
        if write:
            current_consent(row['profile_id'])
            if not consent_active(store,row['consent_id']):
                raise HTTPException(409,'Session source consent is withdrawn; start a new session.')
        return row

    def turn(tid, user, write=False):
        row = store.one('SELECT * FROM conversation_turns WHERE id=?',(tid,))
        if not row:
            raise HTTPException(404,'Conversation turn not found.')
        session(row['session_id'], user, write)
        return row

    def learning_events(sid, result):
        if result.get('idempotent'):
            return
        for observation in result.get('observations',[]):
            if not store.one("SELECT id FROM session_events WHERE type='learning.observation_created' AND json_extract(payload_json,'$.observation_id')=?",(observation['id'],)):
                record_event(store,sid,'learning.observation_created',{'observation_id':observation['id']})
        for hypothesis in result.get('hypotheses',[]):
            record_event(store,sid,'learning.hypothesis_updated',{'hypothesis_id':hypothesis['id']})
        version = result.get('profile_version')
        if version:
            if not store.one("SELECT id FROM session_events WHERE type='learning.profile_version_created' AND json_extract(payload_json,'$.profile_version_id')=?",(version['id'],)):
                record_event(store,sid,'learning.profile_version_created',{'profile_version_id':version['id']})

    from .jobs import LearningJobs
    jobs=LearningJobs(store,learning,learning_events)
    app.state.learning_jobs=jobs

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def backend_lifespan(application):
        import asyncio
        import threading
        voice.recover_pending_clones()
        stopped=threading.Event()
        recovery=asyncio.create_task(asyncio.to_thread(jobs.recover,stopped))
        transport_recovery=asyncio.create_task(asyncio.to_thread(transports.recover))
        try:
            yield
        finally:
            stopped.set()
            try:
                await asyncio.wait_for(asyncio.shield(recovery),timeout=2)
            except asyncio.TimeoutError:
                # Claimed jobs retain durable receipts for the next startup.
                recovery.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(transport_recovery),timeout=2)
            except asyncio.TimeoutError:
                transport_recovery.cancel()

    app.router.lifespan_context=backend_lifespan

    def append_turn(sid, body, user):
        current = session(sid,user,True)
        metadata=dict(body.provider_metadata)
        metadata['capture_mode']=current['mode']
        metadata['split']=current['split']
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM conversation_turns WHERE session_id=? AND external_id=?',(sid,body.external_id)).fetchone() if body.external_id else None
            if existing:
                original_metadata=json.loads(existing['provider_metadata_json'])
                comparison_metadata={k:v for k,v in original_metadata.items() if k not in ('capture_mode','split')}
                requested_metadata={k:v for k,v in body.provider_metadata.items() if k not in ('capture_mode','split')}
                same = (existing['role']==body.role and existing['transcript']==body.transcript and existing['transcript_state']==body.transcript_state and existing['audio_id']==body.audio_id and comparison_metadata==requested_metadata and (body.turn_index is None or existing['turn_index']==body.turn_index))
                if not same:
                    raise HTTPException(409,'Idempotency key was used for a different turn.')
                return decode_turn(existing)
            live = db.execute('SELECT * FROM teaching_sessions WHERE id=?',(sid,)).fetchone()
            consent = db.execute('SELECT * FROM consents WHERE profile_id=? ORDER BY rowid DESC LIMIT 1',(current['profile_id'],)).fetchone()
            source = db.execute('SELECT * FROM consents WHERE id=?',(live['consent_id'],)).fetchone()
            if live['status']!='active' or not consent or consent['withdrawn_at'] or not consent['collection'] or not source or source['withdrawn_at']:
                raise HTTPException(409,'Session is ended or consent is no longer active.')
            if body.audio_id:
                audio = db.execute('SELECT * FROM audio WHERE id=? AND profile_id=?',(body.audio_id,current['profile_id'])).fetchone()
                if not audio:
                    raise HTTPException(422,'Recording does not belong to this profile.')
                ac = db.execute('SELECT * FROM consents WHERE id=?',(audio['consent_id'],)).fetchone()
                if not ac or ac['withdrawn_at'] or not ac['collection']:
                    raise HTTPException(409,'Audio source consent is withdrawn.')
                if body.role!='trainer':
                    raise HTTPException(422,'Trainer source audio cannot be attached to a generated turn.')
                require_audio_partition(db,audio['sha256'],live['split'])
            index=db.execute('SELECT COALESCE(MAX(turn_index),-1)+1 FROM conversation_turns WHERE session_id=?',(sid,)).fetchone()[0]
            if body.turn_index is not None and body.turn_index!=index:
                raise HTTPException(409,f'Next turn_index is {index}.')
            tid=uid(); stamp=now()
            metadata['capture_mode']=live['mode']
            db.execute('INSERT INTO conversation_turns(id,session_id,profile_id,consent_id,turn_index,role,transcript,transcript_state,audio_id,started_at,ended_at,provider_metadata_json,external_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(tid,sid,current['profile_id'],consent['id'],index,body.role,body.transcript,body.transcript_state,body.audio_id,body.started_at.isoformat() if body.started_at else stamp,body.ended_at.isoformat() if body.ended_at else stamp,json.dumps(metadata,ensure_ascii=False),body.external_id,stamp))
            record_event(store,sid,'trainer.turn_completed' if body.role=='trainer' else 'raneen.turn_completed',{'turn_id':tid,'turn_index':index},db)
        store.audit(user['id'],'conversation_turn_created',tid,{'session_id':sid,'role':body.role})
        result=decode_turn(store.one('SELECT * FROM conversation_turns WHERE id=?',(tid,)))
        if body.role=='trainer' and body.audio_id:
            sample=voice.ingest_turn(tid,actor_id=user['id'])
            if sample:
                result['voice_sample']=sample
                record_event(store,sid,'voice.sample_rejected' if sample.get('eligibility')=='rejected' else 'voice.sample_accepted',{'sample_id':sample['id'],'eligibility':sample.get('eligibility')})
        return result

    @app.get('/api/backend/status')
    def backend_status(user=Depends(actor)):
        return {'schema_version':'raneen-backend-v1','providers':get_provider_status(),'capabilities':['sessions','learning','corrections','profile_versions','simulation','evaluation','voice_candidates','realtime_adapter'],'production_calls_enabled':False}

    @app.get('/api/backend/schema')
    def backend_schema(user=Depends(actor)):
        return app.openapi()

    @app.post('/api/profiles/{pid}/provider-authorizations',status_code=201)
    def authorize(pid:str,body:AuthorizationInput,user=Depends(actor)):
        access_profile(pid,user,True); consent=current_consent(pid)
        ident=uid()
        store.execute('INSERT INTO provider_authorizations VALUES(?,?,?,?,?,?,?,NULL)',(ident,pid,consent['id'],body.provider,body.scope,1,now()))
        store.audit(user['id'],'provider_authorization_granted',pid,{'authorization_id':ident,'provider':body.provider,'scope':body.scope,'consent_id':consent['id']})
        return {'id':ident,'provider':body.provider,'scope':body.scope,'consent_id':consent['id']}

    @app.get('/api/profiles/{pid}/provider-authorizations')
    def authorizations(pid:str,user=Depends(actor)):
        access_profile(pid,user)
        return {'items':store.all('SELECT * FROM provider_authorizations WHERE profile_id=? ORDER BY created_at',(pid,))}

    @app.post('/api/profiles/{pid}/provider-authorizations/{aid}/withdraw')
    def revoke_authorization(pid:str,aid:str,user=Depends(actor)):
        access_profile(pid,user,True)
        if not store.one('SELECT id FROM provider_authorizations WHERE id=? AND profile_id=?',(aid,pid)):
            raise HTTPException(404,'Provider authorization not found.')
        store.execute('UPDATE provider_authorizations SET withdrawn_at=COALESCE(withdrawn_at,?) WHERE id=?',(now(),aid))
        store.audit(user['id'],'provider_authorization_withdrawn',aid)
        return {'status':'withdrawn'}

    @app.post('/api/sessions',status_code=201)
    def create_session(body:SessionInput,user=Depends(actor)):
        access_profile(body.profile_id,user,True); consent=current_consent(body.profile_id)
        reject_legacy_write(store,body.profile_id)
        if body.scenario_id and body.scenario_id not in BY_ID:
            raise HTTPException(422,'Unknown scenario.')
        if body.scenario_id and BY_ID[body.scenario_id]['split']!=body.split:
            raise HTTPException(422,'Scenario and session partitions must match.')
        if body.split=='holdout' and not body.scenario_id:
            raise HTTPException(422,'Holdout sessions must identify their held-out scenario.')
        if body.split=='holdout' and body.mode!='teaching':
            raise HTTPException(422,'Use evaluation for generated holdout tests.')
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT * FROM teaching_sessions WHERE profile_id=? AND idempotency_key=?',(body.profile_id,body.idempotency_key)).fetchone() if body.idempotency_key else None
            if existing:
                if any(existing[k]!=getattr(body,k) for k in ('scenario_id','mode','split','realtime_provider')):
                    raise HTTPException(409,'Idempotency key was used for a different session.')
                return dict(existing)
            sid=uid();stamp=now()
            db.execute('INSERT INTO teaching_sessions(id,profile_id,consent_id,status,mode,split,scenario_id,realtime_provider,started_at,created_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(sid,body.profile_id,consent['id'],'active',body.mode,body.split,body.scenario_id,body.realtime_provider,stamp,stamp,body.idempotency_key))
            record_event(store,sid,'session.started',{'profile_id':body.profile_id,'mode':body.mode,'split':body.split},db)
        store.audit(user['id'],'teaching_session_created',sid)
        return store.one('SELECT * FROM teaching_sessions WHERE id=?',(sid,))

    @app.get('/api/profiles/{pid}/sessions')
    def sessions(pid:str,user=Depends(actor)):
        access_profile(pid,user)
        return {'items':store.all('SELECT * FROM teaching_sessions WHERE profile_id=? ORDER BY created_at DESC',(pid,))}

    @app.get('/api/sessions/{sid}')
    def read_session(sid:str,user=Depends(actor)):
        return session(sid,user)

    @app.post('/api/sessions/{sid}/end')
    def end_session(sid:str,user=Depends(actor)):
        row=session(sid,user)
        access_profile(row['profile_id'],user,True)
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed=db.execute("UPDATE teaching_sessions SET status='ended',ended_at=? WHERE id=? AND status='active'",(now(),sid)).rowcount
            if changed:
                record_event(store,sid,'session.ended',{},db)
        return store.one('SELECT * FROM teaching_sessions WHERE id=?',(sid,))

    @app.post('/api/sessions/{sid}/turns',status_code=201)
    def create_turn(sid:str,body:TurnInput,background:BackgroundTasks,user=Depends(actor)):
        result=append_turn(sid,body,user)
        job=jobs.enqueue(result['id']) if body.analyze else None
        if job:
            result['learning_job']={k:job[k] for k in ('id','status','attempts')}
            background.add_task(jobs.run,job['id'])
        return result

    @app.get('/api/sessions/{sid}/learning-jobs')
    def learning_jobs(sid:str,user=Depends(actor)):
        session(sid,user)
        rows=store.all('SELECT * FROM learning_jobs WHERE session_id=? ORDER BY created_at',(sid,))
        for row in rows:
            row['error']=json.loads(row.pop('error_json')) if row['error_json'] else None
        return {'items':rows}

    @app.post('/api/learning-jobs/{jid}/retry')
    def retry_job(jid:str,background:BackgroundTasks,user=Depends(actor)):
        job=store.one('SELECT * FROM learning_jobs WHERE id=?',(jid,))
        if not job:
            raise HTTPException(404,'Learning job not found.')
        session(job['session_id'],user,True)
        if job['status']=='running':
            raise HTTPException(409,'Learning job is already running.')
        background.add_task(jobs.run,jid)
        return {'id':jid,'status':job['status']}

    @app.post('/api/turns/{tid}/audio',status_code=201)
    def attach_audio(tid:str,body:AudioAttachmentInput,user=Depends(actor)):
        source=turn(tid,user,True)
        if source['role']!='trainer':
            raise HTTPException(422,'Only a trainer turn can receive original source audio.')
        audio=store.one('SELECT * FROM audio WHERE id=? AND profile_id=?',(body.audio_id,source['profile_id']))
        if not audio:
            raise HTTPException(422,'Recording does not belong to this profile.')
        if not consent_active(store,audio['consent_id']):
            raise HTTPException(409,'Recording source consent is withdrawn.')
        if source['audio_id'] and source['audio_id']!=body.audio_id:
            raise HTTPException(409,'Turn already has another original recording.')
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            capture_session=db.execute('SELECT split FROM teaching_sessions WHERE id=?',(source['session_id'],)).fetchone()
            require_audio_partition(db,audio['sha256'],capture_session['split'])
            existing=db.execute('SELECT * FROM turn_audio_attachments WHERE turn_id=?',(tid,)).fetchone()
            if existing and existing['audio_id']!=body.audio_id:
                raise HTTPException(409,'Turn already has another attached recording.')
            if not existing and not source['audio_id']:
                db.execute('INSERT INTO turn_audio_attachments VALUES(?,?,?,?,?,?)',(uid(),source['profile_id'],tid,body.audio_id,source['consent_id'],now()))
        sample=voice.ingest_turn(tid,actor_id=user['id'])
        store.audit(user['id'],'turn_audio_attached',tid,{'audio_id':body.audio_id})
        return {'turn_id':tid,'audio_id':body.audio_id,'sample':sample}

    @app.get('/api/sessions/{sid}/turns')
    def turns(sid:str,after_index:int=-1,limit:int=100,user=Depends(actor)):
        session(sid,user)
        if not 1<=limit<=500 or after_index < -1:
            raise HTTPException(422,'Invalid pagination.')
        return {'items':[decode_turn(r) for r in store.all('SELECT * FROM conversation_turns WHERE session_id=? AND turn_index>? ORDER BY turn_index LIMIT ?',(sid,after_index,limit))]}

    @app.get('/api/sessions/{sid}/events')
    def events(sid:str,after:int=0,limit:int=100,user=Depends(actor)):
        session(sid,user)
        if not 1<=limit<=500 or after<0:
            raise HTTPException(422,'Invalid event cursor.')
        rows=store.all('SELECT * FROM session_events WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?',(sid,after,limit))
        for row in rows:
            row['payload']=json.loads(row.pop('payload_json'))
        return {'items':rows,'cursor':rows[-1]['sequence'] if rows else after}

    @app.get('/api/profiles/{pid}/learning-state')
    def learning_state(pid:str,user=Depends(actor)):
        access_profile(pid,user,True); current_consent(pid)
        return learning.learning_state(pid)

    @app.get('/api/profiles/{pid}/versions')
    def versions(pid:str,user=Depends(actor)):
        access_profile(pid,user,True)
        return {'items':learning.versions(pid)}

    @app.post('/api/profiles/{pid}/learning/import-examples')
    def import_examples(pid:str,user=Depends(actor)):
        access_profile(pid,user,True);current_consent(pid)
        return learning.import_examples(pid)

    @app.get('/api/sessions/{sid}/next-question')
    def next_question(sid:str,user=Depends(actor)):
        row=session(sid,user,True)
        return learning.next_question(row['profile_id'],sid)

    @app.post('/api/turns/{tid}/analyze')
    def analyze(tid:str,user=Depends(actor)):
        row=turn(tid,user,True)
        result=learning.analyze_turn(tid)
        learning_events(row['session_id'],result)
        return result

    @app.post('/api/turns/{tid}/correct')
    def correct(tid:str,body:CorrectionInput,user=Depends(actor)):
        target=turn(tid,user,True); source=turn(body.source_turn_id,user,True)
        source_session=session(source['session_id'],user,True)
        if source_session['split']!='train' or BY_ID.get(source_session['scenario_id'],{}).get('split')=='holdout':
            raise HTTPException(409,'Holdout evidence cannot be normalized as a personal correction.')
        if target['role']!='raneen' or source['role']!='trainer' or source['session_id']!=target['session_id'] or source['turn_index']<=target['turn_index']:
            raise HTTPException(422,'Correction must cite a later trainer turn in the same session.')
        if body.rule is None:
            if body.lock:
                raise HTTPException(422,'Review a structured rule before locking it.')
            context=learning.compile_context(target['profile_id'])
            context.update(source_turn_id=source['id'],correction_target_transcript=target['transcript'],source_role='trainer',purpose='normalize_explicit_spoken_correction')
            sim=store.one('SELECT context_json FROM simulation_runs WHERE turn_id=?',(tid,))
            context['correction_target_rules']=json.loads(sim['context_json']).get('personal_rules',[]) if sim else context.get('personal_rules',[])
            proposals=model.analyze(source['transcript'],context)
            if not proposals:
                raise HTTPException(422,'I could not identify the correction. Explain the preferred response more specifically.')
            results=[]
            for proposal in proposals:
                rule={k:proposal[k] for k in ('category','key','value')}
                results.append(learning.record_correction(target['profile_id'],target['session_id'],tid,source['transcript'],rule,source_turn_id=source['id'],locked=False,actor_id=user['id']))
            result=results[-1]
            result['normalized_corrections']=[{k:p[k] for k in ('category','key','value')} for p in proposals]
        else:
            result=learning.record_correction(target['profile_id'],target['session_id'],tid,source['transcript'],body.rule.model_dump(),source_turn_id=source['id'],locked=body.lock,actor_id=user['id'])
        record_event(store,target['session_id'],'learning.correction_recorded',{'target_turn_id':tid,'source_turn_id':source['id']})
        learning_events(target['session_id'],result)
        return result

    @app.post('/api/hypotheses/{hid}/confirm')
    def confirm(hid:str,body:ConfirmationInput,user=Depends(actor)):
        hypothesis=store.one('SELECT * FROM hypotheses WHERE id=?',(hid,))
        if not hypothesis:
            raise HTTPException(404,'Hypothesis not found.')
        access_profile(hypothesis['profile_id'],user,True)
        source=turn(body.source_turn_id,user,True)
        if source['profile_id']!=hypothesis['profile_id']:
            raise HTTPException(422,'Confirmation source belongs to another profile.')
        return learning.confirm_hypothesis(hid,source['id'],locked=body.lock,actor_id=user['id'])

    @app.post('/api/sessions/{sid}/respond')
    def respond(sid:str,user=Depends(actor)):
        row=session(sid,user,True)
        if row['status']!='active' or row['split']!='train':
            raise HTTPException(409,'Responses require an active training session.')
        context=learning.compile_context(row['profile_id'])
        context['mode']=row['mode']
        context['next_question']=learning.next_question(row['profile_id'],sid)
        transcript=store.all('SELECT * FROM conversation_turns WHERE session_id=? ORDER BY turn_index',(sid,))
        if transcript and transcript[-1]['role']=='raneen':
            return {'turn':decode_turn(transcript[-1]),'profile_version_id':row['active_profile_version_id'],'replayed':True}
        if getattr(model,'mode','local_rules') not in ('local_rules','local'):
            for cid in {t['consent_id'] for t in transcript}:
                require_authorization(store,row['profile_id'],'openai','text_learning',cid)
        messages=[{'role':'user' if t['role']=='trainer' else 'assistant','content':t['transcript']} for t in transcript[-40:]]
        record_event(store,sid,'raneen.thinking')
        text=model.respond(context,messages)
        expected=(transcript[-1]['turn_index']+1) if transcript else 0
        result=append_turn(sid,TurnInput(role='raneen',transcript=text,turn_index=expected,external_id=f'response:{expected}'),user)
        pvid=context.get('profile_version_id')
        if pvid:
            store.execute('UPDATE teaching_sessions SET active_profile_version_id=? WHERE id=?',(pvid,sid))
        return {'turn':result,'profile_version_id':pvid,'provider_mode':getattr(model,'mode','local_rules')}

    @app.post('/api/sessions/{sid}/simulation',status_code=201)
    def start_simulation(sid:str,body:SimulationInput,user=Depends(actor)):
        row=session(sid,user,True)
        approved=voice.active_voice(row['profile_id'])
        store.execute('UPDATE teaching_sessions SET active_voice_version_id=? WHERE id=?',(approved['id'] if approved else None,sid))
        result=simulation.start(sid,body.scenario_id,body.caller_text)
        return result

    @app.post('/api/simulations/{simid}/retry',status_code=201)
    def retry_simulation(simid:str,body:RetryInput,user=Depends(actor)):
        row=store.one('SELECT * FROM simulation_runs WHERE id=?',(simid,))
        if not row:
            raise HTTPException(404,'Simulation not found.')
        session(row['session_id'],user,True)
        approved=voice.active_voice(row['profile_id'])
        store.execute('UPDATE teaching_sessions SET active_voice_version_id=? WHERE id=?',(approved['id'] if approved else None,row['session_id']))
        result=simulation.retry(simid,body.caller_text)
        return result

    @app.get('/api/simulations/{simid}')
    def read_simulation(simid:str,user=Depends(actor)):
        result=simulation.get(simid)
        session(result['session_id'],user)
        return result

    @app.get('/api/simulations/{simid}/audio')
    def simulation_audio(simid:str,user=Depends(actor)):
        result=simulation.get(simid)
        session(result['session_id'],user,True)
        if not result['voice_version_id']:
            raise HTTPException(409,'This simulation did not use an approved voice version.')
        return Response(voice.synthesize(result['voice_version_id'],result['response_text']),media_type='audio/mpeg')

    @app.post('/api/sessions/{sid}/resume-teaching')
    def resume_teaching(sid:str,user=Depends(actor)):
        row=session(sid,user,True)
        if row['status']!='active' or row['split']!='train':
            raise HTTPException(409,'Resume requires an active training session.')
        store.execute("UPDATE teaching_sessions SET mode='teaching' WHERE id=?",(sid,))
        record_event(store,sid,'simulation.ended',{'next_mode':'teaching'})
        return store.one('SELECT * FROM teaching_sessions WHERE id=?',(sid,))

    @app.post('/api/sessions/{sid}/reconnect')
    def reconnect(sid:str,user=Depends(actor)):
        row=session(sid,user,True)
        if row['status']!='active':
            raise HTTPException(409,'Ended teaching sessions cannot reconnect.')
        if row['provider_call_id'] and not store.one("SELECT id FROM session_events WHERE session_id=? AND type='provider.call_ended' AND json_extract(payload_json,'$.call_id')=?",(sid,row['provider_call_id'])):
            raise HTTPException(409,'Disconnect the current browser call before reconnecting.')
        store.execute('UPDATE teaching_sessions SET provider_call_id=NULL WHERE id=?',(sid,))
        record_event(store,sid,'session.reconnecting')
        return store.one('SELECT * FROM teaching_sessions WHERE id=?',(sid,))

    @app.post('/api/profiles/{pid}/evaluations',status_code=201)
    def evaluate(pid:str,body:VersionInput,user=Depends(actor)):
        access_profile(pid,user,True); current_consent(pid)
        return simulation.evaluate(pid,body.profile_version_id)

    @app.get('/api/profiles/{pid}/evaluations')
    def evaluations(pid:str,user=Depends(actor)):
        access_profile(pid,user,True)
        return {'items':simulation.evaluations(pid)}

    @app.get('/api/profiles/{pid}/voice/samples')
    def samples(pid:str,user=Depends(actor)):
        access_profile(pid,user)
        return {'items':voice.list_samples(pid),'versions':voice.list_versions(pid)}

    @app.post('/api/voice/samples/{sampleid}/review')
    def review_sample(sampleid:str,body:SampleReviewInput,user=Depends(actor)):
        row=store.one('SELECT profile_id FROM voice_samples WHERE id=?',(sampleid,))
        if not row:
            raise HTTPException(404,'Voice sample not found.')
        access_profile(row['profile_id'],user,True)
        return voice.review_sample(sampleid,user['id'],**body.model_dump())

    @app.post('/api/profiles/{pid}/voice/candidate',status_code=201)
    def candidate(pid:str,body:CandidateInput,user=Depends(actor)):
        access_profile(pid,user,True)
        if getattr(app.state,'enrollment_runtime',False):
            raise HTTPException(410,'Use the bounded voice-enrollment clone workflow.')
        return voice.create_candidate(pid,user['id'],idempotency_key=body.idempotency_key)

    def voice_access(vid,user,write=False):
        row=voice.get_version(vid)
        access_profile(row['profile_id'],user,write)
        return row

    @app.get('/api/voice/{vid}')
    def get_voice(vid:str,user=Depends(actor)):
        return voice_access(vid,user)

    @app.get('/api/voice/{vid}/preview')
    def preview(vid:str,user=Depends(actor)):
        voice_access(vid,user)
        return Response(voice.preview(vid),media_type='audio/mpeg')

    @app.post('/api/voice/{vid}/approve')
    def approve(vid:str,user=Depends(actor)):
        voice_access(vid,user,True)
        return voice.approve(vid,user['id'])

    @app.post('/api/voice/{vid}/reconcile')
    def reconcile_voice(vid:str,body:dict,user=Depends(actor)):
        # The durable receipt verifies its owner even after profile deletion,
        # allowing a late-found clone to enter scoped external cleanup.
        allowed={'provider_voice_id','confirmed_not_created','notes'}
        provider_id=body.get('provider_voice_id')
        absent=body.get('confirmed_not_created',False)
        notes=body.get('notes','')
        if set(body)-allowed or (provider_id is not None and (not isinstance(provider_id,str) or not 1<=len(provider_id)<=160)) or type(absent) is not bool or not isinstance(notes,str) or len(notes)>2000:
            raise HTTPException(422,'Invalid voice reconciliation request.')
        return voice.reconcile_clone(vid,user['id'],provider_voice_id=provider_id,confirmed_not_created=absent,notes=notes)

    @app.post('/api/voice/{vid}/reject')
    def reject(vid:str,body:RejectInput,user=Depends(actor)):
        voice_access(vid,user,True)
        return voice.reject(vid,user['id'],notes=body.notes)

    @app.get('/api/profiles/{pid}/runtime')
    def runtime(pid:str,user=Depends(actor)):
        access_profile(pid,user,True);current_consent(pid)
        return {'context':learning.compile_context(pid),'voice':voice.active_voice(pid),'production_calls_enabled':False}

    # The vendor endpoints use their own secret, never a contributor token.
    def provider_auth(authorization:str|None=Header(default=None)):
        configured=os.environ.get('RANEEN_VAPI_WEBHOOK_SECRET','')
        if len(configured)<32:
            raise HTTPException(503,'Vapi webhook authentication is not configured.')
        supplied=authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
        if not supplied or not secrets.compare_digest(supplied,configured):
            raise HTTPException(401,'Invalid provider webhook credential.')

    @app.get('/api/sessions/{sid}/transport',deprecated=True)
    @app.post('/api/sessions/{sid}/transport')
    def transport(sid:str,user=Depends(actor)):
        row=session(sid,user,True)
        if getattr(app.state,'enrollment_runtime',False):
            raise HTTPException(410,'Use the enrollment private preview call workflow.')
        if row['status']!='active' or row['split']!='train':
            raise HTTPException(409,'Transport requires an active training session.')
        require_authorization(store,row['profile_id'],'vapi','realtime_voice')
        require_authorization(store,row['profile_id'],'vapi','realtime_voice',row['consent_id'])
        if getattr(model,'mode','local_rules') not in ('local_rules','local'):
            require_authorization(store,row['profile_id'],'openai','text_learning')
        context=learning.compile_context(row['profile_id']);context['session_id']=sid
        active=voice.active_voice(row['profile_id'])
        config=build_vapi_transport_config(context,active['provider_voice_id'] if active else None)
        origin=os.environ.get('RANEEN_PUBLIC_ORIGIN') or ('https://'+os.environ['RAILWAY_PUBLIC_DOMAIN'] if os.environ.get('RAILWAY_PUBLIC_DOMAIN') else '')
        secret=os.environ.get('RANEEN_VAPI_WEBHOOK_SECRET','')
        if not origin or not app.state.hosted_staging or len(secret)<32:
            raise HTTPException(503,'Hosted origin and a strong provider webhook secret are required for realtime calls.')
        # Browser receives a server-created assistant ID, never private credentials.
        key=os.environ.get('VAPI_API_KEY') or os.environ.get('VAPI_PRIVATE_KEY','')
        if not key:
            raise HTTPException(503,'Vapi private key is not configured.')
        assistant=config.get('assistant',config.get('assistant_config',{}))
        assistant['server']={'url':origin+'/api/providers/vapi/webhook','headers':{'Authorization':'Bearer '+secret}}
        assistant['model']={'provider':'custom-llm','url':origin+'/api/providers/vapi/sessions/'+sid,'model':'raneen-personal-model','metadataSendMode':'variable','timeoutSeconds':45}
        assistant['credentials']=[{'provider':'custom-llm','apiKey':secret,'name':'Raneen session callback'}]
        assistant['metadata']={'raneen_session_id':sid}
        assistant_id=transports.provision(row,assistant)
        store.execute("UPDATE teaching_sessions SET realtime_provider='vapi' WHERE id=?",(sid,))
        store.audit(user['id'],'vapi_transport_created',sid,{'assistant_id':assistant_id})
        return {'provider':'vapi','public_key':os.environ.get('VAPI_PUBLIC_API_KEY') or os.environ.get('VAPI_PUBLIC_KEY',''),'assistant_id':assistant_id,'session_id':sid,'voice_version_id':active['id'] if active else None,'provider_mode':getattr(model,'mode','local_rules')}

    @app.post('/api/sessions/{sid}/transport/reconcile')
    def reconcile_transport(sid:str,body:dict,user=Depends(actor)):
        if getattr(app.state,'enrollment_runtime',False):
            raise HTTPException(410,'Use the enrollment provider-recovery workflow.')
        aid=body.get('assistant_id')
        if set(body)!={'assistant_id'} or not isinstance(aid,str) or not 1<=len(aid)<=160:
            raise HTTPException(422,'A provider assistant ID is required.')
        return transports.reconcile(sid,aid,user['id'])

    def vendor_session(sid,call=None):
        if call is not None and not isinstance(call,dict):
            raise HTTPException(422,'Invalid provider call.')
        if not isinstance(sid,str) or len(sid)>32:
            raise HTTPException(422,'Invalid realtime session id.')
        row=store.one('SELECT * FROM teaching_sessions WHERE id=?',(sid,))
        if not row or row['realtime_provider']!='vapi':
            raise HTTPException(404,'Realtime session not found.')
        if row['status']!='active' or row['split']!='train' or not consent_active(store,row['consent_id']):
            raise HTTPException(409,'Realtime session is not active.')
        require_authorization(store,row['profile_id'],'vapi','realtime_voice')
        require_authorization(store,row['profile_id'],'vapi','realtime_voice',row['consent_id'])
        callid=(call or {}).get('id')
        if callid is not None and (not isinstance(callid,str) or not 1<=len(callid)<=160):
            raise HTTPException(422,'Invalid provider call identifier.')
        if callid:
            if row['provider_call_id'] and row['provider_call_id']!=callid:
                raise HTTPException(409,'Provider call belongs to another session.')
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                bound=db.execute('SELECT provider_call_id FROM teaching_sessions WHERE id=?',(sid,)).fetchone()['provider_call_id']
                if bound and bound!=callid:
                    raise HTTPException(409,'Realtime session already has another call.')
                other=db.execute('SELECT id FROM teaching_sessions WHERE provider_call_id=? AND id!=?',(callid,sid)).fetchone()
                if other:
                    raise HTTPException(409,'Provider call belongs to another session.')
                db.execute('UPDATE teaching_sessions SET provider_call_id=? WHERE id=?',(callid,sid))
        profile=store.one('SELECT owner_id FROM profiles WHERE id=?',(row['profile_id'],))
        user=store.one('SELECT id,name,role FROM users WHERE id=?',(profile['owner_id'],))
        return row,user

    @app.post('/api/providers/vapi/webhook')
    def webhook(body:dict,background:BackgroundTasks,authorized=Depends(provider_auth)):
        message=body.get('message',body)
        if not isinstance(message,dict):
            raise HTTPException(422,'Invalid Vapi event.')
        call=message.get('call') or {}
        if not isinstance(call,dict):
            raise HTTPException(422,'Invalid provider call.')
        assistant=call.get('assistant') or {}
        if not isinstance(assistant,dict):
            raise HTTPException(422,'Invalid provider assistant.')
        metadata=call.get('metadata') or assistant.get('metadata') or {}
        if not isinstance(metadata,dict):
            raise HTTPException(422,'Invalid provider metadata.')
        sid=metadata.get('raneen_session_id')
        if not sid:
            raise HTTPException(422,'Vapi call must identify its Raneen session.')
        row,user=vendor_session(sid,call)
        event_type=message.get('type')

        def choose_ingestion_mode(mode):
            receipt_key='ingestion-mode:'+str(call.get('id',''))
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                receipt=db.execute('SELECT payload_hash FROM realtime_receipts WHERE session_id=? AND event_id=?',(sid,receipt_key)).fetchone()
                if receipt:
                    return receipt['payload_hash']==mode
                db.execute('INSERT INTO realtime_receipts VALUES(?,?,?,?,?)',(uid(),sid,receipt_key,mode,now()))
            return True

        if event_type=='conversation-update':
            artifact=message.get('artifact') or {}
            if not isinstance(artifact,dict):
                raise HTTPException(422,'Invalid conversation artifact.')
            history=message.get('messages',artifact.get('messages',[]))
            if not isinstance(history,list) or len(history)>300:
                raise HTTPException(422,'Invalid cumulative conversation history.')
            spoken=[]
            for item in history:
                if not isinstance(item,dict):
                    raise HTTPException(422,'Invalid conversation history item.')
                role=item.get('role')
                if role not in ('user','bot','assistant'):
                    continue
                text=item.get('message',item.get('content'))
                start,end=item.get('time'),item.get('endTime')
                if not isinstance(text,str) or not text.strip():
                    continue
                if len(text.strip())>6000:
                    raise HTTPException(422,'Conversation message exceeds 6000 characters.')
                if any(isinstance(t,bool) or not isinstance(t,(int,float)) or not math.isfinite(t) or t<0 for t in (start,end)):
                    continue
                if end<start:
                    raise HTTPException(422,'Conversation message ends before it starts.')
                spoken.append((start,end,'trainer' if role=='user' else 'raneen',text.strip()))
            if not spoken:
                return {'status':'pending_timed_history','accepted_turn_ids':[]}
            if not choose_ingestion_mode('snapshot'):
                return {'status':'ignored_other_ingestion_mode','accepted_turn_ids':[]}
            accepted=[]
            for start,end,role,text in sorted(spoken,key=lambda x:(x[0],x[1])):
                identity=str(call.get('id',''))+':'+role+':'+format(float(start),'.12g')+':'+format(float(end),'.12g')
                external='vapi:'+hashlib.sha256(identity.encode()).hexdigest()
                digest=hashlib.sha256(json.dumps({'identity':identity,'transcript':text},sort_keys=True).encode()).hexdigest()
                result=append_turn(sid,TurnInput(role=role,transcript=text,external_id=external,provider_metadata={'provider':'vapi','call_id':call.get('id'),'source_format':'snapshot','source_time':start,'source_end_time':end,'event_digest':digest}),user)
                job=jobs.enqueue(result['id'])
                if job:
                    background.add_task(jobs.run,job['id'])
                accepted.append(result['id'])
            return {'status':'accepted','accepted_turn_ids':accepted}

        if event_type=='transcript' or event_type in ('transcript[transcriptType="final"]',"transcript[transcriptType='final']"):
            transcript=message.get('transcript','')
            role=message.get('role')
            if role not in ('user','assistant') or not isinstance(transcript,str) or not transcript.strip():
                raise HTTPException(422,'Invalid transcript event.')
            if len(transcript.strip())>6000:
                raise HTTPException(422,'Transcript exceeds 6000 characters.')
            if message.get('transcriptType')!='final' and event_type=='transcript':
                return {'status':'ignored_partial'}
            # Call timestamp/explicit event ID is required for exactly-once finalization.
            eid=message.get('id') or message.get('timestamp')
            if eid is None:
                return {'status':'pending_timed_history'}
            if not isinstance(eid,(str,int,float)) or isinstance(eid,bool) or len(str(eid))>256:
                raise HTTPException(422,'Invalid provider event identifier.')
            external='vapi:'+hashlib.sha256((str(call.get('id',''))+':'+str(eid)+':'+role).encode()).hexdigest()
            if not choose_ingestion_mode('transcript'):
                return {'status':'ignored_other_ingestion_mode'}
            digest=hashlib.sha256(json.dumps({'call_id':call.get('id'),'role':role,'transcript':transcript,'event_id':eid},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            result=append_turn(sid,TurnInput(role='trainer' if role=='user' else 'raneen',transcript=transcript,external_id=external,provider_metadata={'provider':'vapi','call_id':call.get('id'),'source_format':'transcript','event_digest':digest}),user)
            job=jobs.enqueue(result['id'])
            if job:
                background.add_task(jobs.run,job['id'])
            return {'status':'accepted','turn_id':result['id']}
        if event_type=='status-update' and message.get('status')=='ended':
            record_event(store,sid,'provider.call_ended',{'call_id':call.get('id')})
            return {'status':'accepted','session_status':'active','note':'Transport ended; teaching session is preserved for reconnection.'}
        if event_type=='speech-update':
            role=message.get('role')
            et='trainer.speech_started' if role=='user' and message.get('status')=='started' else 'raneen.interrupted' if message.get('status')=='interrupted' else None
            if et:
                record_event(store,sid,et)
            return {'status':'accepted'}
        if event_type=='user-interrupted':
            record_event(store,sid,'raneen.interrupted')
            return {'status':'accepted'}
        return {'status':'ignored','type':event_type}

    @app.post('/api/providers/vapi/sessions/{sid}/chat/completions')
    def custom_llm(sid:str,body:dict,authorized=Depends(provider_auth)):
        row,user=vendor_session(sid,body.get('call'))
        items=body.get('messages',[])
        if not isinstance(items,list) or len(items)>80:
            raise HTTPException(422,'Invalid conversation messages.')
        messages=[]
        for item in items:
            if not isinstance(item,dict) or item.get('role') not in ('system','user','assistant') or not isinstance(item.get('content'),str) or len(item['content'])>6000:
                raise HTTPException(422,'Invalid conversation message.')
            if item['role']!='system':
                messages.append({'role':item['role'],'content':item['content']})
        context=learning.compile_context(row['profile_id']);context['mode']=row['mode'];context['next_question']=learning.next_question(row['profile_id'],sid)
        text=model.respond(context,messages)
        ident='chatcmpl-'+uid()
        if body.get('stream'):
            chunk={'id':ident,'object':'chat.completion.chunk','choices':[{'index':0,'delta':{'role':'assistant','content':text},'finish_reason':None}]}
            finish={'id':ident,'object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':'stop'}]}
            return StreamingResponse(iter(['data: '+json.dumps(chunk,ensure_ascii=False)+'\n\n','data: '+json.dumps(finish)+'\n\n','data: [DONE]\n\n']),media_type='text/event-stream')
        return {'id':ident,'object':'chat.completion','choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}],'model':'raneen-personal-model'}
