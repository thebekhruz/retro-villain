"""Runtime paths and conservative permissions for local application data."""

import os
from collections.abc import Mapping
from pathlib import Path


def secure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def secure_file(path: Path) -> Path:
    if path.exists():
        path.chmod(0o600)
    return path


def resolve_data_dir(explicit: str = '', environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    if explicit:
        path = Path(explicit).expanduser()
    elif values.get('XDG_DATA_HOME'):
        path = Path(values['XDG_DATA_HOME']).expanduser() / 'retro-villain'
    else:
        path = Path.home() / '.local' / 'share' / 'retro-villain'
    return secure_directory(path.resolve())
