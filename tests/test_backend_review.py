"""Security regressions from an independent backend integration review."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from studio.app import CONSENT_VERSION, create_app
from studio.providers import LocalLearningProvider


def auth(user):
    return {'Authorization': 'Bearer ' + user['token']}


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv('RANEEN_LEARNING_PROVIDER', 'local_rules')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setenv('RANEEN_VAPI_WEBHOOK_SECRET', 'x' * 48)
    app = create_app(tmp_path)
    store = app.state.store
    owner = store.create_user('Contributor')
    admin = store.create_user('Admin', 'admin')
    client = TestClient(app, raise_server_exceptions=False)
    result = client.post('/api/profiles', headers=auth(owner), json={
        'name': 'Agent', 'dialect': 'Emirati Arabic', 'contribution': 'both', 'style_notes': '',
    })
    assert result.status_code == 201, result.text
    profile_id = result.json()['id']
    return client, app, store, owner, admin, profile_id


def consent(client, owner, pid):
    result = client.post(f'/api/profiles/{pid}/consent', headers=auth(owner), json={
        'collection': True, 'behavior_export': True, 'voice_export': True,
        'version': CONSENT_VERSION, 'self_attestation': True,
    })
    assert result.status_code == 201, result.text
    return result.json()['id']


def session(client, owner, pid, **fields):
    result = client.post('/api/sessions', headers=auth(owner), json={'profile_id': pid, **fields})
    assert result.status_code == 201, result.text
    return result.json()['id']


def authorize(client, owner, pid, provider='openai', scope='text_learning'):
    result = client.post(f'/api/profiles/{pid}/provider-authorizations', headers=auth(owner), json={
        'provider': provider, 'scope': scope, 'self_attestation': True,
    })
    assert result.status_code == 201, result.text


class RecordingExternalProvider:
    mode = 'openai'
    name = 'test_external_provider'

    def __init__(self):
        self.calls = []

    def analyze(self, transcript, context):
        self.calls.append(('analyze', transcript, copy.deepcopy(context)))
        return LocalLearningProvider().analyze(transcript, context)

    def respond(self, context, messages):
        self.calls.append(('respond', copy.deepcopy(context), copy.deepcopy(messages)))
        return 'What matters most to you?'


def test_new_provider_authorization_does_not_cover_older_unanalyzed_turn(backend):
    c, app, store, owner, admin, pid = backend
    consent(c, owner, pid)
    sid = session(c, owner, pid)
    provider = RecordingExternalProvider()
    app.state.learning.provider.provider = provider
    result = c.post(f'/api/sessions/{sid}/turns', headers=auth(owner), json={
        'role': 'trainer', 'transcript': 'I first ask about the purpose, living or investing.', 'analyze': False,
    })
    assert result.status_code == 201, result.text
    tid = result.json()['id']
    consent(c, owner, pid)
    authorize(c, owner, pid)
    result = c.post(f'/api/turns/{tid}/analyze', headers=auth(owner))
    assert result.status_code == 403, result.text
    assert provider.calls == []
    assert store.all('SELECT * FROM observations') == []


def test_new_provider_authorization_does_not_cover_older_reviewed_example(backend):
    c, app, store, owner, admin, pid = backend
    consent(c, owner, pid)
    result = c.post('/api/examples', headers=auth(owner), json={
        'profile_id': pid, 'scenario_id': 'purpose-01', 'session_id': 'old-demo',
        'response_text': 'I first ask about the purpose, living or investing.',
        'transcript_verified': True, 'action': 'clarify', 'decision_cue': 'Unclear purpose',
        'alternative': 'Do not recommend yet', 'change_condition': 'Purpose becomes clear',
        'delivery': 'question', 'pronunciation_notes': '', 'audio_id': None,
    })
    assert result.status_code == 201, result.text
    eid = result.json()['id']
    result = c.post(f'/api/examples/{eid}/review', headers=auth(admin), json={'status': 'approved'})
    assert result.status_code == 200, result.text
    consent(c, owner, pid)
    authorize(c, owner, pid)
    provider = RecordingExternalProvider()
    app.state.learning.provider.provider = provider
    result = c.post(f'/api/profiles/{pid}/learning/import-examples', headers=auth(owner))
    assert result.status_code == 403, result.text
    assert provider.calls == []
    assert store.all('SELECT * FROM observations') == []


def test_provider_webhook_secret_is_distinct_from_user_access_token(backend):
    c, app, store, owner, admin, pid = backend
    result = c.post('/api/providers/vapi/webhook', headers=auth(owner), json={})
    assert result.status_code == 401
    result = c.post('/api/providers/vapi/webhook', headers={'Authorization': 'Bearer ' + 'x' * 48}, json={})
    assert result.status_code == 422


@pytest.mark.parametrize('malformed_call', [[], ['call'], 'call', 42])
def test_malformed_authenticated_webhook_call_is_client_error(backend, malformed_call):
    c, app, store, owner, admin, pid = backend
    result = c.post('/api/providers/vapi/webhook', headers={'Authorization': 'Bearer ' + 'x' * 48}, json={
        'message': {'call': malformed_call, 'type': 'transcript'},
    })
    assert result.status_code == 422, result.text


@pytest.mark.parametrize('call', [
    {'metadata': 'not-an-object'},
    {'assistant': 'not-an-object'},
    {'assistant': {'metadata': ['not-an-object']}},
])
def test_malformed_authenticated_webhook_metadata_is_client_error(backend, call):
    c, app, store, owner, admin, pid = backend
    result = c.post('/api/providers/vapi/webhook', headers={'Authorization': 'Bearer ' + 'x' * 48}, json={
        'message': {'call': call, 'type': 'transcript'},
    })
    assert result.status_code == 422, result.text


@pytest.mark.parametrize('malformed_call', [['call'], 'call', 42])
def test_malformed_custom_llm_call_is_client_error(backend, malformed_call):
    c, app, store, owner, admin, pid = backend
    consent(c, owner, pid)
    authorize(c, owner, pid, 'vapi', 'realtime_voice')
    sid = session(c, owner, pid, realtime_provider='vapi')
    result = c.post(f'/api/providers/vapi/sessions/{sid}/chat/completions',
        headers={'Authorization': 'Bearer ' + 'x' * 48}, json={
            'messages': [{'role': 'user', 'content': 'Hello.'}], 'call': malformed_call,
        })
    assert result.status_code == 422, result.text


def vapi_transcript(client, sid, call_id, event_id='event-one'):
    return client.post('/api/providers/vapi/webhook', headers={'Authorization': 'Bearer ' + 'x' * 48}, json={
        'message': {'id': event_id, 'type': 'transcript', 'transcriptType': 'final',
            'role': 'user', 'transcript': 'Hello.',
            'call': {'id': call_id, 'metadata': {'raneen_session_id': sid}}},
    })


def test_provider_call_cannot_be_bound_to_two_sessions(backend):
    c, app, store, owner, admin, pid = backend
    consent(c, owner, pid)
    authorize(c, owner, pid, 'vapi', 'realtime_voice')
    first = session(c, owner, pid, realtime_provider='vapi')
    second = session(c, owner, pid, realtime_provider='vapi')
    result = vapi_transcript(c, first, 'one-provider-call')
    assert result.status_code == 200, result.text
    result = vapi_transcript(c, second, 'one-provider-call')
    assert result.status_code == 409, result.text
    assert store.all('SELECT * FROM conversation_turns WHERE session_id=?', (second,)) == []


def test_final_webhook_replay_creates_one_turn_and_one_completion_event(backend):
    c, app, store, owner, admin, pid = backend
    consent(c, owner, pid)
    authorize(c, owner, pid, 'vapi', 'realtime_voice')
    sid = session(c, owner, pid, realtime_provider='vapi')
    first = vapi_transcript(c, sid, 'one-provider-call')
    second = vapi_transcript(c, sid, 'one-provider-call')
    assert first.status_code == second.status_code == 200
    assert first.json()['turn_id'] == second.json()['turn_id']
    assert len(store.all('SELECT * FROM conversation_turns WHERE session_id=?', (sid,))) == 1
    assert len(store.all("SELECT * FROM session_events WHERE session_id=? AND type='trainer.turn_completed'", (sid,))) == 1


def test_new_realtime_authorization_does_not_cover_old_session_consent(backend):
    c, app, store, owner, admin, pid = backend
    consent(c, owner, pid)
    sid = session(c, owner, pid, realtime_provider='vapi')
    consent(c, owner, pid)
    authorize(c, owner, pid, 'vapi', 'realtime_voice')
    result = vapi_transcript(c, sid, 'old-session-call')
    assert result.status_code == 403, result.text
    assert store.all('SELECT * FROM conversation_turns WHERE session_id=?', (sid,)) == []


@pytest.mark.parametrize('mutation', ['end', 'withdraw', 'revoke'])
def test_realtime_transcript_blocked_after_end_or_consent_revocation(backend, mutation):
    c, app, store, owner, admin, pid = backend
    consent(c, owner, pid)
    authorize(c, owner, pid, 'vapi', 'realtime_voice')
    sid = session(c, owner, pid, realtime_provider='vapi')
    if mutation == 'end':
        result = c.post(f'/api/sessions/{sid}/end', headers=auth(owner))
    elif mutation == 'withdraw':
        result = c.post(f'/api/profiles/{pid}/withdraw', headers=auth(owner))
    else:
        aid = store.one('SELECT id FROM provider_authorizations WHERE profile_id=?', (pid,))['id']
        result = c.post(f'/api/profiles/{pid}/provider-authorizations/{aid}/withdraw', headers=auth(owner))
    assert result.status_code == 200, result.text
    result = vapi_transcript(c, sid, 'one-provider-call')
    assert result.status_code == (403 if mutation == 'revoke' else 409), result.text
    assert store.all('SELECT * FROM conversation_turns WHERE session_id=?', (sid,)) == []


def test_unknown_realtime_session_does_not_respond(backend):
    c, app, store, owner, admin, pid = backend
    result = vapi_transcript(c, 'unknown-session', 'one-provider-call')
    assert result.status_code == 404
    assert store.all('SELECT * FROM conversation_turns') == []


def test_delete_profile_removes_its_local_synthesized_voice_preview(backend):
    c, app, store, owner, admin, pid = backend
    consent(c, owner, pid)
    store.execute('''INSERT INTO voice_versions(id,profile_id,version_number,provider,source_manifest_json,
        status,idempotency_key,preview_text,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        ('test-voice', pid, 1, 'elevenlabs', json.dumps({}), 'ready', 'test-build', 'hello', 'today', 'today'))
    preview = app.state.voice.preview_dir / 'test-voice.mp3'
    preview.write_bytes(b'personal synthetic voice')
    result = c.delete(f'/api/profiles/{pid}', headers=auth(owner))
    assert result.status_code == 200, result.text
    assert not preview.exists()


def test_profile_deletion_cascades_conversation_learning_and_evaluation(backend):
    c, app, store, owner, admin, pid = backend
    consent(c, owner, pid)
    sid = session(c, owner, pid)
    result = c.post(f'/api/sessions/{sid}/turns', headers=auth(owner), json={
        'role': 'trainer', 'transcript': 'I first ask about the purpose, living or investing.',
    })
    assert result.status_code == 201, result.text
    result = c.post(f"/api/turns/{result.json()['id']}/analyze", headers=auth(owner))
    assert result.status_code == 200, result.text
    result = c.post(f'/api/sessions/{sid}/simulation', headers=auth(owner), json={})
    assert result.status_code == 201, result.text
    result = c.post(f'/api/profiles/{pid}/evaluations', headers=auth(owner), json={})
    assert result.status_code == 201, result.text
    result = c.delete(f'/api/profiles/{pid}', headers=auth(owner))
    assert result.status_code == 200, result.text
    for table in ('teaching_sessions', 'conversation_turns', 'session_events', 'observations',
                  'hypotheses', 'agent_profile_versions', 'simulation_runs', 'evaluation_runs'):
        assert store.all(f'SELECT * FROM {table}') == []


def test_mixed_naive_and_aware_turn_timestamps_are_rejected(backend):
    c, app, store, owner, admin, pid = backend
    consent(c, owner, pid)
    sid = session(c, owner, pid)
    result = c.post(f'/api/sessions/{sid}/turns', headers=auth(owner), json={
        'role': 'trainer', 'transcript': 'Hello.',
        'started_at': '2026-10-01T09:00:00', 'ended_at': '2026-10-01T09:01:00Z',
    })
    assert result.status_code == 422, result.text
    assert store.all('SELECT * FROM conversation_turns') == []


def test_oversized_final_webhook_transcript_is_client_error(backend):
    c, app, store, owner, admin, pid = backend
    consent(c, owner, pid)
    authorize(c, owner, pid, 'vapi', 'realtime_voice')
    sid = session(c, owner, pid, realtime_provider='vapi')
    result = c.post('/api/providers/vapi/webhook', headers={'Authorization': 'Bearer ' + 'x' * 48}, json={
        'message': {'id': 'event-one', 'type': 'transcript', 'transcriptType': 'final',
            'role': 'user', 'transcript': 'x' * 6001,
            'call': {'id': 'call-one', 'metadata': {'raneen_session_id': sid}}},
    })
    assert result.status_code == 422, result.text
    assert store.all('SELECT * FROM conversation_turns') == []


def test_long_valid_provider_identifiers_fit_generated_idempotency_key(backend):
    c, app, store, owner, admin, pid = backend
    consent(c, owner, pid)
    authorize(c, owner, pid, 'vapi', 'realtime_voice')
    sid = session(c, owner, pid, realtime_provider='vapi')
    result = vapi_transcript(c, sid, 'c' * 160, event_id='e' * 160)
    assert result.status_code == 200, result.text
    assert len(store.all('SELECT * FROM conversation_turns')) == 1


@pytest.mark.parametrize('endpoint', ['versions', 'learning-state'])
def test_reconsent_does_not_restore_admin_access_to_withdrawn_snapshot_evidence(backend, endpoint):
    c, app, store, owner, admin, pid = backend
    consent(c, owner, pid)
    result = c.post('/api/examples', headers=auth(owner), json={
        'profile_id': pid, 'scenario_id': 'purpose-01', 'session_id': 'old-demo',
        'response_text': 'WITHDRAWN PRIVATE DEMONSTRATION', 'transcript_verified': True,
        'action': 'clarify', 'decision_cue': 'Unclear purpose', 'alternative': 'Do not recommend yet',
        'change_condition': 'Purpose becomes clear', 'delivery': 'question', 'pronunciation_notes': '',
    })
    assert result.status_code == 201, result.text
    eid = result.json()['id']
    assert c.post(f'/api/examples/{eid}/review', headers=auth(admin), json={'status': 'approved'}).status_code == 200
    result = c.post(f'/api/profiles/{pid}/learning/import-examples', headers=auth(owner))
    assert result.status_code == 200, result.text
    assert 'WITHDRAWN PRIVATE DEMONSTRATION' in result.text
    assert c.post(f'/api/profiles/{pid}/withdraw', headers=auth(owner)).status_code == 200
    consent(c, owner, pid)
    result = c.get(f'/api/profiles/{pid}/{endpoint}', headers=auth(admin))
    assert result.status_code in (200, 403), result.text
    assert 'WITHDRAWN PRIVATE DEMONSTRATION' not in result.text
