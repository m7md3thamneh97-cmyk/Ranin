"""Owner-flow admission and real-codec regressions; providers stay synthetic."""
import asyncio
import hashlib
import io
import json
import math
import struct
import wave

import httpx
import pytest

from studio.app import now, uid
from studio.enrollment import ProviderError, Providers
from test_voice_enrollment import env, enable, start, auth, upload, synthetic_audio


def test_interview_budget_is_not_reset_by_a_new_connection(env,monkeypatch):
    c,app,owner,_,fake=env
    enable(monkeypatch);sid=start(c,owner)
    app.state.store.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)',(uid(),sid,'realtime_call','older','succeeded','rtc_old',json.dumps({'consumed_seconds':1795}),now(),now()))
    response=c.post(f'/api/enrollment/sessions/{sid}/webrtc',headers=auth(owner)|{'Content-Type':'application/sdp'},content='v=0\r\ns=test\r\n')
    assert response.status_code==429 and fake.secret_calls==0


def test_unknown_interview_blocks_another_paid_connection(env,monkeypatch):
    c,app,owner,_,fake=env
    enable(monkeypatch);sid=start(c,owner)
    app.state.store.execute('INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)',(sid,'reservation_unknown','outcome_unknown',now(),now()))
    response=c.post(f'/api/enrollment/sessions/{sid}/webrtc',headers=auth(owner)|{'Content-Type':'application/sdp'},content='v=0\r\ns=test\r\n')
    assert response.status_code==409 and fake.secret_calls==0


def test_clone_requires_the_final_acknowledged_sequence(env,monkeypatch):
    c,app,owner,_,fake=env
    enable(monkeypatch);sid=start(c,owner)
    for seq in range(4): assert upload(c,owner,sid,seq).status_code==200
    response=c.post(f'/api/enrollment/sessions/{sid}/clone',headers=auth(owner),json={'approve':True,'final_seq':4})
    assert response.status_code==409 and fake.clone_calls==0
    assert not app.state.store.all('SELECT * FROM enrollment_operations WHERE session_id=?',(sid,))


def test_clone_missing_key_does_not_reserve_or_decode(env,monkeypatch):
    c,app,owner,_,fake=env
    enable(monkeypatch);sid=start(c,owner)
    monkeypatch.delenv('ELEVENLABS_API_KEY')
    response=c.post(f'/api/enrollment/sessions/{sid}/clone',headers=auth(owner),json={'approve':True,'final_seq':0})
    assert response.status_code==503 and fake.clone_calls==0
    assert not app.state.store.all('SELECT * FROM enrollment_operations WHERE session_id=?',(sid,))


def test_provider_id_and_invalid_mp3_never_establish_voice_ready(env,monkeypatch):
    c,app,owner,_,fake=env
    enable(monkeypatch);sid=start(c,owner)
    app.state.store.execute("UPDATE enrollment_sessions SET voice_id='voice_test_123456',voice_state='sample_required' WHERE id=?",(sid,))
    async def invalid(*args): return b'ID3'+b'x'*1500
    fake.eleven_speech=invalid
    response=c.post(f'/api/enrollment/sessions/{sid}/preview',headers=auth(owner),json={'approve':True,'kind':'question'})
    assert response.status_code==502
    state=c.get(f'/api/enrollment/sessions/{sid}/workflow',headers=auth(owner)).json()
    assert state['voice_state']=='sample_required' and not state['voice_approved'] and not state['preview_allowed']


def mock_provider_http(monkeypatch, handler):
    """Exercise the real adapter while all outbound traffic stays in memory."""
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr('studio.enrollment.httpx.AsyncClient',
                        lambda **kwargs: real_client(transport=transport, **kwargs))


@pytest.mark.parametrize('status', [404, 410])
def test_hangup_of_known_ended_call_is_idempotent(monkeypatch, status):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == 'POST'
        assert request.url.path == '/v1/realtime/calls/rtc_synthetic_ended/hangup'
        return httpx.Response(status, json={'error': {'code': 'synthetic_gone'}})

    mock_provider_http(monkeypatch, respond)
    assert asyncio.run(Providers().openai_hangup('synthetic-test-key', 'rtc_synthetic_ended')) is None
    assert len(calls) == 1


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_hangup_unconfirmed_http_failure_keeps_uncertainty(monkeypatch, status):
    mock_provider_http(monkeypatch, lambda request: httpx.Response(status))
    with pytest.raises(ProviderError) as error:
        asyncio.run(Providers().openai_hangup('synthetic-test-key', 'rtc_synthetic_ended'))
    assert error.value.uncertain is True


