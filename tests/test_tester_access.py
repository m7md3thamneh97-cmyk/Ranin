"""Invited staging access and finite admission; all fixtures are synthetic."""
from concurrent.futures import ThreadPoolExecutor
import json
import secrets

from fastapi.testclient import TestClient
import pytest

from serve import make_app, OWNER_ID
from studio.app import now, token_hash, uid
from studio.runtime import create_app
from studio.tester_access import COHORT_LIMIT, REDEEM_LIMIT, TESTER_STORAGE_LIMIT

ORIGIN = 'https://studio.example.com'
SCOPES = {'recording': True, 'external_processing': True, 'voice_cloning': True,
          'private_preview': True, 'self_attestation': True}


def auth(token):
    return {'Authorization': 'Bearer ' + token}


@pytest.fixture
def testing(tmp_path, monkeypatch):
    monkeypatch.setenv('RANEEN_TESTER_ACCESS_ENABLED', '1')
    monkeypatch.setenv('RANEEN_VOICE_ENROLLMENT_ENABLED', '1')
    token = secrets.token_urlsafe(32)
    app = make_app(tmp_path, ORIGIN, token)
    return TestClient(app, base_url=ORIGIN), app, token


def invite(testing):
    client, _, token = testing
    result = client.post('/api/testing/invitations', headers=auth(token), json={})
    assert result.status_code == 201, result.text
    return result.json()


def redeem(testing, invitation=None):
    client, _, _ = testing
    invitation = invitation or invite(testing)
    response = client.post('/api/testing/redeem', json={'code': invitation['code']})
    assert response.status_code == 201, response.text
    return invitation, response.json()


def start(testing, account):
    client, _, _ = testing
    response = client.post('/api/enrollment/sessions', headers=auth(account['token']), json=SCOPES)
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_default_disabled_and_public_exemption_is_exact(testing, monkeypatch):
    client, _, token = testing
    monkeypatch.delenv('RANEEN_TESTER_ACCESS_ENABLED')
    assert client.get('/api/testing/access').json()['enabled'] is False
    assert client.post('/api/testing/invitations', headers=auth(token), json={}).status_code == 404
    assert client.post('/api/testing/redeem', json={'code': 'synthetic-invalid'}).status_code == 404
    assert client.get('/api/testing/redeem').status_code == 401
    assert client.post('/api/testing/access', json={}).status_code == 401
    assert client.post('/api/testing/redeem/extra', json={}).status_code == 401
    assert client.get('/api/enrollment/sessions').status_code == 401


def test_owner_only_issuance_preserves_existing_role_boundary(testing):
    client, app, token = testing
    for role in ('admin', 'contributor'):
        other = app.state.store.create_user('Synthetic other', role)
        assert client.post('/api/testing/invitations', headers=auth(other['token']), json={}).status_code == 403
    assert client.post('/api/users', headers=auth(token), json={'name': 'Synthetic employee'}).status_code == 403
    access = client.get('/api/testing/access', headers=auth(token)).json()
    assert access['owner'] and access['can_invite']


def test_one_use_invite_separate_private_credential_and_no_list_leaks(testing):
    client, app, token = testing
    invitation, tester = redeem(testing)
    assert tester['token'] != invitation['code']
    assert tester['user']['role'] == 'tester'
    stored = app.state.store.one('SELECT * FROM users WHERE id=?', (tester['user']['id'],))
    assert stored['token_hash'] == token_hash(tester['token'])
    row = app.state.store.one('SELECT * FROM tester_invitations WHERE id=?', (invitation['id'],))
    assert row['code_hash'] == token_hash(invitation['code'])
    assert row['user_id'] == tester['user']['id']
    listing = client.get('/api/testing/invitations', headers=auth(token)).json()
    assert listing['items'][0]['redeemed'] and not listing['items'][0]['revoked']
    assert invitation['code'] not in json.dumps(listing) and tester['token'] not in json.dumps(listing)
    assert client.post('/api/testing/redeem', json={'code': invitation['code']}).status_code == 401
    assert client.get('/api/me', headers=auth(tester['token'])).json() == tester['user']
    assert client.get('/api/testing/access', headers=auth(tester['token'])).json()['can_invite'] is False
    audit = app.state.store.all('SELECT * FROM audit')
    assert invitation['code'] not in json.dumps(audit) and tester['token'] not in json.dumps(audit)


