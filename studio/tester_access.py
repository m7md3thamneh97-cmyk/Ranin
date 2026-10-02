"""Finite invited testing; credentials stay private and accounts stay isolated."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import re
import secrets
import shutil
import time

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .app import now, token_hash, uid

FEATURE = 'RANEEN_TESTER_ACCESS_ENABLED'
COHORT_LIMIT = 10
ACCESS_DAYS = 7
REDEEM_LIMIT = 30
REDEEM_WINDOW = 60
MIN_FREE_BYTES = 256 * 1024 * 1024
TESTER_STORAGE_LIMIT = 600 * 1024 * 1024


def enabled():
    return os.environ.get(FEATURE, '0').strip() == '1'


def _row(store, sql, args=(), db=None):
    row = db.execute(sql, args).fetchone() if db else store.one(sql, args)
    return dict(row) if row else None


def grant_state(store, user, *, db=None):
    grant = _row(store, 'SELECT * FROM tester_grants WHERE user_id=?', (user['id'],), db)
    if not grant or grant['revoked_at']:
        return 'revoked'
    if not enabled():
        return 'paused'
    return 'active' if grant['expires_at'] > now() else 'expired'


def require_active(store, user, *, db=None):
    if user['role'] != 'tester':
        return
    state = grant_state(store, user, db=db)
    if state != 'active':
        raise HTTPException(403, {'code': 'tester_access_' + state,
                                  'message': 'This test access no longer permits new processing. You can still stop or revoke your saved session.'})


def bound_call_seconds(store, db, user, maximum):
    if user['role'] != 'tester':
        return maximum
    require_active(store, user, db=db)
    grant = _row(store, 'SELECT expires_at FROM tester_grants WHERE user_id=?', (user['id'],), db)
    remaining = int((datetime.fromisoformat(grant['expires_at']) - datetime.now(timezone.utc)).total_seconds())
    if remaining < 10:
        raise HTTPException(403, {'code':'tester_access_expiring', 'message':'This test access is about to expire. You can still stop or revoke your saved session.'})
    return min(maximum, remaining)


def remaining_call_seconds(store, user, maximum):
    """Refresh after provider latency without obstructing internal cleanup."""
    if user['role'] != 'tester':
        return maximum
    grant = store.one('SELECT expires_at,revoked_at FROM tester_grants WHERE user_id=?', (user['id'],))
    if not grant or grant['revoked_at']:
        return 0
    remaining = int((datetime.fromisoformat(grant['expires_at']) - datetime.now(timezone.utc)).total_seconds())
    return max(0, min(maximum, remaining))


def _own_enrollment(store, user, ident):
    row = store.one('SELECT owner_id FROM enrollment_sessions WHERE id=?', (ident,))
    if not row or row['owner_id'] != user['id']:
        raise HTTPException(403, 'This enrollment is not available to this test account.')


def authorize_request(store, user, path, method):
    """An explicit tester route allowlist applies in local and hosted runtimes."""
    state = grant_state(store, user)
    if state == 'revoked':
        raise HTTPException(403, {'code': 'tester_access_revoked', 'message': 'This test access was revoked.'})
    recovery = False
    allowed = False
    if method == 'GET' and path in {'/api/me', '/api/testing/access', '/api/enrollment/readiness', '/api/enrollment/status', '/api/enrollment/sessions'}:
        allowed = recovery = True
    elif path == '/api/enrollment/consent' and method == 'GET':
        allowed = True
    elif path == '/api/enrollment/sessions' and method == 'POST':
        allowed = True
    else:
        match = re.fullmatch(r'/api/enrollment/sessions/([0-9a-f]{32})(?:/(.*))?', path)
        if match:
            ident, suffix = match.groups()
            _own_enrollment(store, user, ident)
            suffix = suffix or ''
            recovery = (method == 'GET' and suffix in {'', 'journey', 'workflow'}) or (method == 'POST' and suffix in {'webrtc-close', 'preview-call/close', 'revoke', 'cleanup'})
            allowed = recovery or (method == 'GET' and (suffix == 'quality' or bool(re.fullmatch(r'voice-samples(?:/(question|number|correction))?', suffix)))) or (method == 'PUT' and bool(re.fullmatch(r'chunks/[0-9]{1,5}', suffix))) or (method == 'POST' and suffix in {'webrtc', 'behavior', 'clone', 'preview', 'voice-approval', 'assistant', 'preview-call'})
        elif method == 'GET':
            match = re.fullmatch(r'/api/profiles/([0-9a-f]{32})/learning-state', path)
            session_match = re.fullmatch(r'/api/sessions/([0-9a-f]{32})/events', path)
            if match or session_match:
                column = 'profile_id' if match else 'teaching_session_id'
                ident = (match or session_match).group(1)
                binding = store.one(f'SELECT enrollment_id FROM enrollment_learning_bindings WHERE {column}=?', (ident,))
                if not binding:
                    raise HTTPException(403, 'This learning record is not available to this test account.')
                _own_enrollment(store, user, binding['enrollment_id'])
                allowed = True
    if not allowed:
        raise HTTPException(403, 'This test account can only use its own private enrollment.')
    if not recovery:
        require_active(store, user)


def admit_session(store, db, user):
    if user['role'] != 'tester':
        return
    require_active(store, user, db=db)
    if db.execute('SELECT id FROM enrollment_sessions WHERE owner_id=? LIMIT 1', (user['id'],)).fetchone():
        raise HTTPException(429, {'code': 'tester_session_limit', 'message': 'Your invitation includes one enrollment. Open your saved session to continue.'})
    total = db.execute("SELECT COUNT(*) AS n FROM enrollment_sessions s JOIN users u ON u.id=s.owner_id WHERE u.role='tester'").fetchone()['n']
    if total >= COHORT_LIMIT:
        raise HTTPException(429, {'code': 'tester_cohort_limit', 'message': 'The private test cohort is full.'})
    if shutil.disk_usage(store.root).free < MIN_FREE_BYTES:
        raise HTTPException(507, {'code': 'tester_storage_full', 'message': 'Testing is paused because recording storage is low.'})


def admit_storage(store, db, user, byte_count):
    if user['role'] != 'tester':
        return
    require_active(store, user, db=db)
    used = db.execute("SELECT COALESCE(SUM(s.total_bytes),0) AS n FROM enrollment_sessions s JOIN users u ON u.id=s.owner_id WHERE u.role='tester'").fetchone()['n']
    if used + byte_count > TESTER_STORAGE_LIMIT or shutil.disk_usage(store.root).free - byte_count < MIN_FREE_BYTES:
        raise HTTPException(507, {'code': 'tester_storage_full', 'message': 'Testing is paused because recording storage is low.'})


def admit_paid_session(store, db, session_id):
    """Called inside each durable paid-operation claim's immediate transaction."""
    account = _row(store, 'SELECT u.id,u.role FROM users u JOIN enrollment_sessions s ON s.owner_id=u.id WHERE s.id=?', (session_id,), db)
    if not account:
        raise HTTPException(404, 'Enrollment not found.')
    require_active(store, account, db=db)
    # Preserve legacy owner behavior when neither session belongs to a tester.
    condition = '' if account['role'] == 'tester' else " AND u.role='tester'"
    sources = (
        ("enrollment_realtime_calls", "state IN ('dispatching','open','close_unknown','outcome_unknown')"),
        ("enrollment_preview_calls", "state IN ('dispatching','open','close_unknown','outcome_unknown')"),
        ("enrollment_operations", "state IN ('dispatching','outcome_unknown') AND x.kind IN ('voice_clone','voice_preview','vapi_assistant')"),
    )
    for table, states in sources:
        busy = db.execute(f'SELECT x.session_id FROM {table} x JOIN enrollment_sessions s ON s.id=x.session_id JOIN users u ON u.id=s.owner_id WHERE x.session_id!=? AND x.{states}{condition} LIMIT 1', (session_id,)).fetchone()
        if busy:
            raise HTTPException(429, {'code': 'tester_capacity_busy', 'message': 'Another private test is using the voice connection. Please try again after it finishes.'})


