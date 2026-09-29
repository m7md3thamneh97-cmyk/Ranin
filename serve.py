"""Owner-only Railway staging launcher. Refuses missing storage, origin or credentials."""
from __future__ import annotations
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
import uvicorn
from studio.app import create_app, token_hash, now

OWNER_ID = 'deployment-owner'


def read_config(env=None, mount_check=os.path.ismount):
    env = os.environ if env is None else env
    domain = env.get('RAILWAY_PUBLIC_DOMAIN', '')
    origin = env.get('RANEEN_PUBLIC_ORIGIN') or ('https://' + domain if domain else '')
    parsed = urlsplit(origin)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.port
            or '*' in parsed.netloc or any(c.isspace() for c in parsed.netloc)):
        raise RuntimeError('Configure one canonical HTTPS public origin before starting staging.')
    origin = 'https://' + parsed.hostname
    token = env.get('RANEEN_ADMIN_TOKEN', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,256}', token) or len(set(token)) < 12:
        raise RuntimeError('Set RANEEN_ADMIN_TOKEN to a securely generated 32-byte-or-longer URL-safe secret.')
    root = Path(env.get('RANEEN_DATA_DIR', '/data')).resolve()
    declared = env.get('RAILWAY_VOLUME_MOUNT_PATH', '')
    if not declared or root != Path(declared).resolve() or not mount_check(str(root)):
        raise RuntimeError('Attach persistent storage exactly at RANEEN_DATA_DIR before starting staging.')
    port = int(env.get('PORT', '8080'))
    if not 1 <= port <= 65535:
        raise RuntimeError('PORT must be between 1 and 65535.')
    return root, origin, token, port


def make_app(root: Path, origin: str, token: str):
    app = create_app(root, public_origin=origin, owner_only=True)
    # Only the managed owner's credential rotates. It is never printed or exported.
    app.state.store.execute(
        "INSERT INTO users(id,name,role,token_hash,created) VALUES(?,?,'admin',?,?) "
        "ON CONFLICT(id) DO UPDATE SET token_hash=excluded.token_hash, role='admin'",
        (OWNER_ID, 'Workspace owner', token_hash(token), now()))
    return app


def main():
    os.umask(0o077)
    root, origin, token, port = read_config()
    app = make_app(root, origin, token)
    # Railway terminates TLS. Origin checks use explicit config, not untrusted proxy headers.
    uvicorn.run(app, host='0.0.0.0', port=port, workers=1, access_log=False,
                proxy_headers=False, server_header=False, limit_concurrency=32,
                timeout_keep_alive=5)


if __name__ == '__main__':
    main()
