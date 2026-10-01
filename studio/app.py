from __future__ import annotations

import array
import contextlib
import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import uuid
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .scenarios import SCENARIOS, BY_ID

CONSENT_VERSION = 'internal-research-v0.1'
CONSENT_TEXT = (
    'I am the contributor, and these are my own words and recordings. I voluntarily '
    'allow storage and human review for this internal Raneen prototype. I will use '
    'fictional scenarios and avoid customer or third-party personal information. '
    'Behaviour-example export and voice-dataset export are separate optional scopes. '
    'This acknowledgement is not a commercial voice licence, identity verification, '
    'or permission for production impersonation. External processing and commercial '
    'deployment require separate reviewed authorization. Withdrawal blocks future '
    'exports here but cannot automatically recall downloaded copies or untrain models.'
)
MAX_AUDIO = 20 * 1024 * 1024
MAX_JSON = 96 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return uuid.uuid4().hex


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class NewUser(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    role: Literal['contributor', 'admin'] = 'contributor'


class Profile(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    dialect: str = Field(min_length=2, max_length=80)
    contribution: Literal['behavior', 'voice', 'both'] = 'both'
    style_notes: str = Field(default='', max_length=3000)


class Consent(StrictModel):
    collection: bool
    behavior_export: bool = False
    voice_export: bool = False
    version: Literal['internal-research-v0.1']
    self_attestation: bool


class Example(StrictModel):
    profile_id: str = Field(min_length=1, max_length=32)
    scenario_id: str = Field(min_length=1, max_length=80)
    session_id: str = Field(min_length=1, max_length=64)
    response_text: str = Field(min_length=1, max_length=6000)
    transcript_verified: bool = False
    action: Literal['answer', 'clarify', 'confirm', 'correct', 'handoff', 'respect_boundary']
    decision_cue: str = Field(min_length=1, max_length=2000)
    alternative: str = Field(default='', max_length=2000)
    change_condition: str = Field(min_length=1, max_length=2000)
    delivery: Literal['neutral', 'question', 'confirmation', 'reassuring', 'firm', 'other'] = 'neutral'
    pronunciation_notes: str = Field(default='', max_length=2000)
    audio_id: str | None = Field(default=None, max_length=32)


class Review(StrictModel):
    status: Literal['approved', 'rejected', 'pending']
    notes: str = Field(default='', max_length=2000)


class Preference(StrictModel):
    profile_id: str = Field(min_length=1, max_length=32)
    scenario_id: str = Field(min_length=1, max_length=80)
    candidate_a: str = Field(min_length=1, max_length=6000)
    candidate_b: str = Field(min_length=1, max_length=6000)
    preferred: Literal['a', 'b', 'neither']
    reason: str = Field(min_length=1, max_length=2000)
    replacement: str = Field(default='', max_length=6000)
    source: str = Field(min_length=1, max_length=120)


SCHEMA = '''
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,name TEXT NOT NULL,role TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(id),name TEXT NOT NULL,dialect TEXT NOT NULL,contribution TEXT NOT NULL,style_notes TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS consents(id TEXT PRIMARY KEY,profile_id TEXT NOT NULL REFERENCES profiles(id),version TEXT NOT NULL,collection INTEGER NOT NULL,behavior_export INTEGER NOT NULL,voice_export INTEGER NOT NULL,text TEXT NOT NULL,created TEXT NOT NULL,withdrawn_at TEXT);
CREATE TABLE IF NOT EXISTS audio(id TEXT PRIMARY KEY,profile_id TEXT NOT NULL REFERENCES profiles(id),consent_id TEXT NOT NULL REFERENCES consents(id),sha256 TEXT NOT NULL,stats TEXT NOT NULL,capture_settings TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS examples(id TEXT PRIMARY KEY,profile_id TEXT NOT NULL REFERENCES profiles(id),consent_id TEXT NOT NULL REFERENCES consents(id),payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',reviewer_id TEXT,review_notes TEXT NOT NULL DEFAULT '',created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS preferences(id TEXT PRIMARY KEY,profile_id TEXT NOT NULL REFERENCES profiles(id),consent_id TEXT NOT NULL REFERENCES consents(id),payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',reviewer_id TEXT,review_notes TEXT NOT NULL DEFAULT '',created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,actor_id TEXT,event TEXT NOT NULL,target_id TEXT,details TEXT NOT NULL,created TEXT NOT NULL);
'''


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.audio_dir = self.root / 'audio'
        self.audio_dir.mkdir(exist_ok=True)
        self.db_path = self.root / 'studio.sqlite3'
        with self.db() as db:
            db.executescript(SCHEMA)
        from .migrations import migrate
        migrate(self)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def one(self, sql, args=()):
        with self.db() as db:
            row = db.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.db() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.db() as db:
            db.execute(sql, args)

    def audit(self, actor, event, target=None, details=None):
        self.execute('INSERT INTO audit VALUES(?,?,?,?,?,?)', (uid(), actor, event, target, json.dumps(details or {}, ensure_ascii=False), now()))

    def create_user(self, name, role='contributor'):
        token = secrets.token_urlsafe(32)
        ident = uid()
        self.execute('INSERT INTO users VALUES(?,?,?,?,?)', (ident, name, role, token_hash(token), now()))
        return dict(id=ident, name=name, role=role, token=token)


def analyze_wav(data: bytes) -> dict:
    """Basic signal checks only. These do NOT assess dialect, intelligibility or accent."""
    if len(data) > MAX_AUDIO:
        raise ValueError('Recording exceeds 20 MB.')
    try:
        with wave.open(io.BytesIO(data), 'rb') as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getcomptype() != 'NONE':
                raise ValueError('Use mono, uncompressed 16-bit PCM WAV.')
            rate, frames = w.getframerate(), w.getnframes()
            if not 8000 <= rate <= 96000 or frames < 1 or frames / rate > 180:
                raise ValueError('Use a supported sample rate and a recording of at most 180 seconds.')
            raw = w.readframes(frames)
            if len(raw) != frames * 2:
                raise ValueError('The WAV audio is truncated.')
    except (wave.Error, EOFError) as exc:
        raise ValueError('Invalid PCM WAV recording.') from exc
    pcm = array.array('h')
    pcm.frombytes(raw)
    if sys.byteorder != 'little':
        pcm.byteswap()
    peak = max(abs(v) for v in pcm) / 32768
    rms = math.sqrt(sum((v / 32768) ** 2 for v in pcm) / len(pcm))
    clipped = sum(abs(v) >= 32700 for v in pcm) / len(pcm)
    flags = []
    if frames / rate < 1:
        flags.append('very_short')
    if rms < .003:
        flags.append('very_quiet_or_silence')
    if clipped > .001:
        flags.append('possible_clipping')
    return dict(sample_rate=rate, channels=1, bits_per_sample=16, duration_seconds=round(frames / rate, 3), peak_dbfs=round(20 * math.log10(max(peak, 1e-9)), 2), rms_dbfs=round(20 * math.log10(max(rms, 1e-9)), 2), clipped_fraction=round(clipped, 6), flags=flags, limitation='Signal heuristics only; no speech, dialect, noise or pronunciation accuracy score.')


def create_app(data_dir: Path | str | None = None, *, public_origin: str | None = None, owner_only: bool = False) -> FastAPI:
    # The local launcher stays loopback-only. Hosted mode requires a canonical HTTPS origin.
    public_host = None
    if public_origin:
        parsed = urlsplit(public_origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.port
                or '*' in parsed.netloc or any(c.isspace() for c in parsed.netloc)):
            raise ValueError('Hosted mode requires one HTTPS origin with no path, credentials, port, or wildcard.')
        public_host = parsed.hostname
        public_origin = 'https://' + public_host
    store = Store(Path(data_dir or os.environ.get('RANEEN_DATA_DIR', './data')))
    app = FastAPI(title='Raneen Teaching Studio', version='0.2.0', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    # No wildcard hosts; Railway's probe hostname is restricted to /healthz below.
    allowed_hosts = [public_host, 'healthcheck.railway.app'] if public_host else ['localhost', '127.0.0.1', '[::1]', 'testserver']
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.state.hosted_staging = bool(public_origin)
    app.state.owner_only = owner_only

    @app.middleware('http')
    async def protect(request: Request, call_next):
        from .tester_access import authorize_request
        path = request.url.path
        public_test_access = (path == '/api/testing/redeem' and request.method == 'POST') or (path == '/api/testing/access' and request.method == 'GET')
        account = None
        if path.startswith('/api/'):
            credential = request.headers.get('authorization', '')
            account = store.one('SELECT id,role FROM users WHERE token_hash=?', (token_hash(credential[7:]),)) if credential.startswith('Bearer ') and len(credential) <= 520 else None
            if account and account['role'] == 'tester':
                try:
                    authorize_request(store, account, path, request.method)
                except HTTPException as exc:
                    return Response(json.dumps({'detail': exc.detail}), status_code=exc.status_code, media_type='application/json', headers={'Cache-Control': 'no-store'})
        if public_origin:
            if request.headers.get('host', '') == 'healthcheck.railway.app' and request.url.path != '/healthz':
                return Response('Not found.', status_code=404)
            # Validate before FastAPI reads an API request body. Never put credentials in URLs.
            if request.url.path.startswith('/api/') and not request.url.path.startswith('/api/providers/vapi/') and not public_test_access:
                if not account:
                    return Response('Authentication required.', status_code=401, headers={'Cache-Control': 'no-store'})
                if owner_only and (account['role'] not in {'admin', 'tester'} or (request.url.path == '/api/users' and request.method == 'POST')):
                    return Response('Owner-only staging. Employee access is not enabled.', status_code=403, headers={'Cache-Control': 'no-store'})
        if request.method in ('POST','PUT','PATCH','DELETE'):
            # Provider-side evidence cannot be forged through legacy text APIs.
            parts=request.url.path.strip('/').split('/')
            target=None
            if len(parts)>=3 and parts[1]=='profiles':
                target=parts[2]
            elif len(parts)>=3 and parts[1] in ('sessions','turns','hypotheses','simulations','examples','preferences','learning-jobs'):
                table={'sessions':'teaching_sessions','turns':'conversation_turns','hypotheses':'hypotheses','simulations':'simulation_runs','examples':'examples','preferences':'preferences','learning-jobs':'learning_jobs'}[parts[1]]
                row=store.one(f'SELECT profile_id FROM {table} WHERE id=?',(parts[2],))
                target=row['profile_id'] if row else None
            elif len(parts)>=3 and parts[1]=='voice':
                table='voice_samples' if parts[2]=='samples' else 'voice_versions'
                ident=parts[3] if table=='voice_samples' and len(parts)>=4 else parts[2]
                row=store.one(f'SELECT profile_id FROM {table} WHERE id=?',(ident,))
                target=row['profile_id'] if row else None
            if target:
                from .backend import enrollment_binding
                if enrollment_binding(store,target):
                    credential=request.headers.get('authorization','')
                    account=store.one('SELECT id FROM users WHERE token_hash=?',(token_hash(credential[7:]),)) if credential.startswith('Bearer ') and len(credential)<=520 else None
                    if not account:
                        return Response('Authentication required.',status_code=401,headers={'Cache-Control':'no-store'})
                    owner=store.one('SELECT owner_id FROM profiles WHERE id=?',(target,))
                    if not owner or owner['owner_id']!=account['id']:
                        return Response('Enrollment learning is owner-only.',status_code=403,headers={'Cache-Control':'no-store'})
                    return Response(json.dumps({'detail':'Enrollment evidence is changed only through its trusted voice workflow.'}),status_code=409,media_type='application/json',headers={'Cache-Control':'no-store'})
        max_size = MAX_AUDIO if '/audio' in request.url.path else MAX_JSON
        if request.method == 'PUT' and re.fullmatch(r'/api/enrollment/sessions/[0-9a-f]{32}/chunks/[0-9]{1,5}', request.url.path):
            from .enrollment import CHUNK_MAX
            max_size = CHUNK_MAX
        length = request.headers.get('content-length')
        if request.method in ('POST', 'PUT', 'PATCH'):
            try:
                if length is None or int(length) < 0:
                    return Response('Content-Length required.', status_code=411)
                if int(length) > max_size:
                    return Response('Request too large.', status_code=413)
            except ValueError:
                return Response('Invalid Content-Length.', status_code=400)
        origin = request.headers.get('origin')
        if origin and origin != (public_origin or str(request.base_url).rstrip('/')):
            return Response('Cross-origin access is disabled.', status_code=403)
        response = await call_next(request)
        if public_origin:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        if request.url.path == '/vapi-frame':
            response.headers['Content-Security-Policy'] = ("default-src 'none'; script-src 'self' "
                "https://c.daily.co/call-machine/versioned/0.87.0/static/call-machine-object-bundle.js; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self' https://*.daily.co wss://*.daily.co; "
                "media-src 'self' blob: https://*.daily.co; worker-src 'self' blob:; "
                "frame-src https://*.daily.co; img-src data:; object-src 'none'; "
                "frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
        elif request.url.path == '/enroll':
            response.headers['Content-Security-Policy'] = ("default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
                "frame-src 'self'; worker-src 'self'; object-src 'none'; frame-ancestors 'none'; "
                "base-uri 'self'; form-action 'self'")
        else:
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; worker-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        response.headers['Permissions-Policy'] = 'microphone=(self), camera=(), geolocation=()'
        return response

    def actor(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Sign in using your private access token.')
        user = store.one('SELECT id,name,role FROM users WHERE token_hash=?', (token_hash(authorization[7:]),))
        if not user:
            raise HTTPException(401, 'Invalid access token.')
        return user

    def admin(user=Depends(actor)):
        if user['role'] != 'admin':
            raise HTTPException(403, 'Administrator access required.')
        return user

    def get_profile(ident, user, owner_only=False):
        profile = store.one('SELECT * FROM profiles WHERE id=?', (ident,))
        if not profile:
            raise HTTPException(404, 'Profile not found.')
        if profile['owner_id'] != user['id'] and (owner_only or user['role'] != 'admin'):
            raise HTTPException(403, 'This profile belongs to another contributor.')
        from .backend import enrollment_binding
        if enrollment_binding(store,ident):
            from .enrollment_learning import check_enrollment_binding
            check_enrollment_binding(store,ident,user['id'])
        return profile

    def current_consent(profile_id):
        consent = store.one('SELECT * FROM consents WHERE profile_id=? ORDER BY rowid DESC LIMIT 1', (profile_id,))
        if not consent or not consent['collection'] or consent['withdrawn_at']:
            raise HTTPException(409, 'Active contributor consent is required.')
        return consent

    def eligible(consent_id, scope):
        old = store.one('SELECT * FROM consents WHERE id=?', (consent_id,))
        return bool(old and old['collection'] and old[scope] and not old['withdrawn_at'])

    @app.get('/healthz', include_in_schema=False)
    def healthz():
        store.one('SELECT 1 AS ok')
        return dict(status='ok')

    @app.get('/readyz', include_in_schema=False)
    def readiness():
        """Public deployment diagnostics: configuration presence, never credentials/data."""
        revision = os.environ.get('RENDER_GIT_COMMIT', '')
        if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
            revision = None
        return {
            'application': 'raneen-owner-platform',
            'revision': revision,
            'enrollment_enabled': os.environ.get('RANEEN_VOICE_ENROLLMENT_ENABLED', '0').strip() == '1',
            'providers_configured': {
                'openai': bool(os.environ.get('OPENAI_API_KEY', '').strip()),
                'elevenlabs': bool(os.environ.get('ELEVENLABS_API_KEY', '').strip()),
                'vapi': bool(os.environ.get('VAPI_API_KEY', '').strip()),
            },
            'audio_decoder_available': bool(shutil.which('ffmpeg') and shutil.which('ffprobe')),
            'storage_available': shutil.disk_usage(store.root).free >= 256 * 1024 * 1024,
            'provider_access_verified': False,
        }

    @app.get('/api/me')
    def me(user=Depends(actor)):
        return user

    @app.post('/api/users', status_code=201)
    def add_user(body: NewUser, user=Depends(admin)):
        result = store.create_user(body.name, body.role)
        store.audit(user['id'], 'create_user', result['id'])
        return result

    @app.get('/api/consent-text')
    def consent_text(user=Depends(actor)):
        return dict(version=CONSENT_VERSION, text=CONSENT_TEXT)

    @app.get('/api/scenarios')
    def scenarios(user=Depends(actor)):
        return dict(items=SCENARIOS, provenance='Authored simulation prompts, not real customer data. Arabic needs native review.')

    @app.get('/api/profiles')
    def profiles(user=Depends(actor)):
        items = store.all('SELECT * FROM profiles' + ('' if user['role'] == 'admin' else ' WHERE owner_id=?') + ' ORDER BY created', () if user['role'] == 'admin' else (user['id'],))
        private_ids={row['profile_id'] for row in store.all('SELECT profile_id FROM enrollment_learning_bindings')}
        items=[item for item in items if item['owner_id']==user['id'] or item['id'] not in private_ids]
        for item in items:
            item['consent'] = store.one('SELECT id,version,collection,behavior_export,voice_export,withdrawn_at FROM consents WHERE profile_id=? ORDER BY rowid DESC LIMIT 1', (item['id'],))
        return dict(items=items)

    @app.post('/api/profiles', status_code=201)
    def add_profile(body: Profile, user=Depends(actor)):
        ident = uid()
        store.execute('INSERT INTO profiles VALUES(?,?,?,?,?,?,?)', (ident, user['id'], body.name, body.dialect, body.contribution, body.style_notes, now()))
        store.audit(user['id'], 'create_profile', ident)
        return dict(id=ident)

    @app.post('/api/profiles/{ident}/consent', status_code=201)
    def set_consent(ident: str, body: Consent, user=Depends(actor)):
        get_profile(ident, user, owner_only=True)
        if not body.self_attestation or not body.collection:
            raise HTTPException(422, 'Collection requires voluntary self-attestation. Use withdraw to stop collection.')
        consent_id = uid()
        store.execute('INSERT INTO consents VALUES(?,?,?,?,?,?,?,?,NULL)', (consent_id, ident, body.version, int(body.collection), int(body.behavior_export), int(body.voice_export), CONSENT_TEXT, now()))
        store.audit(user['id'], 'record_consent', ident, body.model_dump())
        return dict(id=consent_id)

    @app.post('/api/profiles/{ident}/withdraw')
    def withdraw(ident: str, user=Depends(actor)):
        get_profile(ident, user, owner_only=True)
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE consents SET withdrawn_at=? WHERE profile_id=? AND withdrawn_at IS NULL', (now(), ident))
            db.execute('UPDATE provider_authorizations SET withdrawn_at=? WHERE profile_id=? AND withdrawn_at IS NULL', (now(), ident))
            db.execute("UPDATE teaching_sessions SET status='ended',ended_at=? WHERE profile_id=? AND status='active'",(now(),ident))
        store.audit(user['id'], 'withdraw_consent', ident)
        transport_cleanup=app.state.transport_registry.cleanup(ident)
        return dict(status='withdrawn', external_transport_cleanup=transport_cleanup, note='New collection and exports are blocked. Previously downloaded copies and external models are not automatically recalled.')

    @app.delete('/api/profiles/{ident}')
    def delete_profile(ident: str, user=Depends(actor)):
        get_profile(ident, user, owner_only=True)
        audio_rows = store.all('SELECT id FROM audio WHERE profile_id=?', (ident,))
        external_cleanup=app.state.voice.delete_external_for_profile(ident)
        transport_cleanup=app.state.transport_registry.cleanup(ident)
        app.state.voice.delete_local_previews(ident)
        with store.db() as db:
            # New learning tables cascade from profiles. Clear them first so their
            # immutable evidence references never block deletion of source consent.
            for table in ('evaluation_runs','simulation_runs','agent_profile_versions','corrections','hypothesis_observations','hypotheses','learning_analyses','learning_jobs','observations','voice_versions','voice_samples','turn_audio_attachments','provider_authorizations','teaching_sessions'):
                if table == 'hypothesis_observations':
                    db.execute('DELETE FROM hypothesis_observations WHERE hypothesis_id IN (SELECT id FROM hypotheses WHERE profile_id=?)',(ident,))
                else:
                    db.execute(f'DELETE FROM {table} WHERE profile_id=?',(ident,))
            for table in ('examples', 'preferences', 'audio', 'consents'):
                db.execute(f'DELETE FROM {table} WHERE profile_id=?', (ident,))
            db.execute('DELETE FROM profiles WHERE id=?', (ident,))
        for row in audio_rows:
            (store.audio_dir / f"{row['id']}.wav").unlink(missing_ok=True)
        store.audit(user['id'], 'delete_local_profile', ident)
        return dict(status='deleted_locally', external_voice_cleanup=external_cleanup, external_transport_cleanup=transport_cleanup, note='External exports, device backups and filesystem remnants require separate handling.')

    @app.post('/api/profiles/{ident}/audio', status_code=201)
    async def upload_audio(ident: str, request: Request, user=Depends(actor)):
        get_profile(ident, user, owner_only=True)
        consent = current_consent(ident)
        if request.headers.get('content-type', '').split(';')[0] not in ('audio/wav', 'audio/x-wav'):
            raise HTTPException(415, 'Only PCM WAV uploads are accepted.')
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_AUDIO:
                raise HTTPException(413, 'Recording exceeds 20 MB.')
        try:
            stats = analyze_wav(bytes(data))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        capture = request.headers.get('x-capture-settings', '{}')
        if len(capture) > 4000:
            raise HTTPException(422, 'Capture metadata too large.')
        try:
            parsed = json.loads(capture)
            if not isinstance(parsed, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, 'Invalid capture settings.')
        audio_id = uid()
        path = store.audio_dir / f'{audio_id}.wav'
        fd, temp_name = tempfile.mkstemp(prefix='audio-', suffix='.tmp', dir=store.audio_dir)
        os.close(fd)
        temp_path = Path(temp_name)
        temp_path.write_bytes(data)
        finalized = False
        try:
            # Finalization and consent validation share one write transaction. A
            # withdrawal that commits first rejects this upload; if this transaction
            # commits first, the recording was accepted while consent was still active.
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                fresh = db.execute(
                    'SELECT collection,withdrawn_at FROM consents WHERE id=? AND profile_id=?',
                    (consent['id'], ident)).fetchone()
                if not fresh or not fresh['collection'] or fresh['withdrawn_at']:
                    raise HTTPException(409, 'Consent was withdrawn before this recording finalized.')
                os.replace(temp_path, path)
                finalized = True
                db.execute(
                    'INSERT INTO audio VALUES(?,?,?,?,?,?,?)',
                    (audio_id, ident, consent['id'], hashlib.sha256(data).hexdigest(),
                     json.dumps(stats), json.dumps(parsed), now()))
        except Exception:
            if finalized:
                path.unlink(missing_ok=True)
            raise
        finally:
            temp_path.unlink(missing_ok=True)
        store.audit(user['id'], 'upload_audio', audio_id)
        return dict(id=audio_id, stats=stats)

    @app.get('/api/audio/{ident}')
    def download_audio(ident: str, user=Depends(actor)):
        row = store.one('SELECT * FROM audio WHERE id=?', (ident,))
        if not row:
            raise HTTPException(404, 'Recording not found.')
        get_profile(row['profile_id'], user)
        if user['role'] == 'admin':
            profile = store.one('SELECT owner_id FROM profiles WHERE id=?', (row['profile_id'],))
            if profile['owner_id'] != user['id']:
                current_consent(row['profile_id'])
                source=store.one('SELECT collection,withdrawn_at FROM consents WHERE id=?',(row['consent_id'],))
                if not source or not source['collection'] or source['withdrawn_at']:
                    raise HTTPException(409,'Recording source consent is withdrawn.')
        return FileResponse(store.audio_dir / f'{ident}.wav', media_type='audio/wav')

    @app.post('/api/examples', status_code=201)
    def create_example(body: Example, user=Depends(actor)):
        from .backend import reject_legacy_write
        reject_legacy_write(store,body.profile_id)
        get_profile(body.profile_id, user, owner_only=True)
        consent = current_consent(body.profile_id)
        if body.scenario_id not in BY_ID:
            raise HTTPException(422, 'Unknown scenario.')
        if body.audio_id:
            audio = store.one('SELECT * FROM audio WHERE id=? AND profile_id=?', (body.audio_id, body.profile_id))
            if not audio:
                raise HTTPException(422, 'Recording does not belong to this profile.')
            from .backend import require_audio_partition
            with store.db() as db:
                require_audio_partition(db,audio['sha256'],BY_ID[body.scenario_id]['split'])
            # Never attach the same audio identity to multiple demonstrations.
            # This also prevents exact-audio leakage between training and holdout.
            for previous in store.all('SELECT payload FROM examples WHERE profile_id=?', (body.profile_id,)):
                previous_id = json.loads(previous['payload']).get('audio_id')
                if previous_id:
                    previous_audio = store.one('SELECT sha256 FROM audio WHERE id=?', (previous_id,))
                    if previous_audio and previous_audio['sha256'] == audio['sha256']:
                        raise HTTPException(409, 'This exact recording already belongs to a demonstration. Record a new response.')
        ident = uid()
        store.execute('INSERT INTO examples(id,profile_id,consent_id,payload,created) VALUES(?,?,?,?,?)', (ident, body.profile_id, consent['id'], body.model_dump_json(), now()))
        store.audit(user['id'], 'create_example', ident)
        return dict(id=ident, status='pending')

    @app.post('/api/preferences', status_code=201)
    def create_preference(body: Preference, user=Depends(actor)):
        from .backend import reject_legacy_write
        reject_legacy_write(store,body.profile_id)
        get_profile(body.profile_id, user, owner_only=True)
        consent = current_consent(body.profile_id)
        if body.scenario_id not in BY_ID:
            raise HTTPException(422, 'Unknown scenario.')
        ident = uid()
        store.execute('INSERT INTO preferences(id,profile_id,consent_id,payload,created) VALUES(?,?,?,?,?)', (ident, body.profile_id, consent['id'], body.model_dump_json(), now()))
        store.audit(user['id'], 'create_preference', ident)
        return dict(id=ident, status='pending')

    @app.get('/api/library')
    def library(profile_id: str, user=Depends(actor)):
        profile = get_profile(profile_id, user)
        if profile['owner_id'] != user['id']:
            current_consent(profile_id)
        result = {}
        for table in ('examples', 'preferences'):
            rows = store.all(f'SELECT * FROM {table} WHERE profile_id=? ORDER BY created DESC', (profile_id,))
            if profile['owner_id'] != user['id']:
                from .backend import consent_active
                rows=[r for r in rows if consent_active(store,r['consent_id'])]
            for row in rows:
                row['payload'] = json.loads(row['payload'])
                row['split'] = BY_ID[row['payload']['scenario_id']]['split']
                if table == 'examples' and row['payload'].get('audio_id'):
                    audio = store.one('SELECT stats FROM audio WHERE id=?', (row['payload']['audio_id'],))
                    row['audio_stats'] = json.loads(audio['stats']) if audio else None
            result[table] = rows
        return result

    @app.post('/api/{kind}/{ident}/review')
    def review(kind: Literal['examples', 'preferences'], ident: str, body: Review, user=Depends(admin)):
        row = store.one(f'SELECT * FROM {kind} WHERE id=?', (ident,))
        if not row:
            raise HTTPException(404, 'Example not found.')
        current_consent(row['profile_id'])
        payload = json.loads(row['payload'])
        if body.status == 'approved' and kind == 'examples' and not payload['transcript_verified']:
            raise HTTPException(409, 'A contributor-verified transcript is required before approval.')
        store.execute(f'UPDATE {kind} SET status=?,reviewer_id=?,review_notes=? WHERE id=?', (body.status, user['id'], body.notes, ident))
        store.audit(user['id'], 'review_' + kind, ident, body.model_dump())
        return dict(status=body.status)

    @app.get('/api/export/{profile_id}')
    def export(profile_id: str, kind: Literal['behavior', 'voice', 'all'] = 'behavior', user=Depends(admin)):
        profile = get_profile(profile_id, user)
        current = current_consent(profile_id)
        scopes = ['behavior_export'] if kind == 'behavior' else ['voice_export'] if kind == 'voice' else ['behavior_export', 'voice_export']
        if any(not current[scope] for scope in scopes):
            raise HTTPException(403, 'The contributor has not enabled all requested export scopes.')
        rows = store.all("SELECT * FROM examples WHERE profile_id=? AND status='approved' ORDER BY created", (profile_id,))
        behavior = {'train': [], 'holdout': []}
        voices = []
        for row in rows:
            p = json.loads(row['payload'])
            scenario = BY_ID[p['scenario_id']]
            if not p['transcript_verified']:
                continue
            if kind in ('behavior', 'all') and eligible(row['consent_id'], 'behavior_export'):
                behavior[scenario['split']].append(dict(schema_version='raneen-example-v1', id=row['id'], speaker_id=profile_id, dialect=profile['dialect'], family=scenario['family'], session_id=p['session_id'], context=scenario['context'], caller=scenario['caller'], response=p['response_text'], decision=dict(action=p['action'], cue=p['decision_cue'], rejected_alternative=p['alternative'], change_condition=p['change_condition']), delivery=p['delivery'], pronunciation_notes=p['pronunciation_notes'], source='human_demonstration', consent_id=row['consent_id']))
            if kind in ('voice', 'all') and p['audio_id'] and eligible(row['consent_id'], 'voice_export'):
                audio = store.one('SELECT * FROM audio WHERE id=? AND profile_id=?', (p['audio_id'], profile_id))
                if audio and eligible(audio['consent_id'], 'voice_export'):
                    stats = json.loads(audio['stats'])
                    # Obvious signal problems stay in review, not in a voice-training export.
                    if not stats['flags']:
                        voices.append(dict(id=row['id'], audio_id=audio['id'], transcript=p['response_text'], split=scenario['split'], family=scenario['family'], delivery=p['delivery'], sha256=audio['sha256'], signal=stats, capture_settings=json.loads(audio['capture_settings']), consent_id=audio['consent_id']))
        preferences = {'train': [], 'holdout': []}
        if kind in ('behavior', 'all'):
            for row in store.all("SELECT * FROM preferences WHERE profile_id=? AND status='approved'", (profile_id,)):
                if not eligible(row['consent_id'], 'behavior_export'):
                    continue
                p = json.loads(row['payload'])
                s = BY_ID[p['scenario_id']]
                preferences[s['split']].append(dict(id=row['id'], family=s['family'], context=s['context'], caller=s['caller'], **p))
        export_id = uid()
        manifest = dict(schema_version='raneen-dataset-v1', export_id=export_id, created=now(), profile_id=profile_id, dialect=profile['dialect'], contribution=profile['contribution'], kind=kind, counts=dict(training_examples=len(behavior['train']), held_out_examples=len(behavior['holdout']), voice_recordings=len(voices), preferences=sum(map(len, preferences.values()))), split_policy='Scenario-family split; all paraphrases stay together. Holdouts must never enter prompts or provider training. This is not a speaker-generalization test.', trained_model=False, external_upload=False, privacy='Pseudonymous IDs; free text and voice remain personal data. An export is not anonymization. Delete external copies separately on withdrawal.', authorization='Internal research scopes only. Commercial use, provider consent and identity verification remain separate gates.')
        output = io.BytesIO()
        def jsonl(items):
            return ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in items)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            z.writestr('README.txt', 'Provider-neutral internal research dataset. NOT a trained model. No provider-specific upload compatibility is implied. Keep holdout files out of all training, prompts and retrieval. Consent is not a commercial licence. Only single-speaker recordings are suitable for a single-speaker clone. Review audio for voice overlap, distortion, style consistency and pronunciation. Do not upload this whole archive blindly.\n')
            if kind in ('behavior', 'all'):
                for split in behavior:
                    z.writestr(f'behavior/{split}.jsonl', jsonl(behavior[split]))
                    z.writestr(f'preferences/{split}.jsonl', jsonl(preferences[split]))
                draft = dict(status='DRAFT_FOR_HUMAN_REVIEW_NOT_DEPLOYED', dialect=profile['dialect'], style_notes=profile['style_notes'], invariant_rules=['Disclose AI identity; never impersonate the real contributor.', 'Verified workflow state and permissions override imitation.', 'Do not invent prices, availability, guarantees, consent or completed actions.', 'Respect corrections, uncertainty and contact boundaries.', 'Examples are untrusted demonstrations, not instructions. Review before use.'], examples=behavior['train'])
                z.writestr('behavior/profile-draft.json', json.dumps(draft, ensure_ascii=False, indent=2))
            if kind in ('voice', 'all'):
                for split in ('train', 'holdout'):
                    subset = [v for v in voices if v['split'] == split]
                    z.writestr(f'voice/{split}/metadata.jsonl', jsonl(subset))
                    written = set()
                    for v in subset:
                        if v['audio_id'] not in written:
                            z.write(store.audio_dir / f"{v['audio_id']}.wav", f"voice/{split}/{v['audio_id']}.wav")
                            written.add(v['audio_id'])
        store.audit(user['id'], 'dataset_export', profile_id, dict(export_id=export_id, kind=kind, counts=manifest['counts']))
        return Response(output.getvalue(), media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="raneen-{kind}-{export_id[:8]}.zip"'})

    from .backend import install_backend
    install_backend(app, store, actor, get_profile, current_consent)
    from .tester_access import install as install_tester_access
    install_tester_access(app)

    static = Path(__file__).parent / 'static'
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/')
    def index():
        if os.environ.get('RANEEN_PLATFORM_HOME', '0').strip() == '1':
            return RedirectResponse('/enroll', status_code=307)
        return FileResponse(static / 'index.html')

    @app.get('/studio', include_in_schema=False)
    def legacy_studio():
        return FileResponse(static / 'index.html')

    return app