def test_one_use_redemption_is_atomic(testing):
    client, app, _ = testing
    invitation = invite(testing)
    def exchange(_):
        return client.post('/api/testing/redeem', json={'code': invitation['code']}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(exchange, range(2))) == [201, 401]
    assert app.state.store.one('SELECT COUNT(*) AS n FROM tester_grants')['n'] == 1


def test_expired_and_revoked_invites_fail_closed(testing):
    client, app, token = testing
    old = invite(testing)
    app.state.store.execute("UPDATE tester_invitations SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (old['id'],))
    assert client.post('/api/testing/redeem', json={'code': old['code']}).status_code == 401
    revoked = invite(testing)
    assert client.post(f"/api/testing/invitations/{revoked['id']}/revoke", headers=auth(token), json={}).status_code == 200
    assert client.post('/api/testing/redeem', json={'code': revoked['code']}).status_code == 401
    assert app.state.store.one('SELECT COUNT(*) AS n FROM tester_grants')['n'] == 0


def test_persistent_redemption_rate_bound_and_origin_controls(testing):
    client, app, _ = testing
    invitation = invite(testing)
    # Origin validation happens before the unauthenticated request body endpoint.
    assert client.post('/api/testing/redeem', headers={'Origin':'https://evil.example.com'}, json={'code':invitation['code']}).status_code == 403
    assert client.post('/api/testing/redeem', content=b'x' * (96 * 1024 + 1)).status_code == 413
    assert app.state.store.one('SELECT * FROM tester_redemption_limits') is None
    for _ in range(REDEEM_LIMIT):
        assert client.post('/api/testing/redeem', json={'code':'synthetic-wrong'}).status_code == 401
    restart = make_app(app.state.store.root, ORIGIN, secrets.token_urlsafe(32))
    response = TestClient(restart, base_url=ORIGIN).post('/api/testing/redeem', json={'code':invitation['code']})
    assert response.status_code == 429 and response.headers['retry-after'] == '60'
    assert app.state.store.one('SELECT COUNT(*) AS n FROM tester_grants')['n'] == 0


def test_two_testers_keep_enrollment_and_learning_private(testing):
    client, app, owner = testing
    _, first = redeem(testing)
    _, other = redeem(testing)
    sid = start(testing, first)
    other_sid = start(testing, other)
    first_headers, other_headers = auth(first['token']), auth(other['token'])
    own = client.get('/api/enrollment/sessions', headers=first_headers).json()
    assert [s['id'] for s in own['sessions']] == [sid]
    assert [s['id'] for s in client.get('/api/enrollment/sessions', headers=other_headers).json()['sessions']] == [other_sid]
    journey = client.get(f'/api/enrollment/sessions/{sid}/journey', headers=first_headers).json()
    binding = journey['learning']
    for route in (f'/api/enrollment/sessions/{sid}', f'/api/enrollment/sessions/{sid}/workflow',
                  f"/api/profiles/{binding['profile_id']}/learning-state", f"/api/sessions/{binding['session_id']}/events"):
        assert client.get(route, headers=first_headers).status_code == 200
        assert client.get(route, headers=other_headers).status_code == 403
    assert client.get('/api/enrollment/sessions', headers=auth(owner)).json()['sessions'] == []
    assert app.state.store.one('SELECT owner_id FROM profiles WHERE id=?', (binding['profile_id'],))['owner_id'] == first['user']['id']


@pytest.mark.parametrize('method,path,body', [
    ('GET','/api/profiles',None), ('GET','/api/teaching/status',None),
    ('GET','/api/backend/schema',None), ('GET','/api/testing/invitations',None),
    ('POST','/api/users',{'name':'Synthetic malicious admin','role':'admin'}),
    ('POST','/api/profiles',{'name':'Synthetic other voice','dialect':'Arabic'}),
    ('POST','/api/testing/invitations',{}),
])
def test_testers_have_no_legacy_or_admin_privileges(testing, method, path, body):
    client, _, _ = testing
    _, tester = redeem(testing)
    kwargs = {'headers':auth(tester['token'])}
    if body is not None:
        kwargs['json'] = body
    assert client.request(method, path, **kwargs).status_code == 403


def test_local_runtime_enforces_same_tester_allowlist(testing):
    _, app, _ = testing
    _, tester = redeem(testing)
    local = TestClient(create_app(app.state.store.root))
    assert local.get('/api/profiles', headers=auth(tester['token'])).status_code == 403
    assert local.get('/api/me', headers=auth(tester['token'])).status_code == 200


def test_expired_grant_retains_own_stop_and_revoke_but_no_new_work(testing):
    client, app, _ = testing
    _, tester = redeem(testing)
    sid = start(testing, tester)
    headers = auth(tester['token'])
    assert client.get(f'/api/enrollment/sessions/{sid}/voice-samples', headers=headers).status_code == 200
    assert client.get(f'/api/enrollment/sessions/{sid}/voice-samples/question', headers=headers).status_code == 404
    app.state.store.execute("UPDATE tester_grants SET expires_at='2000-01-01T00:00:00+00:00' WHERE user_id=?", (tester['user']['id'],))
    assert client.get(f'/api/enrollment/sessions/{sid}/voice-samples', headers=headers).status_code == 403
    assert client.get(f'/api/enrollment/sessions/{sid}/voice-samples/question', headers=headers).status_code == 403
    assert client.get('/api/me', headers=headers).status_code == 200
    assert client.get('/api/testing/access', headers=headers).json()['state'] == 'expired'
    workflow = client.get(f'/api/enrollment/sessions/{sid}/workflow', headers=headers).json()
    assert workflow['enabled'] is False and workflow['preview_allowed'] is False
    assert client.get('/api/enrollment/sessions', headers=headers).json()['enabled'] is False
    journey = client.get(f'/api/enrollment/sessions/{sid}/journey', headers=headers).json()
    assert journey['enabled'] is False and journey['can_resume'] is False
    assert client.post('/api/enrollment/sessions', headers=headers, json=SCOPES).status_code == 403
    assert client.post(f'/api/enrollment/sessions/{sid}/behavior', headers=headers, json={'approve':True}).status_code == 403
    assert client.post(f'/api/enrollment/sessions/{sid}/webrtc-close', headers=headers, json={}).status_code == 200
    assert client.post(f'/api/enrollment/sessions/{sid}/revoke', headers=headers, json={'confirm':True}).status_code == 200


def test_feature_pause_retains_recovery_and_blocks_work(testing, monkeypatch):
    client, _, _ = testing
    _, tester = redeem(testing)
    sid = start(testing, tester)
    monkeypatch.setenv('RANEEN_TESTER_ACCESS_ENABLED', '0')
    headers = auth(tester['token'])
    assert client.get('/api/testing/access', headers=headers).json()['state'] == 'paused'
    assert client.get('/api/enrollment/sessions', headers=headers).status_code == 200
    assert client.post(f'/api/enrollment/sessions/{sid}/webrtc', headers=headers, content='v=0\r\n',).status_code == 403
    assert client.post(f'/api/enrollment/sessions/{sid}/revoke', headers=headers, json={'confirm':True}).status_code == 200


def test_owner_revocation_invalidates_token_and_revokes_linked_evidence(testing):
    client, app, owner = testing
    invitation, tester = redeem(testing)
    sid = start(testing, tester)
    result = client.post(f"/api/testing/invitations/{invitation['id']}/revoke", headers=auth(owner), json={})
    assert result.status_code == 200 and result.json()['local_use_blocked']
    assert client.get('/api/me', headers=auth(tester['token'])).status_code == 403
    assert client.get(f'/api/enrollment/sessions/{sid}/journey', headers=auth(tester['token'])).status_code == 403
    assert app.state.store.one('SELECT revoked_at FROM enrollment_sessions WHERE id=?', (sid,))['revoked_at']
    assert app.state.store.one('SELECT revoked_at FROM enrollment_learning_bindings WHERE enrollment_id=?', (sid,))['revoked_at']
    assert client.get('/api/me', headers=auth(owner)).json()['id'] == OWNER_ID
    assert client.post(f"/api/testing/invitations/{invitation['id']}/revoke", headers=auth(owner), json={}).status_code == 200


def test_one_lifetime_session_cannot_reset_after_revoke_or_exhaustion(testing):
    client, app, _ = testing
    _, tester = redeem(testing)
    sid = start(testing, tester)
    assert start(testing, tester) == sid
    for index in range(30):
        app.state.store.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)', (uid(),sid,'realtime_call',str(index),'succeeded','rtc_synthetic', '{}',now(),now()))
    assert client.post('/api/enrollment/sessions', headers=auth(tester['token']), json=SCOPES).status_code == 429
    assert client.post(f'/api/enrollment/sessions/{sid}/revoke', headers=auth(tester['token']), json={'confirm':True}).status_code == 200
    assert client.post('/api/enrollment/sessions', headers=auth(tester['token']), json=SCOPES).status_code == 429
    assert app.state.store.one('SELECT COUNT(*) AS n FROM enrollment_sessions WHERE owner_id=?', (tester['user']['id'],))['n'] == 1


