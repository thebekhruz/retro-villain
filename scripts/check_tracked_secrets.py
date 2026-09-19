#!/usr/bin/env python3
"""Reject tracked runtime data and high-confidence credential material."""

import re
import subprocess
from pathlib import Path


ALLOWED_ENV_FILES = {'config.env.example'}
RUNTIME_SUFFIXES = {'.sqlite3', '.db', '.pdf'}
SKIP_CONTENT_PREFIXES = ('docs/', 'tests/')
SKIP_CONTENT_FILES = {'README.md', 'config.env.example'}
SECRET_PATTERNS = (
    ('private key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
    ('Google API key', re.compile(r'\bAIza[0-9A-Za-z_-]{30,}\b')),
    ('OpenAI-style API key', re.compile(r'\bsk-[0-9A-Za-z_-]{20,}\b')),
    ('Bearer token', re.compile(r'\bBearer\s+[0-9A-Za-z._~-]{20,}\b', re.IGNORECASE)),
)


def tracked_paths():
    result = subprocess.run(
        ['git', 'ls-files', '-z'], check=True, stdout=subprocess.PIPE)
    return [Path(value.decode()) for value in result.stdout.split(b'\0') if value]


def filename_issue(path: Path):
    name = path.name
    if (name == '.env' or name.startswith('.env.')) and name not in ALLOWED_ENV_FILES:
        return 'environment file'
    if path.suffix.casefold() in RUNTIME_SUFFIXES:
        return f'runtime artifact {path.suffix}'
    if any(part.casefold() in {'backup', 'backups', 'generated-reports'} for part in path.parts):
        return 'runtime output directory'
    return None


def content_issue(path: Path):
    value = path.as_posix()
    if value in SKIP_CONTENT_FILES or value.startswith(SKIP_CONTENT_PREFIXES):
        return None
    try:
        if path.stat().st_size > 2_000_000:
            return None
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


def main():
    issues = []
    for path in tracked_paths():
        reason = filename_issue(path) or content_issue(path)
        if reason:
            issues.append((path, reason))
    for path, reason in issues:
        print(f'{path}: tracked {reason}')
    return 1 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())
