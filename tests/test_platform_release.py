"""Nonsecret deployment diagnostics and owner landing navigation."""
from fastapi.testclient import TestClient
from studio.app import create_app
from studio.release import backup_before_release
import sqlite3


def test_readiness_never_exposes_credentials(tmp_path, monkeypatch):
    for key in ('OPENAI_API_KEY', 'ELEVENLABS_API_KEY', 'VAPI_API_KEY', 'RANEEN_ADMIN_TOKEN'):
        monkeypatch.setenv(key, 'synthetic-private-value-not-for-output')
    monkeypatch.setenv('RENDER_GIT_COMMIT', 'a' * 40)
    monkeypatch.setenv('RANEEN_VOICE_ENROLLMENT_ENABLED', '1')
    with TestClient(create_app(tmp_path)) as client:
        response = client.get('/readyz')
        data = response.json()
        assert response.status_code == 200
        assert data['revision'] == 'a' * 40
        assert data['enrollment_enabled'] is True
        assert all(data['providers_configured'].values())
        assert data['provider_access_verified'] is False
        assert 'synthetic-private-value' not in response.text
        assert 'deployment-owner' not in response.text
        assert response.headers['cache-control'] == 'no-store'


def test_readiness_whitespace_keys_are_not_configured(tmp_path, monkeypatch):
    for key in ('OPENAI_API_KEY', 'ELEVENLABS_API_KEY', 'VAPI_API_KEY'):
        monkeypatch.setenv(key, '  ')
    monkeypatch.setenv('RENDER_GIT_COMMIT', 'malformed-value')
    with TestClient(create_app(tmp_path)) as client:
        data = client.get('/readyz').json()
        assert not any(data['providers_configured'].values())
        assert data['revision'] is None


def test_platform_home_retains_legacy_studio(tmp_path, monkeypatch):
    monkeypatch.setenv('RANEEN_PLATFORM_HOME', '1')
    with TestClient(create_app(tmp_path)) as client:
        response = client.get('/', follow_redirects=False)
        assert response.status_code == 307
        assert response.headers['location'] == '/enroll'
        assert client.get('/studio').status_code == 200


def test_release_backup_preserves_original_and_is_not_overwritten(tmp_path):
    source = tmp_path / 'studio.sqlite3'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE examples (text TEXT)')
        db.execute("INSERT INTO examples VALUES ('synthetic original')")
    target = backup_before_release(tmp_path, 'b' * 40)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE examples SET text='synthetic changed'")
    assert backup_before_release(tmp_path, 'b' * 40) == target
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT text FROM examples').fetchone()[0] == 'synthetic original'
    assert target.stat().st_mode & 0o777 == 0o600