def test_ten_person_cohort_is_finite_even_after_revoke(testing):
    client, app, owner = testing
    for _ in range(COHORT_LIMIT):
        invitation, _ = redeem(testing)
        assert client.post(f"/api/testing/invitations/{invitation['id']}/revoke", headers=auth(owner), json={}).status_code == 200
    response = client.post('/api/testing/invitations', headers=auth(owner), json={})
    assert response.status_code == 429
    assert app.state.store.one('SELECT COUNT(*) AS n FROM tester_grants')['n'] == COHORT_LIMIT


def test_owner_active_voice_blocks_tester_claim_without_provider_request(testing, monkeypatch):
    client, app, owner = testing
    _, tester = redeem(testing)
    sid = start(testing, tester)
    owner_id = start(testing, {'token':owner})
    app.state.store.execute('INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)', (owner_id,'rtc_synthetic_owner','open',now(),now()))
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-unused-key')
    class NeverCalled:
        async def openai_create_call(self, *args):
            pytest.fail('Capacity denial must happen before a provider call.')
    app.state.enrollment_provider = NeverCalled()
    result = client.post(f'/api/enrollment/sessions/{sid}/webrtc', headers=auth(tester['token']) | {'Content-Type':'application/sdp'}, content='v=0\r\ns=synthetic\r\n')
    assert result.status_code == 429 and result.json()['detail']['code'] == 'tester_capacity_busy'
    assert not app.state.store.one('SELECT id FROM enrollment_operations WHERE session_id=?', (sid,))


