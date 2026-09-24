"""Run corrected audit invariants without live services or application databases.

Original defect reproductions are preserved in commit 759131b. Run this entry
point on the current checkout for the desired behavior after T-368 fixes.
"""
from pathlib import Path
import sys

if __name__ == '__main__':
    import pytest
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    raise SystemExit(pytest.main([str(root / 'tests/test_data_integrity.py'), '-q']))
