import test from 'node:test';
import assert from 'node:assert/strict';
import period from '../../retro/static/period.js';

test('October presets never start in September before October 13', () => {
  for(const preset of ['7','10','30','month']){
    assert.deepEqual(period.presetRange('2026-10-14',preset),{start:'2026-10-13',end:'2026-10-13'});
  }
  assert.ok(period.check('2026-09-30','2026-10-13','2026-10-14'));
  assert.ok(period.check('2026-10-11','2026-10-13','2026-10-14'));
  assert.equal(period.check('2026-10-13','2026-10-13','2026-10-14'),'');
  assert.ok(period.presetRange('2026-10-14','prev-month').end<period.START);
  assert.deepEqual(period.presetRange('2026-11-07','prev-month'),{start:'2026-10-13',end:'2026-10-31'});
});

test('analyticsRange: 3, 7 и 30 дней меню директора — разные периоды (T-486)', () => {
  assert.deepEqual(period.analyticsRange('2026-10-16','3'),{start:'2026-10-13',end:'2026-10-15'});
  assert.deepEqual(period.analyticsRange('2026-10-16','7'),{start:'2026-10-09',end:'2026-10-15'});
  assert.deepEqual(period.analyticsRange('2026-10-16','30'),{start:'2026-09-16',end:'2026-10-15'});
  // Учётные пресеты по-прежнему с 13.10.
  assert.deepEqual(period.presetRange('2026-10-16','30'),{start:'2026-10-13',end:'2026-10-15'});
});

test('ПК-отчёт директора без границы учёта: даты до 13.10 проходят проверку (T-495)', () => {
  // null — живая аналитика iiko: «10 дней» на 17.10 — с 7 октября, а не с 13-го.
  assert.equal(period.check('2026-10-07', '2026-10-16', '2026-10-17', null), '');
  assert.equal(period.check('2026-09-17', '2026-10-16', '2026-10-17', null), '');
  // Остальные правила те же: сегодняшний день не закрыт, 62 дня — предел iiko.
  assert.match(period.check('2026-10-07', '2026-10-17', '2026-10-17', null), /не закрыт/);
  assert.match(period.check('2026-08-01', '2026-10-16', '2026-10-17', null), /62/);
  // Без четвёртого аргумента — прежняя граница учёта (касса, бухгалтерия, сохранённые отчёты).
  assert.match(period.check('2026-10-07', '2026-10-16', '2026-10-17'), /13 октября/);
});