def test_paid_claim_admission_is_atomic_across_testers(testing):
    from studio.tester_access import admit_paid_session
    from fastapi import HTTPException
    _, app, _ = testing
    _, first = redeem(testing)
    _, second = redeem(testing)
    sessions = [start(testing, first), start(testing, second)]
    def claim(sid):
        try:
            with app.state.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                admit_paid_session(app.state.store, db, sid)
                db.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,NULL,?,?,?)', (uid(),sid,'voice_clone',uid(),'dispatching','{}',now(),now()))
            return 201
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, sessions)) == [201,429]


def test_aggregate_tester_audio_limit_preserves_owner_storage(testing):
    from studio.tester_access import admit_storage
    from fastapi import HTTPException
    _, app, owner = testing
    _, tester = redeem(testing)
    sid = start(testing, tester)
    app.state.store.execute('UPDATE enrollment_sessions SET total_bytes=? WHERE id=?', (TESTER_STORAGE_LIMIT, sid))
    with app.state.store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(HTTPException) as exc:
            admit_storage(app.state.store, db, tester['user'], 1)
        assert exc.value.status_code == 507
        admit_storage(app.state.store, db, {'id':OWNER_ID,'role':'admin'}, 1)


def test_paid_call_duration_cannot_outlive_grant(testing):
    from datetime import datetime, timedelta, timezone
    from studio.tester_access import bound_call_seconds
    from fastapi import HTTPException
    _, app, _ = testing
    _, tester = redeem(testing)
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=45)).isoformat()
    app.state.store.execute('UPDATE tester_grants SET expires_at=? WHERE user_id=?', (expiry, tester['user']['id']))
    with app.state.store.db() as db:
        assert 40 <= bound_call_seconds(app.state.store, db, tester['user'], 1800) <= 45
        assert bound_call_seconds(app.state.store, db, {'id':OWNER_ID,'role':'admin'}, 1800) == 1800
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    app.state.store.execute('UPDATE tester_grants SET expires_at=? WHERE user_id=?', (expiry, tester['user']['id']))
    with app.state.store.db() as db:
        with pytest.raises(HTTPException) as exc:
            bound_call_seconds(app.state.store, db, tester['user'], 180)
        assert exc.value.status_code == 403