def test_hangup_network_failure_keeps_uncertainty(monkeypatch):
    def fail(request):
        raise httpx.ConnectError('synthetic connection failure', request=request)

    mock_provider_http(monkeypatch, fail)
    with pytest.raises(ProviderError) as error:
        asyncio.run(Providers().openai_hangup('synthetic-test-key', 'rtc_synthetic_ended'))
    assert error.value.uncertain is True


def test_close_unknown_known_call_can_recover_after_provider_404(env, monkeypatch):
    client, app, owner, _, _ = env
    enable(monkeypatch)
    sid = start(client, owner)
    app.state.store.execute('INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)',
                            (sid, 'rtc_synthetic_ended', 'close_unknown', now(), now()))
    app.state.enrollment_provider = Providers()
    mock_provider_http(monkeypatch, lambda request: httpx.Response(404))
    before = client.get(f'/api/enrollment/sessions/{sid}/journey', headers=auth(owner)).json()
    assert before['can_resume'] is False
    closed = client.post(f'/api/enrollment/sessions/{sid}/webrtc-close', headers=auth(owner), json={})
    assert closed.status_code == 200 and closed.json()['state'] == 'closed'
    after = client.get(f'/api/enrollment/sessions/{sid}/journey', headers=auth(owner)).json()
    assert after['can_resume'] is True and after['provider_pending'] is False


@pytest.mark.parametrize('attempts,seconds,limit,can_resume', [
    (12, 300, None, True),
    (30, 300, 'connections', False),
    (1, 1800, 'time', False),
])
def test_interview_limit_projection_is_specific_and_nonsecret(env, monkeypatch, attempts, seconds, limit, can_resume):
    client, app, owner, _, fake = env
    enable(monkeypatch)
    sid = start(client, owner)
    for index in range(attempts):
        app.state.store.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)',
                                (uid(), sid, 'realtime_call', f'synthetic-attempt-{index}', 'succeeded',
                                 f'rtc_private_fixture_{index}', json.dumps({'consumed_seconds': seconds if index == 0 else 0,
                                                                          'private_test_detail': 'do-not-project-this'}), now(), now()))
    response = client.get(f'/api/enrollment/sessions/{sid}/journey', headers=auth(owner))
    assert response.status_code == 200
    data = response.json()
    assert data['interview_attempts_left'] == 30 - attempts
    assert data['interview_seconds_left'] == 1800 - seconds
    assert data['resume_limit'] == limit
    assert data['can_resume'] is can_resume
    assert 'rtc_private_fixture' not in response.text and 'do-not-project-this' not in response.text
    if not can_resume:
        denied = client.post(f'/api/enrollment/sessions/{sid}/webrtc', headers=auth(owner) | {'Content-Type': 'application/sdp'},
                             content='v=0\r\ns=synthetic\r\n')
        assert denied.status_code == 429 and fake.secret_calls == 0
    else:
        resumed = client.post(f'/api/enrollment/sessions/{sid}/webrtc', headers=auth(owner) | {'Content-Type': 'application/sdp'},
                              content='v=0\r\ns=synthetic\r\n')
        assert resumed.status_code == 200 and fake.secret_calls == 1


def test_chunk_between_old_and_new_size_limits_is_accepted(env, monkeypatch):
    client, app, owner, _, _ = env
    enable(monkeypatch)
    sid = start(client, owner)
    output = io.BytesIO()
    with wave.open(output, 'wb') as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(16000)
        sound.writeframes(b''.join(struct.pack('<h', round(4000 * math.sin(2 * math.pi * 220 * index / 16000)))
                                   for index in range(48000)))
    audio = output.getvalue()
    assert 80 * 1024 < len(audio) < 256 * 1024
    headers = auth(owner) | {'Content-Type': 'audio/wav', 'X-Speaker-Role': 'contributor',
                            'X-Chunk-Sha256': hashlib.sha256(audio).hexdigest(), 'X-Duration-Ms': '3000'}
    response = client.put(f'/api/enrollment/sessions/{sid}/chunks/0', headers=headers, content=audio)
    assert response.status_code == 200 and response.json()['seq'] == 0
    stored = app.state.store.one('SELECT byte_count,sha256 FROM enrollment_chunks WHERE session_id=? AND seq=0', (sid,))
    assert stored['byte_count'] == len(audio) and stored['sha256'] == hashlib.sha256(audio).hexdigest()