class Redeem(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    code: str = Field(min_length=1, max_length=256)


class Empty(BaseModel):
    model_config = ConfigDict(extra='forbid')


def install(app):
    store = app.state.store

    def account(authorization):
        if not authorization or not authorization.startswith('Bearer ') or len(authorization) > 520:
            return None
        return store.one('SELECT id,name,role FROM users WHERE token_hash=?', (token_hash(authorization[7:]),))

    def owner(authorization):
        user = account(authorization)
        if not user:
            raise HTTPException(401, 'Sign in first.')
        if user['role'] != 'admin' or (app.state.hosted_staging and user['id'] != 'deployment-owner'):
            raise HTTPException(403, 'Only the workspace owner can manage test invitations.')
        return user

    def check_enabled():
        if not enabled():
            raise HTTPException(404, 'Invited testing is not enabled on this release.')

    @app.get('/api/testing/access')
    def access(authorization: str | None = Header(default=None)):
        user = account(authorization)
        is_owner = bool(user and user['role'] == 'admin' and (not app.state.hosted_staging or user['id'] == 'deployment-owner'))
        result = {'enabled': enabled(), 'owner': is_owner, 'role': user['role'] if user else None,
                  'can_invite': enabled() and is_owner, 'cohort_limit': COHORT_LIMIT}
        if user and user['role'] == 'tester':
            grant = store.one('SELECT expires_at FROM tester_grants WHERE user_id=?', (user['id'],))
            result.update(state=grant_state(store, user), expires_at=grant['expires_at'] if grant else None)
        return result

    @app.get('/api/testing/invitations')
    def invitations(authorization: str | None = Header(default=None)):
        owner(authorization)
        items = store.all('SELECT id,created,expires_at,redeemed_at,revoked_at FROM tester_invitations ORDER BY created DESC LIMIT 100')
        for item in items:
            item['redeemed'] = bool(item['redeemed_at'])
            item['revoked'] = bool(item['revoked_at'])
        return {'items': items, 'cohort_limit': COHORT_LIMIT}

    @app.post('/api/testing/invitations', status_code=201)
    def create_invitation(body: Empty, authorization: str | None = Header(default=None)):
        user = owner(authorization)
        check_enabled()
        ident, code, stamp = uid(), secrets.token_urlsafe(32), now()
        expiry = (datetime.now(timezone.utc) + timedelta(days=ACCESS_DAYS)).isoformat()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Redeemed grants never disappear, so revocation cannot reset the cohort.
            used = db.execute('SELECT COUNT(*) AS n FROM tester_grants').fetchone()['n']
            pending = db.execute('SELECT COUNT(*) AS n FROM tester_invitations WHERE redeemed_at IS NULL AND revoked_at IS NULL AND expires_at>?', (stamp,)).fetchone()['n']
            if used + pending >= COHORT_LIMIT:
                raise HTTPException(429, {'code': 'tester_cohort_limit', 'message': 'The ten-person private test cohort is full.'})
            db.execute('INSERT INTO tester_invitations(id,owner_id,code_hash,created,expires_at) VALUES(?,?,?,?,?)', (ident, user['id'], token_hash(code), stamp, expiry))
        store.audit(user['id'], 'tester_invitation_created', ident)
        return {'id': ident, 'code': code, 'created': stamp, 'expires_at': expiry}

    @app.post('/api/testing/redeem', status_code=201)
    def redeem(body: Redeem, request: Request):
        check_enabled()
        # Persistent, service-wide rate bound. No IP identifiers or raw codes are stored.
        clock = time.time()
        limited = False
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            limit = db.execute("SELECT * FROM tester_redemption_limits WHERE bucket='global'").fetchone()
            if not limit or clock - limit['window_started'] >= REDEEM_WINDOW:
                db.execute("INSERT INTO tester_redemption_limits VALUES('global',?,1) ON CONFLICT(bucket) DO UPDATE SET window_started=excluded.window_started,attempts=1", (clock,))
            elif limit['attempts'] >= REDEEM_LIMIT:
                limited = True
            else:
                db.execute("UPDATE tester_redemption_limits SET attempts=attempts+1 WHERE bucket='global'")
        if limited:
            raise HTTPException(429, {'code': 'tester_redemption_busy', 'message': 'Too many sign-in attempts. Please wait one minute.'}, headers={'Retry-After': '60'})
        token, user_id, stamp = secrets.token_urlsafe(32), uid(), now()
        expiry = (datetime.now(timezone.utc) + timedelta(days=ACCESS_DAYS)).isoformat()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            invite = db.execute('SELECT * FROM tester_invitations WHERE code_hash=?', (token_hash(body.code),)).fetchone()
            if not invite or invite['revoked_at'] or invite['redeemed_at'] or invite['expires_at'] <= stamp:
                raise HTTPException(401, {'code': 'tester_invitation_invalid', 'message': 'This invitation is invalid, expired, or already used. Use your saved personal access code to return.'})
            if db.execute('SELECT COUNT(*) AS n FROM tester_grants').fetchone()['n'] >= COHORT_LIMIT:
                raise HTTPException(429, {'code': 'tester_cohort_limit', 'message': 'The private test cohort is full.'})
            user = {'id': user_id, 'name': 'Private tester', 'role': 'tester'}
            db.execute('INSERT INTO users VALUES(?,?,?,?,?)', (user_id, user['name'], 'tester', token_hash(token), stamp))
            db.execute('INSERT INTO tester_grants(user_id,invitation_id,created,expires_at) VALUES(?,?,?,?)', (user_id, invite['id'], stamp, expiry))
            db.execute('UPDATE tester_invitations SET redeemed_at=?,user_id=? WHERE id=?', (stamp, user_id, invite['id']))
        store.audit(user_id, 'tester_invitation_redeemed', invite['id'])
        return {'token': token, 'user': user, 'expires_at': expiry}

    @app.post('/api/testing/invitations/{ident}/revoke')
    async def revoke_invitation(ident: str, body: Empty, authorization: str | None = Header(default=None)):
        user = owner(authorization)
        stamp = now()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            invite = db.execute('SELECT user_id FROM tester_invitations WHERE id=?', (ident,)).fetchone()
            if not invite:
                raise HTTPException(404, 'Invitation not found.')
            db.execute('UPDATE tester_invitations SET revoked_at=COALESCE(revoked_at,?) WHERE id=?', (stamp, ident))
            if invite['user_id']:
                db.execute('UPDATE tester_grants SET revoked_at=COALESCE(revoked_at,?) WHERE user_id=?', (stamp, invite['user_id']))
                db.execute("UPDATE enrollment_sessions SET state='revoked',revoked_at=COALESCE(revoked_at,?),updated=? WHERE owner_id=?", (stamp, stamp, invite['user_id']))
        store.audit(user['id'], 'tester_access_revoked', ident)
        cleanup = []
        if invite['user_id']:
            callback = getattr(app.state, 'revoke_tester_enrollments', None)
            if callback:
                cleanup = await callback(invite['user_id'])
        return {'id': ident, 'revoked': True, 'local_use_blocked': True, 'cleanup': cleanup}