def test_pre_tester_schema_upgrade_preserves_owner_and_enrollment(testing):
    client, app, token = testing
    sid = start(testing, {'token':token})
    store = app.state.store
    enrollment = store.one('SELECT * FROM enrollment_sessions WHERE id=?', (sid,))
    owner = store.one('SELECT * FROM users WHERE id=?', (OWNER_ID,))
    before = store.all("SELECT name,sha256 FROM schema_migrations WHERE name!='009_tester_access.sql' ORDER BY name")
    with store.db() as db:
        db.execute('DROP TABLE tester_grants')
        db.execute('DROP TABLE tester_invitations')
        db.execute('DROP TABLE tester_redemption_limits')
        db.execute("DELETE FROM schema_migrations WHERE name='009_tester_access.sql'")
    restarted = make_app(store.root, ORIGIN, token)
    upgraded = restarted.state.store
    assert upgraded.one('SELECT * FROM enrollment_sessions WHERE id=?', (sid,)) == enrollment
    assert upgraded.one('SELECT * FROM users WHERE id=?', (OWNER_ID,)) == owner
    assert upgraded.all("SELECT name,sha256 FROM schema_migrations WHERE name!='009_tester_access.sql' ORDER BY name") == before
    assert upgraded.one("SELECT sha256 FROM schema_migrations WHERE name='009_tester_access.sql'")
    assert upgraded.one('SELECT COUNT(*) AS n FROM tester_grants')['n'] == 0


def test_interview_expiring_during_provider_dispatch_closes_before_delivery(testing, monkeypatch):
    client, app, _ = testing
    _, tester = redeem(testing)
    sid = start(testing, tester)
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-unused-key')
    class Delayed:
        closed = False
        async def openai_create_call(self, *args):
            app.state.store.execute("UPDATE tester_grants SET expires_at='2000-01-01T00:00:00+00:00' WHERE user_id=?", (tester['user']['id'],))
            return {'call_id':'rtc_synthetic_expiry','sdp':'v=0\r\n'}
        async def openai_hangup(self, *args):
            self.closed = True
    provider = Delayed()
    app.state.enrollment_provider = provider
    async def attach(*args): pass
    app.state.enrollment_sideband.attach = attach
    response = client.post(f'/api/enrollment/sessions/{sid}/webrtc', headers=auth(tester['token']) | {'Content-Type':'application/sdp'}, content='v=0\r\ns=synthetic\r\n')
    assert response.status_code == 403
    assert response.json()['detail']['code'] == 'tester_access_expired'
    assert provider.closed
    assert app.state.store.one('SELECT state FROM enrollment_realtime_calls WHERE session_id=?', (sid,))['state'] == 'closed'


