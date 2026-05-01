"""
pytest configuration: ensure both the repo root (so `odoo_erpnet_fp.*`
imports resolve) and the tests directory (so `from mock_device import
MockDevice` resolves) are on sys.path before test collection.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = Path(__file__).resolve().parent
for p in (_REPO_ROOT, _TESTS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
