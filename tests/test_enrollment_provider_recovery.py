"""Synthetic provider rejection/recovery contracts; no real voice or network."""
import asyncio
import concurrent.futures
import json
import tempfile
import threading
from pathlib import Path

import httpx
import pytest

from studio.app import now, uid
from studio.enrollment import ProviderError, Providers
from studio.enrollment_provider_errors import response_diagnostics
from test_voice_enrollment import env, enable, start, auth, upload


@pytest.fixture
def recovery(env, monkeypatch):
    client, app, owner, _, fake = env
    enable(monkeypatch)
    sid = start(client, owner)
    assert upload(client, owner, sid, 0, duration=1000).status_code == 200
    analyses = []

    def sample(chunks, destination, **kwargs):
        analyses.append(kwargs)
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination, suffix='.wav', delete=False) as output:
            output.write(b'synthetic-selected-audio-fixture')
            path = output.name
        return [{'seq': 0, 'path': path, 'mime': 'audio/wav'}], {
            'total_ms': 60000, 'algorithm': 'synthetic-provider-recovery-fixture', 'source': 'synthetic',
        }

    monkeypatch.setattr('studio.enrollment_audio.prepare_clone_sample', sample)
    return client, app, owner, fake, sid, analyses


def clone(client, owner, sid, retry=False):
    body = {'approve': True, 'final_seq': 0}
    if retry:
        body['retry_failed'] = True
    return client.post(f'/api/enrollment/sessions/{sid}/clone', headers=auth(owner), json=body)


def rejected(status=401, code='invalid_api_key'):
    return ProviderError('UNTRUSTED provider message with private_fixture_secret',
                         diagnostics={'http_status': status, 'provider_code': code,
                                      'body': 'private_fixture_secret', 'reason': 'private_fixture_secret'})


def operations(app, sid, kind='voice_clone'):
    return app.state.store.all('SELECT * FROM enrollment_operations WHERE session_id=? AND kind=? ORDER BY created,rowid', (sid, kind))


def workflow(client, owner, sid):
    return client.get(f'/api/enrollment/sessions/{sid}/workflow', headers=auth(owner))


def test_rejected_clone_survives_refresh_and_requires_explicit_retry(recovery):
    client, app, owner, fake, sid, analyses = recovery
    fake.clone_error = rejected()
    failed = clone(client, owner, sid)
    assert failed.status_code == 422
    detail = failed.json()['detail']
    assert detail['code'] == 'voice_clone_failed' and detail['details']['reason'] == 'auth'
    assert detail['details']['retry_allowed'] is True
    assert detail['details']['attempts'] == 1 and detail['details']['attempt_limit'] == 3
    before = operations(app, sid)
    refreshed = workflow(client, owner, sid)
    state = refreshed.json()
    assert state['stage'] == 'preparing' and state['clone_retry_allowed'] is True
    assert state['voice_failure'] == detail and state['clone_attempts'] == 1
    assert operations(app, sid) == before and fake.clone_calls == 1
    assert clone(client, owner, sid).status_code == 409
    assert len(analyses) == 1 and fake.clone_calls == 1
    for output in (failed.text, refreshed.text, json.dumps(before)):
        assert 'private_fixture_secret' not in output and 'UNTRUSTED' not in output and 'body' not in output
    fake.clone_error = None
    retried = clone(client, owner, sid, retry=True)
    assert retried.status_code == 200 and fake.clone_calls == 2
    after = operations(app, sid)
    assert after[0] == before[0] and after[1]['state'] == 'succeeded'
    versions = app.state.store.all('SELECT * FROM enrollment_voice_versions WHERE session_id=? ORDER BY version', (sid,))
    assert len(versions) == 2 and len({x['manifest_digest'] for x in versions}) == 2
    assert json.loads(versions[1]['sample_manifest'])['provider_attempt'] == 2
    assert all(json.loads(x['sample_manifest'])['algorithm'] == 'synthetic-provider-recovery-fixture' for x in versions)
    assert workflow(client, owner, sid).json()['voice_failure'] is None
    assert clone(client, owner, sid, retry=True).json()['reused'] is True and fake.clone_calls == 2


