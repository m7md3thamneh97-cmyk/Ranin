"""Hosted route boundaries use isolated synthetic account/configuration fixtures."""
from fastapi.testclient import TestClient

from studio.app import create_app as foundation
from studio.runtime import create_app


ORIGIN = 'https://raneen.example'
CONSENT = dict(consent_version='becoming-v1', own_voice=True, recording=True,
               external_processing=True, voice_cloning=True, private_preview=True,
               language='en')


def configure(monkeypatch, enabled='1'):
    monkeypatch.setenv('RANEEN_BECOMING_ENABLED', enabled)
    monkeypatch.setenv('VAPI_API_KEY', 'synthetic-vapi-fixture')
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'synthetic-eleven-fixture')
    monkeypatch.setenv('RANEEN_VAPI_TEMPLATE_ID', '00000000-0000-4000-8000-000000000001')


def test_new_home_and_original_studio_routes(tmp_path, monkeypatch):
    configure(monkeypatch)
    with TestClient(create_app(tmp_path, public_origin=ORIGIN, owner_only=True), base_url=ORIGIN) as client:
        home = client.get('/')
        assert home.status_code == 200
        assert '/static/become.js' in home.text
        assert client.get('/create').status_code == 200
        assert client.get('/enroll').status_code == 200
        assert client.get('/studio').status_code == 200
        assert 'wss://*.daily.co' in home.headers['content-security-policy']
        assert 'frame-ancestors \'none\'' in home.headers['content-security-policy']
        assert home.headers['cache-control'] == 'no-store'


def test_public_capability_api_never_opens_owner_apis(tmp_path, monkeypatch):
    configure(monkeypatch)
    with TestClient(create_app(tmp_path, public_origin=ORIGIN, owner_only=True), base_url=ORIGIN) as client:
        assert client.get('/api/becoming/readiness').status_code == 200
        for path in ('/api/me', '/api/profiles', '/api/enrollment/sessions',
                     '/api/becomingevil/readiness', '/api/becoming/readiness/extra'):
            assert client.get(path).status_code == 401
        missing = client.get('/api/becoming/sessions/' + '0' * 32)
        assert missing.status_code == 401
        created = client.post('/api/becoming/sessions', json=CONSENT, headers={'Origin': ORIGIN})
        assert created.status_code == 201
        ident = created.json()['id']
        assert 'HttpOnly' in created.headers['set-cookie']
        assert 'Secure' in created.headers['set-cookie']
        assert 'SameSite=strict' in created.headers['set-cookie']
        assert client.get('/api/becoming/sessions/' + ident).status_code == 200
        assert client.get('/api/becoming/sessions/' + ident,
                          headers={'Authorization': 'Bearer invalid-fixture'}).status_code == 401
        assert client.get('/api/becoming/sessions/' + '1' * 32).status_code == 401


def test_start_requires_explicit_consent_and_same_origin(tmp_path, monkeypatch):
    configure(monkeypatch)
    with TestClient(create_app(tmp_path, public_origin=ORIGIN, owner_only=True), base_url=ORIGIN) as client:
        denied = dict(CONSENT, own_voice=False)
        assert client.post('/api/becoming/sessions', json=denied, headers={'Origin': ORIGIN}).status_code == 403
        assert client.post('/api/becoming/sessions', json=CONSENT).status_code == 403
        assert client.post('/api/becoming/sessions', json=CONSENT,
                           headers={'Origin': 'https://another.example'}).status_code == 403
        assert client.app.state.store.one('SELECT COUNT(*) n FROM becoming_sessions')['n'] == 0


def test_additive_install_preserves_existing_evidence(tmp_path, monkeypatch):
    configure(monkeypatch, '0')
    prior = foundation(tmp_path)
    user = prior.state.store.create_user('Synthetic contributor')
    prior.state.store.execute('INSERT INTO profiles VALUES(?,?,?,?,?,?,?)',
                             ('a' * 32, user['id'], 'Synthetic profile', 'Gulf Arabic', 'both', 'fixture', '2026-01-01'))
    app = create_app(tmp_path, public_origin=ORIGIN, owner_only=True)
    assert app.state.store.one('SELECT name FROM profiles WHERE id=?', ('a' * 32,))['name'] == 'Synthetic profile'
    assert [item['version'] for item in app.state.store.all('SELECT version FROM becoming_schema_versions ORDER BY version')] == [10, 11]
    repeated = create_app(tmp_path, public_origin=ORIGIN, owner_only=True)
    assert [item['version'] for item in repeated.state.store.all('SELECT version FROM becoming_schema_versions ORDER BY version')] == [10, 11]


def test_previous_enrollment_flag_cannot_enable_anonymous_calls(tmp_path, monkeypatch):
    configure(monkeypatch, '0')
    monkeypatch.setenv('RANEEN_VOICE_ENROLLMENT_ENABLED', '1')
    with TestClient(create_app(tmp_path, public_origin=ORIGIN, owner_only=True), base_url=ORIGIN) as client:
        assert client.get('/api/becoming/readiness').json()['enabled'] is False
        assert client.post('/api/becoming/sessions', json=CONSENT,
                           headers={'Origin': ORIGIN}).status_code == 404
