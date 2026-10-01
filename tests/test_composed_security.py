"""Security checks for composing enrollment with the personal learning backend."""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from studio.app import CONSENT_VERSION, SCHEMA, token_hash
from studio.runtime import create_app
from studio.teaching import SCHEMA as TEACHING_SCHEMA
from studio.enrollment import SCHEMA as ENROLLMENT_SCHEMA


def auth(user):
    return {'Authorization': 'Bearer ' + user['token']}


def start_enrollment(client, owner):
    result = client.post('/api/enrollment/sessions', headers=auth(owner), json={
        'recording': True, 'external_processing': True, 'voice_cloning': True,
        'private_preview': True, 'self_attestation': True,
    })
    assert result.status_code == 201, result.text
    ident = result.json()['id']
    result = client.get(f'/api/enrollment/sessions/{ident}/workflow', headers=auth(owner))
    assert result.status_code == 200, result.text
    learning = result.json()['learning']
    assert learning['profile_id'] and learning['session_id']
    return ident, learning


@pytest.fixture
def composed(tmp_path, monkeypatch):
    monkeypatch.setenv('RANEEN_LEARNING_STUDIO_ENABLED', '1')
    monkeypatch.setenv('RANEEN_VOICE_ENROLLMENT_ENABLED', '1')
    monkeypatch.setenv('RANEEN_LEARNING_PROVIDER', 'local')
    monkeypatch.setenv('RANEEN_PUBLIC_ORIGIN', 'https://raneen.test')
    monkeypatch.setenv('VAPI_API_KEY', 'synthetic-private-vapi-key')
    monkeypatch.setenv('VAPI_PRIVATE_KEY', 'synthetic-private-vapi-key')
    monkeypatch.setenv('VAPI_PUBLIC_API_KEY', 'synthetic-reusable-browser-key')
    monkeypatch.setenv('VAPI_PUBLIC_KEY', 'synthetic-reusable-browser-key')
    monkeypatch.setenv('RANEEN_VAPI_PUBLIC_KEY_RESTRICTED', '1')
    monkeypatch.setenv('RANEEN_VAPI_WEBHOOK_SECRET', 'x' * 48)
    monkeypatch.setenv('RANEEN_VAPI_VOICE_ID', 'abcdefghijklmnopqrst')
    app = create_app(tmp_path, public_origin='https://raneen.test', owner_only=True)
    owner = app.state.store.create_user('Synthetic owner', 'admin')
    other = app.state.store.create_user('Synthetic other owner', 'admin')
    client = TestClient(app, base_url='https://raneen.test')
    result = client.post('/api/profiles', headers=auth(owner), json={
        'name': 'Synthetic speaker', 'dialect': 'Emirati Arabic', 'contribution': 'both', 'style_notes': '',
    })
    assert result.status_code == 201, result.text
    pid = result.json()['id']
    result = client.post(f'/api/profiles/{pid}/consent', headers=auth(owner), json={
        'version': CONSENT_VERSION, 'collection': True, 'behavior_export': True,
        'voice_export': True, 'self_attestation': True,
    })
    assert result.status_code == 201, result.text
    return client, app, owner, other, pid


def test_composed_runtime_cannot_return_reusable_vapi_credentials(composed, monkeypatch):
    c, app, owner, other, pid = composed
    for provider, scope in [('vapi', 'realtime_voice')]:
        result = c.post(f'/api/profiles/{pid}/provider-authorizations', headers=auth(owner), json={
            'provider': provider, 'scope': scope, 'self_attestation': True,
        })
        assert result.status_code == 201, result.text
    result = c.post('/api/sessions', headers=auth(owner), json={'profile_id': pid})
    assert result.status_code == 201, result.text
    sid = result.json()['id']
    calls = []

    def create_assistant(self, config):
        calls.append(config)
        return 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'

    monkeypatch.setattr('studio.providers.VapiProvider.create_assistant', create_assistant)
    result = c.get(f'/api/sessions/{sid}/transport', headers=auth(owner))
    assert result.status_code in (403, 404, 410), result.text
    assert 'synthetic-reusable-browser-key' not in result.text
    assert 'synthetic-private-vapi-key' not in result.text
    assert calls == []