def test_clone_attempt_budget_is_session_wide_and_precedes_decode(recovery):
    client, app, owner, fake, sid, analyses = recovery
    fake.clone_error = rejected(422, 'invalid_parameters')
    for attempt in range(1, 4):
        assert clone(client, owner, sid, retry=attempt > 1).status_code == 422
    denied = clone(client, owner, sid, retry=True)
    assert denied.status_code == 429 and denied.json()['detail']['code'] == 'clone_retry_limit'
    assert fake.clone_calls == 3 and len(analyses) == 3
    assert len(operations(app, sid)) == 3
    state = workflow(client, owner, sid).json()
    assert state['clone_retry_allowed'] is False
    assert state['voice_failure']['details']['retry_allowed'] is False


def test_concurrent_explicit_clone_retries_claim_one_paid_operation(recovery):
    client, app, owner, fake, sid, _ = recovery
    fake.clone_error = rejected()
    assert clone(client, owner, sid).status_code == 422
    entered, release = threading.Event(), threading.Event()

    async def pending(*args):
        fake.clone_calls += 1
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return {'voice_id': 'voice_synthetic_recovered', 'requires_verification': False}

    fake.eleven_clone = pending
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(clone, client, owner, sid, True)
        assert entered.wait(5)
        second = pool.submit(clone, client, owner, sid, True)
        try:
            assert second.result(timeout=5).status_code == 409
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
    assert fake.clone_calls == 2 and len(operations(app, sid)) == 2


def seed_failure(app, sid, *, status=401, state='failed', provider_id=None, error=None):
    ident, stamp = uid(), now()
    old = {'error': error if error is not None else f'ElevenLabs clone returned HTTP {status}.',
           'voice_version_id': 'historical-private-id', 'manifest_digest': 'historical-selection-digest'}
    app.state.store.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)',
                            (ident, sid, 'voice_clone', 'ivc-v1:historical-selection-digest', state, provider_id,
                             json.dumps(old), stamp, stamp))
    app.state.store.execute('UPDATE enrollment_sessions SET voice_state=? WHERE id=?', (state, sid))
    return operations(app, sid)[0]


def test_legacy_exact_rejection_can_retry_after_selection_changes(recovery):
    client, app, owner, fake, sid, _ = recovery
    old = seed_failure(app, sid, status=401)
    state = workflow(client, owner, sid).json()
    assert state['clone_retry_allowed'] is True and state['voice_failure']['details']['reason'] == 'auth'
    assert clone(client, owner, sid).status_code == 409 and fake.clone_calls == 0
    assert clone(client, owner, sid, retry=True).status_code == 200 and fake.clone_calls == 1
    assert operations(app, sid)[0] == old and len(operations(app, sid)) == 2


@pytest.mark.parametrize('status,state,provider_id,error', [
    (408, 'failed', None, None), (409, 'failed', None, None), (500, 'failed', None, None),
    (302, 'failed', None, None), (401, 'outcome_unknown', None, None),
    (401, 'dispatching', None, None), (401, 'failed', 'voice_known_id', None),
    (401, 'failed', None, 'ElevenLabs clone returned HTTP 401. private_fixture_secret'),
    (401, 'failed', None, 'timeout private_fixture_secret'),
])
def test_unproven_legacy_failure_never_retries(recovery, status, state, provider_id, error):
    client, app, owner, fake, sid, analyses = recovery
    seed_failure(app, sid, status=status, state=state, provider_id=provider_id, error=error)
    response = workflow(client, owner, sid)
    assert response.json()['clone_retry_allowed'] is False
    assert 'private_fixture_secret' not in response.text and 'historical-private-id' not in response.text
    assert clone(client, owner, sid, retry=True).status_code in {200, 409}
    assert fake.clone_calls == 0 and analyses == []


def test_generic_mock_error_is_safe_and_never_retryable(recovery):
    client, app, owner, fake, sid, _ = recovery
    fake.clone_error = ProviderError('private_fixture_secret arbitrary message')
    failed = clone(client, owner, sid)
    assert failed.status_code == 422
    assert failed.json()['detail'] == {'code': 'voice_clone_failed', 'message': 'The voice provider could not create the clone.'}
    assert clone(client, owner, sid, retry=True).status_code == 409 and fake.clone_calls == 1
    assert 'private_fixture_secret' not in json.dumps(operations(app, sid))


