import test from 'node:test';
import assert from 'node:assert/strict';
import logic from '../../retro/static/founder-logic.js';

test('период аналитики проверяется до запроса теми же правилами, что на сервере', () => {
  assert.equal(logic.periodError('2026-10-02', '2026-10-28'), null);
  assert.equal(logic.periodError('2026-10-20', '2026-10-10'), 'Дата начала должна быть не позже даты конца.');
  assert.equal(logic.periodError('', '2026-10-10'), 'Укажите обе даты периода.');
  assert.equal(logic.periodError('2026-10-02', '2027-10-03'), 'Период не может быть длиннее 366 дней.');
  assert.equal(logic.periodError('2026-10-02', '2027-10-02'), null);  // 365 дней — можно
});

test('сентябрь закрыт, быстрый период обрезается до начала учёта', () => {
  assert.ok(logic.periodError('2026-09-30','2026-10-06'));
  assert.deepEqual(logic.quickPeriod('30','2026-10-07'),{start:'2026-10-02',end:'2026-10-06'});
});
