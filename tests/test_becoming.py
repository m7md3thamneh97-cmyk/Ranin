"""Synthetic microphone PCM and mocked providers; these tests do not prove a real clone."""
import asyncio
import hashlib
import io
import json
import math
import struct
import subprocess
import threading
import time
import wave
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from studio.becoming import apply_migration
from studio.becoming_providers import checked_provider_url, sanitized_assistant
from studio.enrollment import ProviderError
from studio.runtime import create_app


CALL_ID = '11111111-2222-3333-4444-555555555555'
VOICE_ID = 'synthetic_voice_12345'
CONSENT = {'consent_version': 'becoming-v1', 'own_voice': True, 'recording': True,
           'external_processing': True, 'voice_cloning': True, 'private_preview': True, 'language': 'en'}


@lru_cache(maxsize=4)
def wav(duration=3, silent=False):
    data = io.BytesIO()
    with wave.open(data, 'wb') as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000)
        handle.writeframes(b'\0\0' * round(24000 * duration) if silent else
                           b''.join(struct.pack('<h', int(math.sin(i * .07) * 4500)) for i in range(round(24000 * duration))))
    return data.getvalue()


@lru_cache(maxsize=1)
def synthesized():
    return subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                           'sine=frequency=440:sample_rate=24000:duration=1', '-f', 'mp3', 'pipe:1'],
                          capture_output=True, check=True).stdout


class Fake:
    def __init__(self):
        self.clone_count = 0; self.synth_count = 0; self.controls = []; self.deleted = []; self.ended = []
        self.calls = []; self.error = None; self.verification = False; self.block = None

    async def vapi_json(self, key, method, path, *, json_body=None):
        self.calls.append((method, path, json_body))
        if method == 'GET':
            return {'model': {'provider': 'openai', 'model': 'gpt-4.1-mini',
                              'messages': [{'role': 'system', 'content': 'Keep Dubai and Abu Dhabi buyer context.'}],
                              'tools': [{'type': 'transferCall'}], 'toolIds': ['unsafe-tool']},
                    'voice': {'provider': '11labs', 'voiceId': 'bootstrap_voice'},
                    'transcriber': {'provider': 'speechmatics', 'model': 'enhanced', 'language': 'ar_en'},
                    'server': {'url': 'https://production.example/', 'secret': 'DO-NOT-COPY'},
                    'credentialIds': ['DO-NOT-COPY']}
        return {'id': CALL_ID, 'transport': {'provider': 'daily', 'callUrl': 'https://private.daily.co/synthetic',
                                           'callToken': 'synthetic-room-capability'},
                'monitor': {'controlUrl': 'https://websocket.vapi.ai/synthetic/control'}}

    async def eleven_clone(self, key, name, files):
        self.clone_count += 1
        with wave.open(str(files[0][1]), 'rb') as handle:
            assert handle.getnframes() in (24000 * 30, 24000 * 45, 24000 * 60)
        if self.block:
            await self.block.wait()
        if self.error:
            raise self.error
        return {'voice_id': VOICE_ID, 'requires_verification': self.verification}

    async def eleven_speech(self, key, voice_id, text):
        assert voice_id == VOICE_ID
        self.synth_count += 1
        return synthesized()

    async def eleven_account_read(self, key):
        return {'account_read_verified': True, 'instant_voice_cloning_available': True}

    async def vapi_control(self, url, body):
        self.controls.append(body)

    async def vapi_end_call(self, url):
        self.ended.append(url)

    async def eleven_delete_voice(self, key, voice_id):
        self.deleted.append(voice_id)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for name, value in {'RANEEN_BECOMING_ENABLED': '1', 'VAPI_API_KEY': 'mock-vapi-private',
                        'ELEVENLABS_API_KEY': 'mock-eleven-private',
                        'RANEEN_VAPI_TEMPLATE_ID': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}.items():
        monkeypatch.setenv(name, value)
    app = create_app(tmp_path)
    fake = Fake(); app.state.becoming_provider = fake
    with TestClient(app) as client:
        yield app, client, fake


def start(client):
    response = client.post('/api/becoming/sessions', json=CONSENT)
    assert response.status_code == 201, response.text
    data = response.json()
    return data['id'], {'Authorization': 'Bearer ' + data['capability']}