def test_chunk_above_256_kib_is_rejected_before_storage(env, monkeypatch):
    client, app, owner, _, _ = env
    enable(monkeypatch)
    sid = start(client, owner)
    audio = b's' * (256 * 1024 + 1)
    headers = auth(owner) | {'Content-Type': 'audio/webm', 'X-Speaker-Role': 'contributor',
                            'X-Chunk-Sha256': hashlib.sha256(audio).hexdigest(), 'X-Duration-Ms': '3000'}
    response = client.put(f'/api/enrollment/sessions/{sid}/chunks/0', headers=headers, content=audio)
    assert response.status_code == 413
    assert not app.state.store.all('SELECT * FROM enrollment_chunks WHERE session_id=?', (sid,))


def explicitly_consented_start(client, owner):
    return client.post('/api/enrollment/sessions', headers=auth(owner), json={
        'recording': True, 'external_processing': True, 'voice_cloning': True,
        'private_preview': True, 'self_attestation': True,
    })


@pytest.mark.parametrize('attempts,consumed_seconds', [(1, 1800), (30, 300)])
def test_new_consent_after_exhausted_closed_interview_preserves_previous_session(env, monkeypatch, attempts, consumed_seconds):
    client, app, owner, _, _ = env
    enable(monkeypatch)
    old_sid = start(client, owner)
    assert upload(client, owner, old_sid, 0, duration=1000).status_code == 200
    for index in range(attempts):
        app.state.store.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)',
                                (uid(), old_sid, 'realtime_call', f'finished-attempt-{index}', 'succeeded',
                                 f'rtc_finished_{index}', json.dumps({'consumed_seconds': consumed_seconds if index == 0 else 0}), now(), now()))
    app.state.store.execute('INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)',
                            (old_sid, 'rtc_finished_last', 'closed', now(), now()))
    before = app.state.store.one('SELECT * FROM enrollment_sessions WHERE id=?', (old_sid,))
    old_chunks = app.state.store.all('SELECT * FROM enrollment_chunks WHERE session_id=?', (old_sid,))
    old_operations = app.state.store.all('SELECT * FROM enrollment_operations WHERE session_id=? ORDER BY id', (old_sid,))

    response = explicitly_consented_start(client, owner)
    assert response.status_code == 201
    assert response.json()['resumed'] is False
    new_sid = response.json()['id']
    assert new_sid != old_sid
    assert app.state.store.one('SELECT * FROM enrollment_sessions WHERE id=?', (old_sid,)) == before
    assert app.state.store.all('SELECT * FROM enrollment_chunks WHERE session_id=?', (old_sid,)) == old_chunks
    assert app.state.store.all('SELECT * FROM enrollment_operations WHERE session_id=? ORDER BY id', (old_sid,)) == old_operations
    fresh = app.state.store.one('SELECT * FROM enrollment_sessions WHERE id=?', (new_sid,))
    assert fresh['owner_id'] == owner['id'] and fresh['state'] == 'collecting'
    assert fresh['voice_id'] is None and fresh['total_bytes'] == 0
    assert all(json.loads(fresh['consent_json'])[scope] for scope in
               ('recording', 'external_processing', 'voice_cloning', 'private_preview', 'self_attestation'))


@pytest.mark.parametrize('call_state', ['open', 'dispatching', 'close_unknown', 'outcome_unknown'])
def test_exhausted_interview_with_unresolved_call_cannot_create_replacement(env, monkeypatch, call_state):
    client, app, owner, _, _ = env
    enable(monkeypatch)
    old_sid = start(client, owner)
    app.state.store.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)',
                            (uid(), old_sid, 'realtime_call', 'exhausted-prior-call', 'succeeded',
                             'rtc_exhausted_prior', json.dumps({'consumed_seconds': 1800}), now(), now()))
    app.state.store.execute('INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)',
                            (old_sid, 'rtc_unresolved', call_state, now(), now()))
    before = app.state.store.one('SELECT * FROM enrollment_sessions WHERE id=?', (old_sid,))

    response = explicitly_consented_start(client, owner)
    assert response.status_code == 201
    assert response.json() == {'id': old_sid, 'resumed': True}
    assert app.state.store.one('SELECT * FROM enrollment_sessions WHERE id=?', (old_sid,)) == before
    assert app.state.store.one('SELECT COUNT(*) AS n FROM enrollment_sessions WHERE owner_id=?', (owner['id'],))['n'] == 1
    journey = client.get(f'/api/enrollment/sessions/{old_sid}/journey', headers=auth(owner)).json()
    assert journey['can_resume'] is False