def test_composed_runtime_cannot_clone_outside_enrollment_operation_caps(composed, monkeypatch):
    c, app, owner, other, pid = composed
    result = c.post(f'/api/profiles/{pid}/provider-authorizations', headers=auth(owner), json={
        'provider': 'elevenlabs', 'scope': 'voice_clone', 'self_attestation': True,
    })
    assert result.status_code == 201, result.text
    calls = []

    def candidate(*args, **kwargs):
        calls.append((args, kwargs))
        return {'id': 'synthetic-unguarded-voice'}

    monkeypatch.setattr(app.state.voice, 'create_candidate', candidate)
    result = c.post(f'/api/profiles/{pid}/voice/candidate', headers=auth(owner), json={})
    assert result.status_code in (403, 404, 410), result.text
    assert calls == []


def test_composed_backend_profile_mutations_remain_owner_scoped(composed):
    c, app, owner, other, pid = composed
    result = c.post('/api/sessions', headers=auth(other), json={'profile_id': pid})
    assert result.status_code == 403
    result = c.post(f'/api/profiles/{pid}/provider-authorizations', headers=auth(other), json={
        'provider': 'openai', 'scope': 'text_learning', 'self_attestation': True,
    })
    assert result.status_code == 403
    assert app.state.store.all('SELECT * FROM teaching_sessions') == []
    assert app.state.store.all('SELECT * FROM provider_authorizations') == []


def test_raw_browser_turn_cannot_enter_enrollment_bound_learning(composed):
    c, app, owner, other, pid = composed
    ident, learning = start_enrollment(c, owner)
    result = c.post(f"/api/sessions/{learning['session_id']}/turns", headers=auth(owner), json={
        'role': 'trainer', 'transcript': 'I first ask about investment purpose.', 'analyze': False,
    })
    assert result.status_code in (403, 409, 410), result.text
    assert app.state.store.all('SELECT * FROM conversation_turns WHERE session_id=?', (learning['session_id'],)) == []


@pytest.mark.parametrize('route', ['sessions', 'examples', 'preferences'])
def test_body_profile_id_cannot_bypass_enrollment_trusted_evidence_guard(composed, route):
    c, app, owner, other, pid = composed
    ident, learning = start_enrollment(c, owner)
    bodies = {
        'sessions': {'profile_id': learning['profile_id']},
        'examples': {
            'profile_id': learning['profile_id'], 'scenario_id': 'purpose-01', 'session_id': 'untrusted-browser-demo',
            'response_text': 'Untrusted typed demo', 'transcript_verified': True, 'action': 'clarify',
            'decision_cue': 'Untrusted typed cue', 'alternative': 'Untrusted typed alternative',
            'change_condition': 'Untrusted typed condition', 'delivery': 'question', 'pronunciation_notes': '',
        },
        'preferences': {
            'profile_id': learning['profile_id'], 'scenario_id': 'purpose-01', 'candidate_a': 'A',
            'candidate_b': 'B', 'preferred': 'b', 'reason': 'Untrusted typed preference',
            'replacement': '', 'source': 'Untrusted browser input',
        },
    }
    result = c.post(f'/api/{route}', headers=auth(owner), json=bodies[route])
    assert result.status_code in (403, 409, 410), result.text


def test_enrollment_consent_cannot_be_replaced_by_legacy_export_permissions(composed):
    c, app, owner, other, pid = composed
    ident, learning = start_enrollment(c, owner)
    before = app.state.store.all('SELECT * FROM consents WHERE profile_id=?', (learning['profile_id'],))
    assert before and all(not r['behavior_export'] and not r['voice_export'] for r in before)
    result = c.post(f"/api/profiles/{learning['profile_id']}/consent", headers=auth(owner), json={
        'version': CONSENT_VERSION, 'collection': True, 'behavior_export': True,
        'voice_export': True, 'self_attestation': True,
    })
    assert result.status_code in (403, 409, 410), result.text
    assert app.state.store.all('SELECT * FROM consents WHERE profile_id=?', (learning['profile_id'],)) == before


