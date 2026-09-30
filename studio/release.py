"""Preserve the existing database before a hosted release applies additive schema."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import sqlite3


def backup_before_release(root: Path, revision: str | None) -> Path | None:
    source = root / 'studio.sqlite3'
    if not source.exists() or not revision:
        return None
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise RuntimeError('Invalid release revision; database backup was not attempted.')
    directory = root / 'release-backups'
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / (revision + '.sqlite3')
    if target.exists():
        return target
    if shutil.disk_usage(root).free < source.stat().st_size * 2 + 64 * 1024 * 1024:
        raise RuntimeError('Insufficient private disk space for the pre-release database backup.')
    temporary = target.with_suffix('.partial')
    try:
        with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as original:
            with sqlite3.connect(temporary) as backup:
                original.backup(backup, pages=256)
                if backup.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Pre-release database backup validation failed.')
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)
