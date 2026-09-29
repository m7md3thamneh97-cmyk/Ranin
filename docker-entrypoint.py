"""Prepare the dedicated volume, then permanently drop root before serving HTTP."""
import os
from pathlib import Path
import sys
from serve import read_config

root, _, _, _ = read_config()
os.umask(0o077)
if os.geteuid() == 0:
    root.mkdir(parents=True, exist_ok=True)
    audio = root / 'audio'
    audio.mkdir(exist_ok=True)
    for directory in (root, audio):
        if directory.is_symlink():
            raise RuntimeError('The dedicated data directories must not be symlinks.')
        os.chown(directory, 10001, 10001)
        os.chmod(directory, 0o700)
    for name in ('studio.sqlite3', 'studio.sqlite3-wal', 'studio.sqlite3-shm'):
        file = root / name
        if file.is_symlink():
            raise RuntimeError('Database files must not be symlinks.')
        if file.exists():
            os.chown(file, 10001, 10001)
            os.chmod(file, 0o600)
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)
os.execv(sys.executable, [sys.executable, '/app/serve.py'])
