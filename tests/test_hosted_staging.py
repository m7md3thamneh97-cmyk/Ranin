import json
import secrets
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from serve import make_app, read_config, OWNER_ID
from studio.app import create_app

ORIGIN = 'https://studio.example.com'


def environment(tmp_path):
    return {'RANEEN_PUBLIC_ORIGIN': ORIGIN, 'RANEEN_ADMIN_TOKEN': secrets.token_urlsafe(32),
            'RANEEN_DATA_DIR': str(tmp_path), 'RAILWAY_VOLUME_MOUNT_PATH': str(tmp_path), 'PORT': '8080'}


def test_railway_config_valid(tmp_path):
    env = environment(tmp_path)
    root, origin, secret, port = read_config(env, mount_check=lambda _: True)
    assert (root, origin, secret, port) == (tmp_path, ORIGIN, env['RANEEN_ADMIN_TOKEN'], 8080)


def test_railway_generated_domain(tmp_path):
    env = environment(tmp_path)
    del env['RANEEN_PUBLIC_ORIGIN']
    env['RAILWAY_PUBLIC_DOMAIN'] = 'example.up.railway.app'
    assert read_config(env, mount_check=lambda _: True)[1] == 'https://example.up.railway.app'


@pytest.mark.parametrize('bad', ['', 'http://example.com', 'https://*.example.com', 'https://user:secret@example.com', 'https://example.com/path', 'https://example.com:1234', 'https://example.com?q=x'])
def test_invalid_origins_fail_closed(tmp_path, bad):
    env = environment(tmp_path)
    env['RANEEN_PUBLIC_ORIGIN'] = bad
    with pytest.raises(RuntimeError):
        read_config(env, mount_check=lambda _: True)


def test_no_storage_no_start(tmp_path):
    env = environment(tmp_path)
    with pytest.raises(RuntimeError, match='persistent storage'):
        read_config(env, mount_check=lambda _: False)
    del env['RAILWAY_VOLUME_MOUNT_PATH']
    with pytest.raises(RuntimeError, match='persistent storage'):
        read_config(env, mount_check=lambda _: True)


@pytest.mark.parametrize('bad', ['', 'password', 'x' * 43])
def test_weak_admin_credentials_fail_closed(tmp_path, bad):
    env = environment(tmp_path)
    env['RANEEN_ADMIN_TOKEN'] = bad
    with pytest.raises(RuntimeError, match='secret'):
        read_config(env, mount_check=lambda _: True)


@pytest.fixture
def hosted(tmp_path):
    token = secrets.token_urlsafe(32)
    app = make_app(tmp_path, ORIGIN, token)
    return TestClient(app, base_url=ORIGIN), app, token


def auth(token):
    return {'Authorization': 'Bearer ' + token}


def test_health_and_headers(hosted):
    c, app, token = hosted
    assert c.get('/healthz').json() == {'status': 'ok'}
    page = c.get('/')
    assert page.status_code == 200
    assert page.headers['strict-transport-security'] == 'max-age=31536000'
    assert 'noindex' in page.headers['x-robots-tag']
    assert 'frame-ancestors' in page.headers['content-security-policy']


def test_probe_host_limited_to_health(hosted):
    c, _, _ = hosted
    assert c.get('/healthz', headers={'Host': 'healthcheck.railway.app'}).status_code == 200
    assert c.get('/', headers={'Host': 'healthcheck.railway.app'}).status_code == 404
    assert c.get('/', headers={'Host': 'evil.example.com'}).status_code == 400


def test_unauthenticated_upload_rejected_before_parsing(hosted):
    c, _, _ = hosted
    assert c.post('/api/profiles/unknown/audio', content=b'not wav').status_code == 401
    assert c.get('/api/me').status_code == 401


def test_owner_can_access_but_cannot_invite(hosted):
    c, _, token = hosted
    assert c.get('/api/me', headers=auth(token)).json()['id'] == OWNER_ID
    assert c.post('/api/users', headers=auth(token), json={'name': 'Employee'}).status_code == 403


def test_other_contributors_blocked_on_staging(hosted):
    c, app, _ = hosted
    employee = app.state.store.create_user('Test contributor')
    assert c.get('/api/me', headers=auth(employee['token'])).status_code == 403


def test_origin_comparison_works_behind_https_terminating_proxy(hosted):
    c, _, token = hosted
    headers = auth(token) | {'Origin': ORIGIN}
    assert c.post('/api/profiles', headers=headers, json={'name':'Owner', 'dialect':'Jordanian'}).status_code == 201
    headers['Origin'] = 'https://attacker.example'
    assert c.post('/api/profiles', headers=headers, json={'name':'Owner', 'dialect':'Jordanian'}).status_code == 403


def test_restart_keeps_data_and_rotates_managed_secret(tmp_path, capsys):
    first = secrets.token_urlsafe(32)
    app = make_app(tmp_path, ORIGIN, first)
    c = TestClient(app, base_url=ORIGIN)
    result = c.post('/api/profiles', headers=auth(first), json={'name':'Owner', 'dialect':'Jordanian'})
    assert result.status_code == 201
    second = secrets.token_urlsafe(32)
    app = make_app(tmp_path, ORIGIN, second)
    c = TestClient(app, base_url=ORIGIN)
    assert c.get('/api/me', headers=auth(first)).status_code == 401
    assert c.get('/api/profiles', headers=auth(second)).json()['items'][0]['id'] == result.json()['id']
    captured = capsys.readouterr()
    assert first not in captured.out + captured.err
    assert second not in captured.out + captured.err
    stored = app.state.store.one('SELECT token_hash FROM users WHERE id=?', (OWNER_ID,))['token_hash']
    assert stored != second


def test_local_default_still_rejects_public_host(tmp_path):
    c = TestClient(create_app(tmp_path), base_url=ORIGIN)
    assert c.get('/').status_code == 400
