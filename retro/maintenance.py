"""Safe backup, verification, and installation of Retro SQLite data."""

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from retro.runtime import secure_directory, secure_file
from retro.schema import SchemaError


MaintenanceError = SchemaError


@dataclass(frozen=True)
class DatabaseCheck:
    filename: str
    size: int
    sha256: str
    integrity: str
    schema_version: int


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_database(path: Path) -> DatabaseCheck:
    path = Path(path)
    if not path.is_file():
        raise MaintenanceError(f'База не найдена: {path}.')
    try:
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
            integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
            version_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retro_schema_version'"
            ).fetchone()
            version = 0
            if version_table:
                row = connection.execute('SELECT MAX(version) FROM retro_schema_version').fetchone()
                version = int(row[0] or 0)
    except sqlite3.Error as error:
        raise MaintenanceError(f'Не удалось проверить базу {path.name}: {error.__class__.__name__}.') from None
    return DatabaseCheck(path.name, path.stat().st_size, file_sha256(path), integrity, version)


def _backup_one(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + '.tmp')
    if temporary.exists():
        temporary.unlink()
    try:
        with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as source_db:
            with sqlite3.connect(temporary) as target_db:
                source_db.backup(target_db)
        with temporary.open('rb') as copied:
            os.fsync(copied.fileno())
        if verify_database(temporary).integrity != 'ok':
            raise MaintenanceError(f'Проверка {source.name} не пройдена.')
        os.replace(temporary, destination)
        secure_file(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def backup_databases(sources: dict[str, Path], backup_root: Path, now: datetime) -> Path:
    backup_root = secure_directory(Path(backup_root))
    backup_dir = backup_root / now.strftime('%Y%m%dT%H%M%SZ')
    try:
        backup_dir.mkdir(mode=0o700)
    except FileExistsError:
        raise MaintenanceError(f'Резервная копия {backup_dir.name} уже существует.') from None
    checks = []
    try:
        for filename, source in sorted(sources.items()):
            source = Path(source)
            if not source.is_file():
                raise MaintenanceError(f'Исходная база не найдена: {source}.')
            destination = backup_dir / filename
            _backup_one(source, destination)
            checks.append(asdict(verify_database(destination)))
        manifest = backup_dir / 'manifest.json'
        manifest.write_text(
            json.dumps({'created_at': now.isoformat(), 'databases': checks}, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        secure_file(manifest)
        return backup_dir
    except Exception:
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise


def _verified_manifest(backup_dir: Path) -> list[dict]:
    manifest_path = backup_dir / 'manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        entries = manifest['databases']
    except (OSError, ValueError, KeyError, TypeError):
        raise MaintenanceError('Manifest резервной копии повреждён.') from None
    if not isinstance(entries, list) or not entries:
        raise MaintenanceError('Manifest не содержит баз данных.')
    for entry in entries:
        source = backup_dir / entry.get('filename', '')
        check = verify_database(source)
        if check.integrity != 'ok' or check.sha256 != entry.get('sha256'):
            raise MaintenanceError(f'Резервная копия {source.name} не прошла проверку.')
    return entries


def install_verified_backup(backup_dir: Path, destination: Path, replace: bool = False) -> list[Path]:
    backup_dir = Path(backup_dir)
    entries = _verified_manifest(backup_dir)
    destination = secure_directory(Path(destination))
    targets = [destination / entry['filename'] for entry in entries]
    existing = [path for path in targets if path.exists()]
    if existing and not replace:
        raise MaintenanceError(f'Файл уже существует: {existing[0].name}.')
    installed = []
    for entry, target in zip(entries, targets, strict=True):
        temporary = target.with_suffix(target.suffix + '.installing')
        try:
            shutil.copyfile(backup_dir / entry['filename'], temporary)
            secure_file(temporary)
            check = verify_database(temporary)
            if check.integrity != 'ok' or check.sha256 != entry['sha256']:
                raise MaintenanceError(f'Копия {target.name} повреждена при установке.')
            os.replace(temporary, target)
            secure_file(target)
            installed.append(target)
        finally:
            if temporary.exists():
                temporary.unlink()
    return installed
