"""Owner-only hosted staging launcher for Render and Railway.

Refuse to start without a canonical HTTPS origin, a dedicated mounted data
filesystem, and an explicitly supplied strong owner credential. No cloud API
credentials are needed to start. Optional AI calls require separately configured keys.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

import uvicorn
from studio.app import token_hash, now
from studio.runtime import create_app
from studio.release import backup_before_release

OWNER_ID = 'deployment-owner'


def is_data_mount(path: str) -> bool:
    """Recognize a dedicated Linux disk/bind mount without accepting tmpfs.

    os.path.ismount alone can miss bind mounts on the same filesystem. Render's
    configured persistent disk must also be verified in its control plane; an
    environment variable or a directory's existence is never sufficient here.
    """
    root = Path(path)
    if not root.is_absolute() or root.is_symlink() or root.resolve() == Path('/'):
        return False
    target = str(root.resolve())
    try:
        mountinfo = Path('/proc/self/mountinfo').read_text(encoding='utf-8')
    except OSError:
        return os.path.ismount(target)
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(' - ')
        fields, filesystem = before.split(), after.split()
        if not separator or len(fields) < 6 or not filesystem:
            continue
        mountpoint = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), fields[4])
        if mountpoint == target:
            return filesystem[0] not in {
                'tmpfs', 'ramfs', 'overlay', 'proc', 'sysfs', 'devtmpfs',
                'cgroup', 'cgroup2', 'squashfs',
            } and 'rw' in fields[5].split(',')
    return False


def valid_admin_token(token: str) -> bool:
    """Accept existing URL-safe secrets and Render's base64-generated secrets.

    This checks format, not entropy. Provision tokens with a cryptographically
    secure generator; never use a human-chosen password in this field.
    """
    if len(set(token)) < 12:
        return False
    if re.fullmatch(r'[A-Za-z0-9_-]{43,256}', token):
        return True
    if not re.fullmatch(r'[A-Za-z0-9+/]{43,254}={0,2}', token) or len(token) > 256:
        return False
    try:
        decoded = base64.b64decode(token, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(decoded) >= 32 and base64.b64encode(decoded).decode('ascii') == token


def read_config(
    env: Mapping[str, str] | None = None,
    mount_check: Callable[[str], bool] = is_data_mount,
) -> tuple[Path, str, str, int]:
    env = os.environ if env is None else env
    domain = env.get('RAILWAY_PUBLIC_DOMAIN', '')
    origin = (env.get('RANEEN_PUBLIC_ORIGIN') or env.get('RENDER_EXTERNAL_URL')
              or ('https://' + domain if domain else ''))
    try:
        parsed = urlsplit(origin)
        valid_origin = (
            parsed.scheme == 'https' and bool(parsed.hostname)
            and not parsed.username and not parsed.password
            and parsed.path in ('', '/') and not parsed.query and not parsed.fragment
            and parsed.port is None and '*' not in parsed.netloc
            and not any(c.isspace() or ord(c) < 32 for c in origin)
            and '\\' not in origin
        )
    except ValueError:
        valid_origin = False
    if not valid_origin:
        raise RuntimeError('Configure one canonical HTTPS public origin before starting staging.')
    origin = 'https://' + parsed.hostname
    token = env.get('RANEEN_ADMIN_TOKEN', '')
    if not valid_admin_token(token):
        raise RuntimeError('Set RANEEN_ADMIN_TOKEN to a securely generated 32-byte-or-longer secret.')
    raw_root = Path(env.get('RANEEN_DATA_DIR', '/data'))
    declarations = [env[key] for key in ('RANEEN_VOLUME_MOUNT_PATH', 'RAILWAY_VOLUME_MOUNT_PATH')
                    if env.get(key)]
    root = raw_root.resolve()
    if (not raw_root.is_absolute() or raw_root.is_symlink() or root == Path('/')
            or not declarations
            or any(not Path(value).is_absolute() or Path(value).resolve() != root
                   for value in declarations)
            or not mount_check(str(root))):
        raise RuntimeError('Attach persistent storage exactly at RANEEN_DATA_DIR before starting staging.')
    try:
        port = int(env.get('PORT', '8080'))
    except (TypeError, ValueError) as exc:
        raise RuntimeError('PORT must be between 1 and 65535.') from exc
    if not 1 <= port <= 65535:
        raise RuntimeError('PORT must be between 1 and 65535.')
    return root, origin, token, port


def make_app(root: Path, origin: str, token: str):
    backup_before_release(root, os.environ.get('RENDER_GIT_COMMIT'))
    app = create_app(root, public_origin=origin, owner_only=True)
    # Only the managed owner's credential rotates. Never print or export it.
    app.state.store.execute(
        "INSERT INTO users(id,name,role,token_hash,created) VALUES(?,?,'admin',?,?) "
        "ON CONFLICT(id) DO UPDATE SET token_hash=excluded.token_hash, role='admin'",
        (OWNER_ID, 'Workspace owner', token_hash(token), now()))
    return app


def main():
    os.umask(0o077)
    root, origin, token, port = read_config()
    app = make_app(root, origin, token)
    # The hosting platform terminates TLS. Do not trust arbitrary proxy headers.
    uvicorn.run(app, host='0.0.0.0', port=port, workers=1, access_log=False,
                proxy_headers=False, server_header=False, limit_concurrency=32,
                timeout_keep_alive=5)


if __name__ == '__main__':
    main()