def test_revoke_enrollment_immediately_blocks_its_generic_learning_reads(composed):
    c, app, owner, other, pid = composed
    ident, learning = start_enrollment(c, owner)
    for owner_credentials in (auth(owner), auth(other)):
        result = c.get(f"/api/profiles/{learning['profile_id']}/learning-state", headers=owner_credentials)
        assert result.status_code == (200 if owner_credentials == auth(owner) else 403), result.text
    result = c.post(f'/api/enrollment/sessions/{ident}/revoke', headers=auth(owner), json={'confirm': True})
    assert result.status_code == 200, result.text
    for endpoint in (f"/api/profiles/{learning['profile_id']}/learning-state",
                     f"/api/profiles/{learning['profile_id']}/runtime",
                     f"/api/sessions/{learning['session_id']}/turns"):
        result = c.get(endpoint, headers=auth(owner))
        assert result.status_code in (409, 410), result.text


def test_legacy_admin_profile_listing_does_not_reveal_other_private_enrollments(composed):
    c, app, owner, other, pid = composed
    ident, learning = start_enrollment(c, owner)
    result = c.get('/api/profiles', headers=auth(other))
    assert result.status_code == 200, result.text
    assert learning['profile_id'] not in {item['id'] for item in result.json()['items']}


def test_composed_migrations_preserve_existing_foundation_teaching_and_enrollment(tmp_path):
    # Construct the pre-learning-engine database directly. Reopening with Store
    # must add the backend tables, not rebuild any established evidence table.
    token = 'synthetic-migration-owner-token'
    with sqlite3.connect(tmp_path / 'studio.sqlite3') as db:
        db.executescript(SCHEMA)
        db.executescript(TEACHING_SCHEMA)
        db.executescript(ENROLLMENT_SCHEMA)
        db.execute('INSERT INTO users VALUES(?,?,?,?,?)', ('old-owner', 'Synthetic owner', 'admin', token_hash(token), 'today'))
        db.execute('INSERT INTO profiles VALUES(?,?,?,?,?,?,?)', ('old-profile', 'old-owner', 'Synthetic speaker', 'Emirati Arabic', 'both', '', 'today'))
        db.execute('INSERT INTO consents VALUES(?,?,?,?,?,?,?,?,?)', ('old-consent', 'old-profile', CONSENT_VERSION, 1, 1, 0, 'synthetic consent text', 'today', None))
        db.execute('INSERT INTO examples(id,profile_id,consent_id,payload,status,created) VALUES(?,?,?,?,?,?)',
                   ('old-example', 'old-profile', 'old-consent', json.dumps({'response_text': 'Synthetic Arabic: شو الأهم عندك؟'}), 'pending', 'today'))
        db.execute('INSERT INTO teaching_packs VALUES(?,?,?,?,?)', ('old-pack', 'old-profile', '{}', 'synthetic-digest', 'today'))
        db.execute('''INSERT INTO enrollment_sessions(id,owner_id,state,consent_version,consent_json,created,updated)
            VALUES(?,?,?,?,?,?,?)''', ('old-enrollment', 'old-owner', 'collecting', 'voice-enrollment-v1', '{}', 'today', 'today'))
    app = create_app(tmp_path)
    store = app.state.store
    assert store.one('SELECT payload FROM examples WHERE id=?', ('old-example',))['payload'] == json.dumps({'response_text': 'Synthetic Arabic: شو الأهم عندك؟'})
    assert store.one('SELECT id FROM teaching_packs WHERE id=?', ('old-pack',)) is not None
    assert store.one('SELECT id FROM enrollment_sessions WHERE id=?', ('old-enrollment',)) is not None
    before = store.all('SELECT * FROM schema_migrations ORDER BY name')
    assert len(before) >= 5
    app = create_app(tmp_path)
    assert app.state.store.all('SELECT * FROM schema_migrations ORDER BY name') == before
    assert app.state.store.all('PRAGMA foreign_key_check') == []
    client = TestClient(app)
    assert client.get('/api/me', headers={'Authorization': 'Bearer ' + token}).status_code == 200