@pytest.mark.parametrize('operation_kind', ['voice_clone', 'vapi_assistant'])
def test_exhausted_closed_interview_with_unknown_provider_operation_is_reused(env, monkeypatch, operation_kind):
    client, app, owner, _, _ = env
    enable(monkeypatch)
    old_sid = start(client, owner)
    app.state.store.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)',
                            (uid(), old_sid, 'realtime_call', 'exhausted-completed-call', 'succeeded',
                             'rtc_exhausted_closed', json.dumps({'consumed_seconds': 1800}), now(), now()))
    app.state.store.execute('INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)',
                            (old_sid, 'rtc_exhausted_closed', 'closed', now(), now()))
    app.state.store.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)',
                            (uid(), old_sid, operation_kind, 'uncertain-provider-operation', 'outcome_unknown',
                             None, '{}', now(), now()))
    before = app.state.store.one('SELECT * FROM enrollment_sessions WHERE id=?', (old_sid,))

    response = explicitly_consented_start(client, owner)
    assert response.status_code == 201
    assert response.json() == {'id': old_sid, 'resumed': True}
    assert app.state.store.one('SELECT * FROM enrollment_sessions WHERE id=?', (old_sid,)) == before
    assert app.state.store.one('SELECT COUNT(*) AS n FROM enrollment_sessions WHERE owner_id=?', (owner['id'],))['n'] == 1


@pytest.mark.parametrize('preview_state', ['open', 'dispatching', 'close_unknown', 'outcome_unknown'])
def test_exhausted_closed_interview_with_unresolved_preview_is_reused(env, monkeypatch, preview_state):
    client, app, owner, _, _ = env
    enable(monkeypatch)
    old_sid = start(client, owner)
    app.state.store.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)',
                            (uid(), old_sid, 'realtime_call', 'exhausted-completed-call', 'succeeded',
                             'rtc_exhausted_closed', json.dumps({'consumed_seconds': 1800}), now(), now()))
    app.state.store.execute('INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)',
                            (old_sid, 'rtc_exhausted_closed', 'closed', now(), now()))
    app.state.store.execute('INSERT INTO enrollment_preview_calls VALUES(?,?,?,?,?,?,?,?)',
                            (uid(), old_sid, 'synthetic-assistant', preview_state,
                             'synthetic-preview-call', None, now(), now()))

    response = explicitly_consented_start(client, owner)
    assert response.status_code == 201
    assert response.json() == {'id': old_sid, 'resumed': True}
    assert app.state.store.one('SELECT COUNT(*) AS n FROM enrollment_sessions WHERE owner_id=?', (owner['id'],))['n'] == 1


def test_exactly_ten_interview_seconds_remaining_reuses_existing_session(env, monkeypatch):
    client, app, owner, _, _ = env
    enable(monkeypatch)
    old_sid = start(client, owner)
    app.state.store.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)',
                            (uid(), old_sid, 'realtime_call', 'almost-completed-call', 'succeeded',
                             'rtc_almost_completed', json.dumps({'consumed_seconds': 1790}), now(), now()))
    app.state.store.execute('INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)',
                            (old_sid, 'rtc_almost_completed', 'closed', now(), now()))

    response = explicitly_consented_start(client, owner)
    assert response.status_code == 201
    assert response.json() == {'id': old_sid, 'resumed': True}
    assert app.state.store.one('SELECT COUNT(*) AS n FROM enrollment_sessions WHERE owner_id=?', (owner['id'],))['n'] == 1
    journey = client.get(f'/api/enrollment/sessions/{old_sid}/journey', headers=auth(owner)).json()
    assert journey['interview_seconds_left'] == 10
    assert journey['resume_limit'] is None and journey['can_resume'] is True