def test_preview_expiring_during_provider_dispatch_closes_before_delivery(testing, monkeypatch):
    client, app, _ = testing
    _, tester = redeem(testing)
    sid = start(testing, tester)
    monkeypatch.setenv('VAPI_API_KEY', 'synthetic-unused-key')
    voice = 'voice_synthetic_private'
    assistant = '11111111-1111-1111-1111-111111111111'
    behavior_id = uid()
    app.state.store.execute('UPDATE enrollment_sessions SET voice_id=?,voice_state=?,active_behavior_id=?,assistant_id=? WHERE id=?', (voice,'ready',behavior_id,assistant,sid))
    app.state.store.execute('INSERT INTO enrollment_voice_approvals VALUES(?,?,?)', (sid,voice,now()))
    app.state.store.execute('INSERT INTO enrollment_behavior_versions VALUES(?,?,?,?,?,?)', (behavior_id,sid,1,json.dumps({'evidence_origin':'trusted_audio_v1','evidence':[{'synthetic':True}]}),'synthetic-digest',now()))
    app.state.store.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)', (uid(),sid,'vapi_assistant','synthetic-operation','succeeded',assistant,json.dumps({'behavior_id':behavior_id}),now(),now()))
    class Delayed:
        stopped = False
        async def vapi_json(self, *args, **kwargs):
            app.state.store.execute("UPDATE tester_grants SET expires_at='2000-01-01T00:00:00+00:00' WHERE user_id=?", (tester['user']['id'],))
            return {'id':'22222222-2222-2222-2222-222222222222', 'webCallUrl':'https://synthetic.daily.co/test', 'monitor':{'controlUrl':'https://synthetic.vapi.ai/control'}}
        async def vapi_end_call(self, *args):
            self.stopped = True
    provider = Delayed()
    app.state.enrollment_provider = provider
    response = client.post(f'/api/enrollment/sessions/{sid}/preview-call', headers=auth(tester['token']), json={'approve':True})
    assert response.status_code == 403, response.text
    assert response.json()['detail']['code'] == 'tester_access_expired'
    assert provider.stopped
    assert app.state.store.one('SELECT state FROM enrollment_preview_calls WHERE session_id=?', (sid,))['state'] == 'closed'


@pytest.mark.parametrize('withdraw', ['expire','revoke'])
def test_inflight_speech_preview_is_never_delivered_after_access_loss(testing, monkeypatch, withdraw):
    client, app, _ = testing
    _, tester = redeem(testing)
    sid = start(testing, tester)
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'synthetic-unused-key')
    app.state.store.execute("UPDATE enrollment_sessions SET voice_id='voice_synthetic_private',voice_state='sample_required' WHERE id=?", (sid,))
    monkeypatch.setattr('studio.enrollment_audio.validate_synthesized_audio', lambda audio: {'synthetic':True})
    class Delayed:
        async def eleven_speech(self, *args):
            if withdraw == 'expire':
                app.state.store.execute("UPDATE tester_grants SET expires_at='2000-01-01T00:00:00+00:00' WHERE user_id=?", (tester['user']['id'],))
            else:
                app.state.store.execute("UPDATE enrollment_sessions SET revoked_at=?,state='revoked' WHERE id=?", (now(),sid))
            return b'ID3synthetic-private-speech'
    app.state.enrollment_provider = Delayed()
    response = client.post(f'/api/enrollment/sessions/{sid}/preview', headers=auth(tester['token']), json={'approve':True,'kind':'question'})
    assert response.status_code == (403 if withdraw == 'expire' else 410)
    assert b'ID3synthetic-private-speech' not in response.content
    operation = app.state.store.one("SELECT state FROM enrollment_operations WHERE session_id=? AND kind='voice_preview'", (sid,))
    assert operation['state'] == ('succeeded' if withdraw == 'expire' else 'cancelled')