def provider_http(monkeypatch, handler):
    actual = httpx.AsyncClient
    monkeypatch.setattr('studio.enrollment.httpx.AsyncClient',
                        lambda **kwargs: actual(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.parametrize('status,field,code,reason', [
    (401, 'code', 'invalid_api_key', 'auth'), (403, 'status', 'missing_permissions', 'permission'),
    (403, 'code', 'feature_not_available', 'plan'), (402, 'type', 'payment_required', 'quota'),
    (400, 'code', 'voice_limit_reached', 'voice_limit'), (422, 'type', 'validation_error', 'request_validation'),
    (429, 'status', 'concurrent_limit_exceeded', 'rate_limit'),
])
def test_real_adapter_safe_diagnostics_and_multipart_wire(monkeypatch, tmp_path, status, field, code, reason):
    audio = tmp_path / 'synthetic.wav'
    audio.write_bytes(b'synthetic-audio-fixture')
    seen = []

    def reject(request):
        payload = request.read()
        assert b'name="files"; filename="synthetic.wav"' in payload
        assert b'files[]' not in payload
        seen.append(request)
        return httpx.Response(status, json={'detail': {field: code, 'message': 'private_fixture_secret',
                                                      'api_key': 'private_fixture_secret'}})

    provider_http(monkeypatch, reject)
    with pytest.raises(ProviderError) as failed:
        asyncio.run(Providers().eleven_clone('synthetic-key', 'Synthetic voice', [('synthetic.wav', audio, 'audio/wav')]))
    assert failed.value.uncertain is False
    assert failed.value.diagnostics == {'provider': 'elevenlabs', 'http_status': status, 'reason': reason,
                                       'rejected': True, 'provider_code': code}
    assert 'private_fixture_secret' not in str(failed.value) and len(seen) == 1


@pytest.mark.parametrize('status', [302, 408, 409, 500])
def test_nonrejection_http_response_is_uncertain(monkeypatch, tmp_path, status):
    audio = tmp_path / 'synthetic.wav'; audio.write_bytes(b'synthetic')
    provider_http(monkeypatch, lambda request: httpx.Response(status, json={'detail': {'message': 'private_fixture_secret'}}))
    with pytest.raises(ProviderError) as failed:
        asyncio.run(Providers().eleven_clone('synthetic-key', 'Synthetic', [('synthetic.wav', audio, 'audio/wav')]))
    assert failed.value.uncertain is True and failed.value.diagnostics['rejected'] is False


@pytest.mark.parametrize('body', [None, [], {'voice_id': 'voice_synthetic_valid'},
                                 {'voice_id': 'voice_synthetic_valid', 'requires_verification': 'false'}])
def test_malformed_success_stays_uncertain(monkeypatch, tmp_path, body):
    audio = tmp_path / 'synthetic.wav'; audio.write_bytes(b'synthetic')
    provider_http(monkeypatch, lambda request: httpx.Response(200, json=body))
    with pytest.raises(ProviderError) as failed:
        asyncio.run(Providers().eleven_clone('synthetic-key', 'Synthetic', [('synthetic.wav', audio, 'audio/wav')]))
    assert failed.value.uncertain is True


def test_remote_protocol_error_after_upload_stays_uncertain(monkeypatch, tmp_path):
    audio = tmp_path / 'synthetic.wav'; audio.write_bytes(b'synthetic')

    def unconfirmed(request):
        request.read()
        raise httpx.RemoteProtocolError('private_fixture_secret', request=request)

    provider_http(monkeypatch, unconfirmed)
    with pytest.raises(ProviderError) as failed:
        asyncio.run(Providers().eleven_clone('synthetic-key', 'Synthetic', [('synthetic.wav', audio, 'audio/wav')]))
    assert failed.value.uncertain is True and 'private_fixture_secret' not in str(failed.value)


def test_provider_error_body_bound_and_unknown_code_are_not_projected():
    oversized = httpx.Response(401, content=b'x' * (64 * 1024 + 1))
    oversized.json = lambda: (_ for _ in ()).throw(AssertionError('oversized body must not be decoded'))
    assert response_diagnostics(oversized) == {'provider': 'elevenlabs', 'http_status': 401, 'reason': 'auth', 'rejected': True}
    unknown = response_diagnostics(httpx.Response(422, json={'detail': {'code': 'private_fixture_secret', 'message': 'private_fixture_secret'}}))
    assert unknown == {'provider': 'elevenlabs', 'http_status': 422, 'reason': 'request_validation', 'rejected': True}


def preview(client, owner, sid, kind='question', retry=False):
    body = {'approve': True, 'kind': kind}
    if retry:
        body['retry_failed'] = True
    return client.post(f'/api/enrollment/sessions/{sid}/preview', headers=auth(owner), json=body)


def test_preview_rejected_request_requires_explicit_bounded_retry(recovery):
    client, app, owner, fake, sid, _ = recovery
    assert clone(client, owner, sid).status_code == 200
    calls = []

    async def denied(*args):
        calls.append(args)
        raise rejected(402, 'payment_required')

    fake.eleven_speech = denied
    failed = preview(client, owner, sid)
    assert failed.status_code == 502
    assert failed.json()['detail']['details']['reason'] == 'quota'
    state = workflow(client, owner, sid).json()
    assert state['voice_failure'] == failed.json()['detail']
    assert state['preview_retry_allowed'] == {'question': True, 'number': False, 'correction': False}
    assert preview(client, owner, sid).status_code == 409 and len(calls) == 1
    assert preview(client, owner, sid, retry=True).status_code == 502
    assert preview(client, owner, sid, retry=True).status_code == 502
    assert preview(client, owner, sid, retry=True).status_code == 429 and len(calls) == 3
    assert len(operations(app, sid, 'voice_preview')) == 3
    assert workflow(client, owner, sid).json()['preview_retry_allowed']['question'] is False


def test_preview_retry_success_reuses_voice_and_cached_audio(recovery):
    client, app, owner, fake, sid, _ = recovery
    assert clone(client, owner, sid).status_code == 200
    original_speech = fake.eleven_speech

    async def denied(*args):
        raise rejected(403, 'missing_permissions')

    fake.eleven_speech = denied
    assert preview(client, owner, sid).status_code == 502
    before = operations(app, sid, 'voice_preview')[0]
    fake.eleven_speech = original_speech
    assert preview(client, owner, sid, retry=True).status_code == 200
    assert preview(client, owner, sid, retry=True).status_code == 200
    assert len(fake.speech_calls) == 1 and fake.clone_calls == 1
    assert operations(app, sid, 'voice_preview')[0] == before
    assert workflow(client, owner, sid).json()['voice_failure'] is None


def test_unknown_preview_blocks_other_sample_and_explicit_retry(recovery):
    client, app, owner, fake, sid, _ = recovery
    assert clone(client, owner, sid).status_code == 200
    calls = []

    async def unknown(*args):
        calls.append(args)
        raise ProviderError('private_fixture_secret', uncertain=True)

    fake.eleven_speech = unknown
    assert preview(client, owner, sid).status_code == 502
    assert preview(client, owner, sid, retry=True).status_code == 409
    assert preview(client, owner, sid, kind='number', retry=True).status_code == 409
    assert len(calls) == 1 and len(operations(app, sid, 'voice_preview')) == 1
    state = workflow(client, owner, sid).json()
    assert state['preview_retry_allowed'] == {'question': False, 'number': False, 'correction': False}
    assert state['voice_failure']['code'] == 'outcome_unknown'


@pytest.mark.parametrize('local_code', ['decoder_unavailable', 'invalid_voice_preview'])
def test_local_preview_validation_code_survives_refresh_safely(recovery, monkeypatch, local_code):
    from studio.enrollment_audio import AudioValidationError
    client, app, owner, fake, sid, _ = recovery
    assert clone(client, owner, sid).status_code == 200

    def cannot_validate(audio):
        raise AudioValidationError(local_code, 'Synthetic local validation failure.')

    monkeypatch.setattr('studio.enrollment_audio.validate_synthesized_audio', cannot_validate)
    failed = preview(client, owner, sid)
    assert failed.status_code == 502 and failed.json()['detail']['code'] == local_code
    operation = operations(app, sid, 'voice_preview')[0]
    historical = json.loads(operation['detail'])
    historical['failure']['message'] = 'private_fixture_secret historical arbitrary text'
    historical['failure']['details'] = {'private_field': 'private_fixture_secret'}
    app.state.store.execute('UPDATE enrollment_operations SET detail=? WHERE id=?',
                            (json.dumps(historical), operation['id']))
    refreshed = workflow(client, owner, sid)
    failure = refreshed.json()['voice_failure']
    assert failure['code'] == local_code
    assert failure['message'] == 'The generated voice sample could not be decoded and checked for playback.'
    assert failure['details']['retry_allowed'] is False
    assert 'private_fixture_secret' not in refreshed.text and 'private_field' not in refreshed.text
    assert preview(client, owner, sid, retry=True).status_code == 409
    assert len(fake.speech_calls) == 1
