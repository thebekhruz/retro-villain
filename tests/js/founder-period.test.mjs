import test from 'node:test';
import assert from 'node:assert/strict';
import logic from '../../retro/static/founder-logic.js';

test('период аналитики проверяется до запроса теми же правилами, что на сервере', () => {
  assert.equal(logic.periodError('2026-10-13', '2026-10-28'), null);
  assert.equal(logic.periodError('2026-10-27', '2026-10-17'), 'Дата начала должна быть не позже даты конца.');
  assert.equal(logic.periodError('', '2026-10-10'), 'Укажите обе даты периода.');
  assert.equal(logic.periodError('2026-10-13', '2027-10-14'), 'Период не может быть длиннее 366 дней.');
  assert.equal(logic.periodError('2026-10-13', '2027-10-13'), null);  // 365 дней — можно
});

test('сентябрь закрыт, быстрый период обрезается до начала учёта', () => {
  assert.ok(logic.periodError('2026-09-30','2026-10-13'));
  assert.deepEqual(logic.quickPeriod('30','2026-10-14'),{start:'2026-10-13',end:'2026-10-13'});
});