def call(client, ident, headers):
    response = client.post(f'/api/becoming/sessions/{ident}/call', json={}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def upload(client, ident, headers, seq, *, overlap=False, eligibility=3000, source='isolated_microphone', data=None):
    data = wav() if data is None else data
    capture = {'source': source, 'speaker': 'user', 'assistant_overlap': overlap,
               'speech_gate': 'vapi-user-speech', 'eligible_ms': eligibility, 'sample_rate': 24000}
    return client.put(f'/api/becoming/sessions/{ident}/chunks/{seq}', content=data,
                      headers=headers | {'Content-Type': 'audio/wav', 'X-Chunk-SHA256': hashlib.sha256(data).hexdigest(),
                                         'X-Capture-Settings': json.dumps(capture)})


def collect(client, ident, headers):
    for seq in range(10):
        response = upload(client, ident, headers, seq)
        assert response.status_code == 200, response.text
        assert response.json()['seq'] == seq


def wait_for(client, ident, headers, states):
    for _ in range(100):
        response = client.get(f'/api/becoming/sessions/{ident}', headers=headers)
        data = response.json()
        if data['state'] in states:
            return data
        time.sleep(.02)
    pytest.fail('No expected terminal processing state: ' + repr(data))


def test_default_disabled_even_with_legacy_enrollment(tmp_path, monkeypatch):
    monkeypatch.delenv('RANEEN_BECOMING_ENABLED', raising=False)
    monkeypatch.setenv('RANEEN_VOICE_ENROLLMENT_ENABLED', '1')
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.post('/api/becoming/sessions', json=CONSENT).status_code == 404


def test_consent_cookie_capability_isolated_and_no_secrets(setup):
    app, client, fake = setup
    assert client.post('/api/becoming/sessions', json=CONSENT | {'own_voice': False}).status_code == 403
    ident, headers = start(client)
    second, second_headers = start(client)
    assert client.get(f'/api/becoming/sessions/{ident}', headers=second_headers).status_code == 401
    assert client.get(f'/api/becoming/sessions/{ident}', headers={'Authorization': 'Bearer invalid'}).status_code == 401
    assert client.get('/api/me', headers=headers).status_code == 401
    cookies = [cookie for cookie in client.cookies.jar if cookie.path.endswith(ident)]
    assert cookies and cookies[0]._rest.get('HttpOnly') is None and cookies[0].path == '/api/becoming/sessions/' + ident
    call(client, ident, headers)
    result = client.get(f'/api/becoming/sessions/{ident}', headers=headers)
    for forbidden in ('mock-vapi-private', 'mock-eleven-private', 'DO-NOT-COPY', 'control_url', 'webhook_hash', 'capability_hash', 'bootstrap_config'):
        assert forbidden not in result.text
    inline = fake.calls[-1][2]['assistant']
    assert 'tools' not in inline['model'] and 'toolIds' not in inline['model']
    assert 'credentialIds' not in inline
    assert 'Hi' in inline['firstMessage']
    assert inline['clientMessages'] == []
    assert {'assistant.started', 'assistant.speechStarted'} <= set(inline['serverMessages'])
    assert call(client, ident, headers)['reused'] is True
    assert len([x for x in fake.calls if x[0] == 'POST']) == 1


def test_pcm_provenance_duration_order_and_idempotency(setup):
    app, client, fake = setup
    ident, headers = start(client); call(client, ident, headers)
    assert upload(client, ident, headers, 0, overlap=True).status_code == 422
    assert upload(client, ident, headers, 0, source='mixed_call').status_code == 422
    assert upload(client, ident, headers, 0, eligibility=5000).status_code == 422
    assert upload(client, ident, headers, 0, data=wav(silent=True)).status_code == 422
    assert upload(client, ident, headers, 1).status_code == 409
    accepted = upload(client, ident, headers, 0)
    assert accepted.json()['eligible_audio_seconds'] == 3
    assert upload(client, ident, headers, 0).json()['duplicate'] is True
    assert upload(client, ident, headers, 0, eligibility=2999).status_code == 409
    assert fake.clone_count == 0


def test_clone_synth_same_call_then_authoritative_audio_confirmation(setup):
    app, client, fake = setup
    ident, headers = start(client); result = call(client, ident, headers)
    collect(client, ident, headers)
    data = wait_for(client, ident, headers, {'SWITCHING_VOICE'})
    assert data['voice_ready'] is True and data['voice_id'] == VOICE_ID
    assert data['telemetry']['clone_request_at'] <= data['telemetry']['clone_created_at'] <= data['telemetry']['clone_ready_at']
    assert fake.clone_count == 1 and fake.synth_count == 1
    handoff = [x for x in fake.controls if x['type'] == 'handoff'][0]
    assert handoff['content'] == '' and handoff['destination']['contextEngineeringPlan'] == {'type': 'all'}
    assistant = handoff['destination']['assistant']
    assert assistant['voice']['voiceId'] == VOICE_ID and assistant['firstMessage'] == ''
    assert 'Dubai' in assistant['model']['messages'][0]['content']
    client_event = {'event_id': 'browser-1', 'call_id': CALL_ID, 'type': 'assistant.speechStarted',
                    'new_assistant_voice': {'provider': '11labs', 'voice_id': VOICE_ID}}
    assert client.post(f'/api/becoming/sessions/{ident}/events', json=client_event, headers=headers).status_code == 200
    assert client.get(f'/api/becoming/sessions/{ident}', headers=headers).json()['state'] == 'SWITCHING_VOICE'
    row = app.state.store.one('SELECT bootstrap_config FROM becoming_sessions WHERE id=?', (ident,))
    # Local mode deliberately does not configure public callbacks; test verified source through service.
    app.state.store.execute('UPDATE becoming_sessions SET webhook_hash=? WHERE id=?', (hashlib.sha256(b'provider-only-token').hexdigest(), ident))
    webhook_headers = {'X-Raneen-Becoming-Event': 'provider-only-token'}
    message = {'type': 'speech-update', 'role': 'assistant', 'status': 'started', 'call': {'id': CALL_ID},
               'assistant': {'voice': {'provider': '11labs', 'voiceId': 'bootstrap_voice'}}}
    assert client.post(f'/api/becoming/provider/{ident}', json={'message': message}, headers=webhook_headers).status_code == 200
    assert client.get(f'/api/becoming/sessions/{ident}', headers=headers).json()['state'] == 'SWITCHING_VOICE'
    message['assistant']['voice']['voiceId'] = VOICE_ID
    assert client.post(f'/api/becoming/provider/{ident}', json={'message': message}, headers=webhook_headers).status_code == 200
    active = client.get(f'/api/becoming/sessions/{ident}', headers=headers).json()
    assert active['state'] == 'CLONED_ACTIVE'
    assert active['telemetry']['cloned_voice_first_audio_source'] == 'provider_authenticated'
    assert client.get(f'/api/becoming/sessions/{ident}/voice-check', headers=headers).status_code == 200
    for _ in range(3):
        assert client.post(f'/api/becoming/sessions/{ident}/process', json={}, headers=headers).status_code == 200
    assert fake.clone_count == 1 and fake.synth_count == 1 and len([x for x in fake.controls if x['type'] == 'handoff']) == 1


@pytest.mark.parametrize('uncertain,state', [(False, 'CLONE_FAILED'), (True, 'CLONE_UNKNOWN')])
def test_clone_failure_bootstrap_survives_no_duplicate(setup, uncertain, state):
    app, client, fake = setup
    fake.error = ProviderError('Synthetic provider rejection.', uncertain=uncertain)
    ident, headers = start(client); call(client, ident, headers); collect(client, ident, headers)
    data = wait_for(client, ident, headers, {state})
    assert data['call_state'] == 'open' and not data['voice_ready']
    assert data['failure']['bootstrap_continues'] is True
    for _ in range(3):
        client.post(f'/api/becoming/sessions/{ident}/process', json={}, headers=headers)
    assert fake.clone_count == 1 and fake.synth_count == 0 and not fake.ended


def test_verification_required_never_bypassed(setup):
    app, client, fake = setup; fake.verification = True
    ident, headers = start(client); call(client, ident, headers); collect(client, ident, headers)
    assert wait_for(client, ident, headers, {'VERIFICATION_REQUIRED'})['voice_ready'] is False
    client.post(f'/api/becoming/sessions/{ident}/process', json={}, headers=headers)
    assert fake.synth_count == 0 and not fake.controls


def test_end_preserves_voice_revoke_removes_only_scoped_artifacts(setup):
    app, client, fake = setup
    ident, headers = start(client); call(client, ident, headers); collect(client, ident, headers)
    wait_for(client, ident, headers, {'SWITCHING_VOICE'})
    assert client.post(f'/api/becoming/sessions/{ident}/end', json={}, headers=headers).status_code == 200
    assert not fake.deleted and fake.ended
    assert (app.state.becoming.root / ident / 'voice-check.mp3').is_file()
    assert client.post(f'/api/becoming/sessions/{ident}/call', json={}, headers=headers).status_code == 410
    assert client.post(f'/api/becoming/sessions/{ident}/revoke', json={'confirm': True}, headers=headers).status_code == 200
    assert fake.deleted == [VOICE_ID]
    assert not (app.state.becoming.root / ident).exists()
    assert client.get(f'/api/becoming/sessions/{ident}/voice-check', headers=headers).status_code == 410


def test_revoke_during_clone_tracks_then_deletes_late_voice(setup):
    app, client, fake = setup
    fake.block = asyncio.Event()
    ident, headers = start(client); call(client, ident, headers)
    collect(client, ident, headers)
    wait_for(client, ident, headers, {'CLONING'})
    # CLONING records the atomic reservation before PCM assembly. Synchronize on
    # actual provider entry so this specifically tests a result arriving after revocation.
    for _ in range(100):
        if fake.clone_count == 1:
            break
        time.sleep(.02)
    assert fake.clone_count == 1
    response = client.post(f'/api/becoming/sessions/{ident}/revoke', json={'confirm': True}, headers=headers)
    assert response.json()['cleanup'] == 'pending'
    client.portal.call(fake.block.set)
    for _ in range(100):
        if fake.deleted:
            break
        time.sleep(.02)
    assert fake.deleted == [VOICE_ID] and fake.synth_count == 0
    row = app.state.store.one('SELECT voice_id,state FROM becoming_sessions WHERE id=?', (ident,))
    assert row == {'voice_id': VOICE_ID, 'state': 'REVOKED'}
    assert client.get(f'/api/becoming/sessions/{ident}/voice-check', headers=headers).status_code == 410


def test_revoke_before_clone_dispatch_never_creates_a_voice(setup, monkeypatch):
    app, client, fake = setup
    ident, headers = start(client); call(client, ident, headers)
    monkeypatch.setattr(app.state.becoming, 'spawn_process', lambda ident: None)
    collect(client, ident, headers)
    assert fake.clone_count == 0
    assert client.post(f'/api/becoming/sessions/{ident}/revoke', json={'confirm': True}, headers=headers).status_code == 200
    with pytest.raises(Exception) as rejected:
        client.portal.call(app.state.becoming.process, ident)
    assert getattr(rejected.value, 'status_code', None) == 410
    assert fake.clone_count == 0 and fake.synth_count == 0


def test_revoke_during_assembly_releases_undispatched_operation(setup, monkeypatch):
    app, client, fake = setup
    entered = threading.Event(); release = threading.Event()
    def assembly(chunks, sample):
        entered.set()
        assert release.wait(5), 'The controlled synthetic assembly was not released.'
    monkeypatch.setattr(app.state.becoming, 'assemble', assembly)
    ident, headers = start(client); call(client, ident, headers)
    try:
        collect(client, ident, headers)
        assert entered.wait(2)
        response = client.post(f'/api/becoming/sessions/{ident}/revoke', json={'confirm': True}, headers=headers)
        assert response.status_code == 200
    finally:
        release.set()
    for _ in range(100):
        operation = app.state.store.one("SELECT state,detail FROM becoming_operations WHERE session_id=? AND kind='clone'", (ident,))
        telemetry = json.loads(app.state.store.one('SELECT telemetry FROM becoming_sessions WHERE id=?', (ident,))['telemetry'])
        if operation['state'] == 'failed' and telemetry.get('cleanup') == 'complete':
            break
        time.sleep(.02)
    assert operation['state'] == 'failed'
    assert json.loads(operation['detail'])['provider_request_dispatched'] is False
    assert fake.clone_count == 0
    status = client.get(f'/api/becoming/sessions/{ident}', headers=headers).json()
    assert 'clone_request_at' not in status['telemetry']
    assert status['telemetry']['cleanup'] == 'complete'


def test_simultaneous_processors_claim_one_clone_synthesis_and_handoff(setup, monkeypatch):
    app, client, fake = setup
    ident, headers = start(client); call(client, ident, headers)
    monkeypatch.setattr(app.state.becoming, 'spawn_process', lambda ident: None)
    collect(client, ident, headers)
    async def concurrent():
        await asyncio.gather(app.state.becoming.process(ident), app.state.becoming.process(ident),
                             app.state.becoming.process(ident))
    client.portal.call(concurrent)
    assert fake.clone_count == 1 and fake.synth_count == 1
    assert len([control for control in fake.controls if control['type'] == 'handoff']) == 1


def test_restart_unknown_clone_never_repeats_paid_create(setup):
    from studio.becoming import BecomingService
    app, client, fake = setup
    ident, headers = start(client); call(client, ident, headers)
    stamp = app.state.store.one('SELECT created FROM becoming_sessions WHERE id=?', (ident,))['created']
    app.state.store.execute("UPDATE becoming_sessions SET state='CLONING',eligible_ms=30000 WHERE id=?", (ident,))
    app.state.store.execute("INSERT INTO becoming_operations VALUES('unknown-clone',?,'clone','attempt-1','dispatching',NULL,'{}',?,?)", (ident, stamp, stamp))
    recovered = BecomingService(app)
    client.portal.call(recovered.process, ident)
    assert fake.clone_count == 0
    assert app.state.store.one("SELECT state FROM becoming_operations WHERE id='unknown-clone'")['state'] == 'outcome_unknown'


def test_explicit_audio_rejection_can_collect_larger_sample_without_uncertain_retry(setup):
    app, client, fake = setup
    fake.error = ProviderError('ElevenLabs clone returned HTTP 422.', diagnostics={'http_status': 422, 'provider_code': 'invalid_audio'})
    ident, headers = start(client); call(client, ident, headers); collect(client, ident, headers)
    wait_for(client, ident, headers, {'COLLECTING_VOICE'})
    assert client.get(f'/api/becoming/sessions/{ident}', headers=headers).json()['minimum_speech_seconds'] == 45
    fake.error = None
    for seq in range(10, 15):
        assert upload(client, ident, headers, seq).status_code == 200
    wait_for(client, ident, headers, {'SWITCHING_VOICE'})
    assert fake.clone_count == 2


def test_preflight_is_read_only_and_returns_no_template_or_keys(setup):
    app, client, fake = setup
    ident, headers = start(client)
    response = client.post(f'/api/becoming/sessions/{ident}/preflight', json={}, headers=headers)
    assert response.status_code == 200 and response.json()['paid_operations_created'] is False
    assert response.json()['vapi_template_verified'] and response.json()['instant_voice_cloning_available']
    assert 'DO-NOT-COPY' not in response.text and 'mock-vapi-private' not in response.text
    assert all(method == 'GET' for method, _, _ in fake.calls)


def test_restart_restores_known_voice_and_retryable_cleanup(setup):
    from studio.becoming import BecomingService
    app, client, fake = setup
    ident, headers = start(client)
    stamp = app.state.store.one('SELECT created FROM becoming_sessions WHERE id=?', (ident,))['created']
    app.state.store.execute("UPDATE becoming_sessions SET state='CLONING' WHERE id=?", (ident,))
    app.state.store.execute("INSERT INTO becoming_operations VALUES('known-clone',?,'clone','attempt-1','succeeded',?,?,?,?)", (ident, VOICE_ID, json.dumps({'requires_verification': False}), stamp, stamp))
    app.state.store.execute("INSERT INTO becoming_operations VALUES('cleanup',?,'cleanup_voice',?,'dispatching',?,'{}',?,?)", (ident, VOICE_ID, VOICE_ID, stamp, stamp))
    BecomingService(app)
    assert app.state.store.one('SELECT voice_id,state FROM becoming_sessions WHERE id=?', (ident,)) == {'voice_id': VOICE_ID, 'state': 'CLONE_CREATED'}
    assert app.state.store.one("SELECT state FROM becoming_operations WHERE id='cleanup'")['state'] == 'retryable'


def test_revoke_purges_observed_transcripts_and_still_works_when_disabled(setup, monkeypatch):
    app, client, fake = setup
    ident, headers = start(client); call(client, ident, headers)
    event = {'event_id': 'private-words', 'call_id': CALL_ID, 'type': 'transcript', 'role': 'user',
             'transcript_type': 'final', 'transcript': 'Synthetic private wording.'}
    client.post(f'/api/becoming/sessions/{ident}/events', json=event, headers=headers)
    monkeypatch.setenv('RANEEN_BECOMING_ENABLED', '0')
    assert client.post(f'/api/becoming/sessions/{ident}/revoke', json={'confirm': True}, headers=headers).status_code == 200
    assert not app.state.store.all('SELECT * FROM becoming_events WHERE session_id=?', (ident,))
    assert client.get(f'/api/becoming/sessions/{ident}', headers=headers).json()['style_profile']['wording_examples'] == []


def test_webhook_wrong_capability_and_call_and_malformed(setup):
    app, client, fake = setup
    ident, headers = start(client); call(client, ident, headers)
    app.state.store.execute('UPDATE becoming_sessions SET webhook_hash=? WHERE id=?', (hashlib.sha256(b'provider-token').hexdigest(), ident))
    hook = {'X-Raneen-Becoming-Event': 'provider-token'}
    assert client.post(f'/api/becoming/provider/{ident}', json={}, headers=headers).status_code == 401
    assert client.post(f'/api/becoming/provider/{ident}', json={'message': []}, headers=hook).status_code == 400
    assert client.post(f'/api/becoming/provider/{ident}', json={'message': {'call': {'id': 'other'}, 'type': 'speech-update'}}, headers=hook).status_code == 409
    assert client.post(f'/api/becoming/provider/{ident}', json={'message': {'call': {'id': CALL_ID}, 'type': 'speech-update', 'role': 'invalid', 'status': 'started'}}, headers=hook).status_code == 400


def test_sse_relays_only_scoped_normalized_provider_evidence(setup):
    from starlette.requests import Request
    app, client, fake = setup
    ident, headers = start(client); call(client, ident, headers)
    private = {'type': 'speech-update', 'role': 'user', 'status': 'started', 'api_key': 'DO-NOT-RELAY',
               'assistant': {'server': {'headers': {'secret': 'DO-NOT-RELAY'}}},
               'active_assistant_voice': {'provider': '11labs', 'voice_id': VOICE_ID, 'secret': 'DO-NOT-RELAY'}}
    stamp = app.state.store.one('SELECT created FROM becoming_sessions WHERE id=?', (ident,))['created']
    for event_id, source in [('trusted', 'provider_authenticated'), ('untrusted', 'client_reported')]:
        app.state.store.execute('INSERT INTO becoming_events VALUES(?,?,?,?,?,?)', (ident, event_id, source, 'speech-update', json.dumps(private), stamp))
    request = Request({'type': 'http', 'method': 'GET', 'scheme': 'http', 'path': f'/api/becoming/sessions/{ident}/events-stream',
                       'query_string': b'', 'headers': [(b'authorization', headers['Authorization'].encode()), (b'host', b'testserver')],
                       'server': ('testserver', 80), 'client': ('testclient', 100), 'app': app})
    checks = 0
    async def disconnected():
        nonlocal checks
        checks += 1
        return checks > 1
    request.is_disconnected = disconnected
    endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/api/becoming/sessions/{ident}/events-stream')
    async def read():
        response = await endpoint(ident, request)
        return ''.join([part async for part in response.body_iterator])
    result = client.portal.call(read)
    assert result.count('event: provider_event') == 1
    assert 'DO-NOT-RELAY' not in result and 'api_key' not in result and 'assistant' not in result.replace('active_assistant_voice', '')
    assert 'received_at_ms' in result and 'relay_at_ms' in result
    assert not app.state.becoming.stream_counts


def test_active_limit_failure_is_durable(setup, monkeypatch):
    app, client, fake = setup
    monkeypatch.setenv('RANEEN_BECOMING_MAX_ACTIVE_CALLS', '1')
    ident, headers = start(client); call(client, ident, headers)
    other, other_headers = start(client)
    assert client.post(f'/api/becoming/sessions/{other}/call', json={}, headers=other_headers).status_code == 429
    operation = app.state.store.one("SELECT state FROM becoming_operations WHERE session_id=? AND kind='call'", (other,))
    assert operation['state'] == 'failed'
    assert len([x for x in fake.calls if x[0] == 'POST']) == 1


def test_revoke_during_template_read_never_dispatches_or_strands_call(setup, monkeypatch):
    app, client, fake = setup
    ident, headers = start(client)
    entered = threading.Event(); release = asyncio.Event()
    original = fake.vapi_json
    async def template_read(key, method, path, *, json_body=None):
        if method == 'GET':
            entered.set()
            await release.wait()
        return await original(key, method, path, json_body=json_body)
    monkeypatch.setattr(fake, 'vapi_json', template_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f'/api/becoming/sessions/{ident}/call', json={}, headers=headers)
        try:
            assert entered.wait(2)
            assert client.post(f'/api/becoming/sessions/{ident}/revoke', json={'confirm': True}, headers=headers).status_code == 200
        finally:
            client.portal.call(release.set)
        assert pending.result(timeout=5).status_code == 410
    assert not any(method == 'POST' for method, _, _ in fake.calls)
    operation = app.state.store.one("SELECT state,detail FROM becoming_operations WHERE session_id=? AND kind='call'", (ident,))
    assert operation['state'] == 'failed' and json.loads(operation['detail'])['provider_request_dispatched'] is False
    status = client.get(f'/api/becoming/sessions/{ident}', headers=headers).json()
    assert status['call_state'] == 'failed' and status['state'] == 'REVOKED'
    assert status['telemetry']['cleanup'] == 'complete'


def test_style_evidence_separate_bounded_and_updates_no_clone(setup):
    app, client, fake = setup
    ident, headers = start(client); call(client, ident, headers)
    for turn in range(3):
        event = {'event_id': f'turn-{turn}', 'call_id': CALL_ID, 'type': 'transcript', 'role': 'user',
                 'transcript_type': 'final', 'transcript': f'شوف أنا أبدأ بالسؤال عن Dubai والميزانية {turn}'}
        assert client.post(f'/api/becoming/sessions/{ident}/events', json=event, headers=headers).status_code == 200
    profile = client.get(f'/api/becoming/sessions/{ident}', headers=headers).json()['style_profile']
    assert profile['evidence_sources'] == ['client_reported'] * 3
    assert profile['code_switching_observed'] and profile['dialect'] == 'unverified'
    assert len([x for x in fake.controls if x['type'] == 'add-message']) == 1 and fake.clone_count == 0


def test_additive_migration_preserves_old_users(setup):
    app, client, fake = setup
    original = app.state.store.create_user('Synthetic Owner', 'admin')
    apply_migration(app.state.store)
    assert app.state.store.one('SELECT name FROM users WHERE id=?', (original['id'],))['name'] == 'Synthetic Owner'
    assert app.state.store.one('SELECT version FROM becoming_schema_versions')['version'] == 10
    assert app.state.store.one('SELECT MAX(version) AS v FROM becoming_schema_versions')['v'] == 11


def test_expired_retention_blocks_personal_artifacts_but_allows_revoke(setup):
    app, client, fake = setup
    ident, headers = start(client)
    cookie = next(item for item in client.cookies.jar if item.path.endswith(ident))
    assert cookie.expires - time.time() > 6 * 86400
    app.state.store.execute("UPDATE becoming_sessions SET retention_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (ident,))
    assert client.get(f'/api/becoming/sessions/{ident}', headers=headers).status_code == 410
    assert client.get(f'/api/becoming/sessions/{ident}/voice-check', headers=headers).status_code == 410
    assert client.post(f'/api/becoming/sessions/{ident}/revoke', json={'confirm': True}, headers=headers).status_code == 200


@pytest.mark.parametrize('url', ['https://attacker.example/control', 'https://x.vapi.ai:444/control',
                                 'https://user@x.vapi.ai/control', 'https://x.vapi.ai/control#fragment',
                                 'https://vapi.ai/control', 'http://x.vapi.ai/control'])
def test_provider_control_url_rejects_unsafe_destinations(url):
    with pytest.raises(ProviderError):
        checked_provider_url(url, '.vapi.ai')


def test_template_aggregate_prompt_is_bounded():
    template = {'model': {'provider': 'openai', 'model': 'mock', 'messages':
                         [{'role': 'system', 'content': 'a' * 20000}, {'role': 'system', 'content': 'b' * 10000}]},
                'voice': {'provider': '11labs', 'voiceId': 'bootstrap'}}
    with pytest.raises(ProviderError):
        sanitized_assistant(template, 'a' * 32, webhook_url=None, webhook_secret='test', duration=180)
