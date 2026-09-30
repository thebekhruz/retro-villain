import test from 'node:test';
import assert from 'node:assert/strict';
import logic from '../../retro/static/founder-logic.js';

test('период аналитики проверяется до запроса теми же правилами, что на сервере', () => {
  assert.equal(logic.periodError('2026-09-01', '2026-09-28'), null);
  assert.equal(logic.periodError('2026-09-20', '2026-09-10'), 'Дата начала должна быть не позже даты конца.');
  assert.equal(logic.periodError('', '2026-09-10'), 'Укажите обе даты периода.');
  assert.equal(logic.periodError('2025-08-31', '2026-09-01'), 'Период не может быть длиннее 366 дней.');
  assert.equal(logic.periodError('2025-09-01', '2026-09-01'), null);  // 365 дней — можно
});
