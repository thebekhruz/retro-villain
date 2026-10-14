"""Прежняя граница учёта (06.10, нулевые остатки) для сценариев недели 2–10 октября.

Эти тесты разбирают снимки C-01…C-04 и сверку зарплат по дням до 13.10 и
проверяют формулы, а не саму границу. Граница 13.10 и её остатки проверяются
без подмены в test_october_access.py и test_accounting_start.py.
"""
import sys
from datetime import date, timedelta

import pytest

from retro import accounting_period
from retro.modules.accountant import opening_migration

LEGACY_START = date(2026, 10, 6)
# Имена, под которыми модули держат границу; другие даты (например DAY теста) не трогаем.
NAMES = {'ACCOUNTING_START', 'SALARY_SHIFT_START', 'SALARY_ENTRY_START', 'ENTRY_START', 'FIRST_SHIFT', 'CUTOVER'}


@pytest.fixture
def legacy_boundary(monkeypatch):
    replace = {accounting_period.ACCOUNTING_START: LEGACY_START,
               accounting_period.SALARY_ENTRY_START: LEGACY_START,
               accounting_period.SALARY_SHIFT_START: LEGACY_START - timedelta(days=1)}
    # Модули (и скрипты из scripts/) импортируют границу по имени — подменяем каждую копию.
    for module in list(sys.modules.values()):
        if module is None:
            continue
        for attr, value in list(vars(module).items()):
            if attr in NAMES and type(value) is date and value in replace:
                monkeypatch.setattr(module, attr, replace[value])
    monkeypatch.setattr(opening_migration, 'CASH_OPENING', '0')
    monkeypatch.setattr(opening_migration, 'RESERVE_OPENINGS', dict.fromkeys(opening_migration.RESERVE_OPENINGS, '0'))
