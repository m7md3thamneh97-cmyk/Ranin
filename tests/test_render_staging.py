"""Render migration regression tests. No cloud account or real voice data used."""
import base64
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from serve import OWNER_ID, is_data_mount, make_app, read_config, valid_admin_token

ORIGIN = 'https://raneen-example.onrender.com'


def render_env(root):
    return {
        'RENDER': 'true', 'RENDER_EXTERNAL_URL': ORIGIN,
        'RANEEN_DATA_DIR': str(root), 'RANEEN_VOLUME_MOUNT_PATH': str(root),
        'RANEEN_ADMIN_TOKEN': base64.b64encode(secrets.token_bytes(32)).decode('ascii'),
        'PORT': '8080',
    }


def test_render_defaults_do_not_need_railway_variables(tmp_path):
    env = render_env(tmp_path)
    assert read_config(env, lambda _: True) == (tmp_path, ORIGIN, env['RANEEN_ADMIN_TOKEN'], 8080)


def test_custom_origin_overrides_render_url(tmp_path):
    env = render_env(tmp_path)
    env['RANEEN_PUBLIC_ORIGIN'] = 'https://studio.example.com/'
    assert read_config(env, lambda _: True)[1] == 'https://studio.example.com'


@pytest.mark.parametrize('bad', ['http://x.onrender.com', 'https://user:password@x.onrender.com',
                                'https://x.onrender.com/a', 'https://x.onrender.com?q=1',
                                'https://x.onrender.com#f', 'https://*.onrender.com',
                                'https://x.onrender.com:443', 'https://x.onrender.com:bad',
                                'https://[invalid', 'https://x.onrender.com\n',
                                'https://x.onrender.com\\evil', 'https://x.onrender.com:0'])
def test_bad_render_origins_rejected(tmp_path, bad):
    env = render_env(tmp_path)
    env['RENDER_EXTERNAL_URL'] = bad
    with pytest.raises(RuntimeError, match='HTTPS'):
        read_config(env, lambda _: True)


def test_mount_declaration_does_not_bypass_actual_mount_check(tmp_path):
    with pytest.raises(RuntimeError, match='persistent storage'):
        read_config(render_env(tmp_path), lambda _: False)


def test_render_flag_does_not_imply_persistence(tmp_path):
    env = render_env(tmp_path)
    del env['RANEEN_VOLUME_MOUNT_PATH']
    with pytest.raises(RuntimeError, match='persistent storage'):
        read_config(env, lambda _: True)


def test_conflicting_mount_declarations_fail(tmp_path):
    env = render_env(tmp_path)
    env['RAILWAY_VOLUME_MOUNT_PATH'] = '/different'
    with pytest.raises(RuntimeError, match='persistent storage'):
        read_config(env, lambda _: True)


def test_wrong_mount_path_fails(tmp_path):
    env = render_env(tmp_path)
    env['RANEEN_VOLUME_MOUNT_PATH'] = str(tmp_path / 'other')
    with pytest.raises(RuntimeError, match='persistent storage'):
        read_config(env, lambda _: True)


def test_symlink_data_path_fails(tmp_path):
    actual = tmp_path / 'real'
    actual.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(RuntimeError, match='persistent storage'):
        read_config(render_env(link), lambda _: True)


@pytest.mark.parametrize('bad', ['0', '65536', 'abc', '-1', '1.5'])
def test_invalid_port_fails(tmp_path, bad):
    env = render_env(tmp_path)
    env['PORT'] = bad
    with pytest.raises(RuntimeError, match='PORT'):
        read_config(env, lambda _: True)


def test_render_generated_base64_token_including_special_characters():
    # Known pattern is test-only, never a deployed credential.
    value = base64.b64encode(bytes(range(30)) + b'\xfb\xff').decode('ascii')
    assert any(c in value for c in '+/') and value.endswith('=')
    assert valid_admin_token(value)


@pytest.mark.parametrize('bad', ['', 'password', 'x' * 64, 'abc' * 22, 'A+B/C=' * 20,
                                'a' * 42, 'alpha ' + 'A' * 60, 'a' * 257])
def test_invalid_tokens_fail(bad):
    assert not valid_admin_token(bad)


def test_render_owner_auth_and_restart(tmp_path, capsys):
    token = render_env(tmp_path)['RANEEN_ADMIN_TOKEN']
    auth = {'Authorization': 'Bearer ' + token, 'Origin': ORIGIN}
    with TestClient(make_app(tmp_path, ORIGIN, token), base_url=ORIGIN) as c:
        assert c.get('/healthz').status_code == 200
        assert c.get('/api/me').status_code == 401
        assert c.get('/api/me', headers=auth).json()['id'] == OWNER_ID
        assert c.post('/api/users', headers=auth, json={'name': 'Employee'}).status_code == 403
        profile = c.post('/api/profiles', headers=auth,
                         json={'name': 'Synthetic test owner', 'dialect': 'Jordanian'})
        assert profile.status_code == 201
        assert c.post('/api/profiles', headers=auth | {'Origin': 'https://evil.example'},
                      json={'name': 'Other', 'dialect': 'Jordanian'}).status_code == 403
    with TestClient(make_app(tmp_path, ORIGIN, token), base_url=ORIGIN) as c:
        assert c.get('/api/profiles', headers=auth).json()['items'][0]['id'] == profile.json()['id']
    output = capsys.readouterr()
    assert token not in output.out + output.err


def mountinfo(monkeypatch, text):
    original = Path.read_text
    def read(self, *args, **kwargs):
        return text if str(self) == '/proc/self/mountinfo' else original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)


def test_linux_same_device_bind_mount_detected(tmp_path, monkeypatch):
    mountinfo(monkeypatch, f'90 23 8:1 /persistent {tmp_path} rw,relatime - ext4 /dev/sda1 rw\n')
    assert is_data_mount(str(tmp_path))


@pytest.mark.parametrize('filesystem', ['tmpfs', 'ramfs', 'overlay'])
def test_ephemeral_mount_rejected(tmp_path, monkeypatch, filesystem):
    mountinfo(monkeypatch, f'90 23 0:1 / {tmp_path} rw - {filesystem} none rw\n')
    assert not is_data_mount(str(tmp_path))


def test_readonly_mount_rejected(tmp_path, monkeypatch):
    mountinfo(monkeypatch, f'90 23 8:1 / {tmp_path} ro - ext4 /dev/sda1 ro\n')
    assert not is_data_mount(str(tmp_path))


def test_unmounted_directory_rejected(tmp_path, monkeypatch):
    mountinfo(monkeypatch, '23 1 0:1 / / rw - overlay overlay rw\n')
    assert not is_data_mount(str(tmp_path))


def test_linux_mountpoint_with_spaces(tmp_path, monkeypatch):
    folder = tmp_path / 'data directory'
    folder.mkdir()
    encoded = str(folder).replace(' ', r'\040')
    mountinfo(monkeypatch, f'90 23 8:1 / {encoded} rw - ext4 /dev/sda1 rw\n')
    assert is_data_mount(str(folder))
