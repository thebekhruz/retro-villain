#!/usr/bin/env python3
"""Report unsafe runtime path permissions without reading file contents."""

import argparse
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PermissionIssue:
    path: Path
    actual_mode: int
    expected_mode: int


def check_runtime_permissions(paths):
    issues = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        expected = 0o700 if path.is_dir() else 0o600
        actual = stat.S_IMODE(path.stat().st_mode)
        if actual & ~expected:
            issues.append(PermissionIssue(path, actual, expected))
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='+', type=Path)
    issues = check_runtime_permissions(parser.parse_args().paths)
    for issue in issues:
        print(f'{issue.path}: mode={issue.actual_mode:04o}, expected={issue.expected_mode:04o}')
    return 1 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())
