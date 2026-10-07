"""Application factory for pre-cutover regression fixtures.

Existing financial workflow tests construct September data and their own opening
balances. Keep those fixtures isolated from the installed restaurant's October
policy. Public date enforcement and the real bootstrap are tested without these
overrides in test_october_access.py. No production setting disables the policy.
"""
from unittest.mock import patch

from retro.app import create_app as production_app
from retro.accounting_access import require_accounting_dates


def create_app(*args, **kwargs):
    with patch('retro.modules.accountant.opening_migration.apply_october_opening'):
        app = production_app(*args, **kwargs)
    app.dependency_overrides[require_accounting_dates] = lambda: None
    return app
