import test from 'node:test';
import assert from 'node:assert/strict';
import logic from '../../retro/static/analytics-logic.js';

const NBSP = ' ';
const grouped = text => text.replaceAll(NBSP, ' ').replaceAll(' ', ' ');

test('T-524: окно по умолчанию заканчивается вчера, подписи дат короткие', () => {
  assert.equal(logic.defaultEnd('2026-10-22'), '2026-10-21');
  assert.equal(logic.defaultEnd('2026-03-01'), '2026-02-28');
  assert.equal(logic.shortDate('2026-10-05'), '05.10');
  assert.equal(logic.rangeLabel('2026-10-15', '2026-10-21'), '15.10 – 21.10');
  assert.equal(logic.rangeLabel('2026-10-15', '2026-10-21', '2026-10-22'), '15.10 – 21.10');
  assert.equal(logic.rangeLabel('2025-10-16', '2025-10-22', '2026-10-22'), '16.10.2025 – 22.10.2025');
  assert.equal(logic.dayLabel({weekday: 'пн', day: '2026-10-19'}), 'пн 19.10');
});

test('суммы: группировка, знак, компактная ось', () => {
  assert.equal(grouped(logic.formatMoney('12345678')), '12 345 678');
  assert.equal(logic.formatMoney(null), '—');
  assert.equal(grouped(logic.signedMoney('1234567')), '+1 234 567');
  assert.equal(grouped(logic.signedMoney(-1500)), '−1 500');
  assert.equal(logic.signedMoney(0), '0');
  assert.equal(logic.compactMoney(5900000), '5,9 млн');
  assert.equal(logic.compactMoney(820000), '820 тыс');
  assert.equal(logic.compactMoney(1300000000), '1,3 млрд');
  assert.equal(logic.compactMoney(0), '0');
});

test('проценты со знаком и одной десятой', () => {
  assert.equal(logic.formatPercent(12.3), '+12,3 %');
  assert.equal(logic.formatPercent(-4), '−4,0 %');
  assert.equal(logic.formatPercent(0), '0 %');
  assert.equal(logic.formatPercent(null), '—');
});

test('фраза сравнения: выросла / упала / без изменений / нет данных / нулевая база', () => {
  const up = logic.trend({diff: '210', percent: 15});
  assert.equal(up.tone, 'up');
  assert.equal(up.sentence, 'Выручка выросла на 15,0 % (+210 сум).');
  const down = logic.trend({diff: '-1000', percent: -14.3});
  assert.equal(down.tone, 'down');
  assert.equal(grouped(down.sentence), 'Выручка упала на 14,3 % (−1 000 сум).');
  assert.equal(logic.trend({diff: '0', percent: 0}).sentence, 'Выручка не изменилась.');
  const unknown = logic.trend({diff: null, percent: null, direction: 'unknown'});
  assert.equal(unknown.tone, 'unknown');
  assert.equal(unknown.sentence, 'Нет данных для сравнения.');
  const noBase = logic.trend({diff: '500', percent: null});
  assert.equal(noBase.tone, 'up');
  assert.match(noBase.sentence, /раньше продаж не было/);
  assert.equal(noBase.percent, '—');
});

test('потолок оси — «красивое» число не ниже максимума', () => {
  assert.equal(logic.niceCeil(0), 1);
  assert.equal(logic.niceCeil(1), 1);
  assert.equal(logic.niceCeil(1610), 2000);
  assert.equal(logic.niceCeil(2300), 2500);
  assert.equal(logic.niceCeil(4_700_000), 5_000_000);
  assert.equal(logic.niceCeil(7_200_000), 10_000_000);
});

test('точки линии: равный шаг по индексам, пропуск рвёт линию, а не тянет через ноль', () => {
  const points = logic.linePoints(['100', null, '50', '0'], 100, 300, 200);
  assert.deepEqual(points.map(p => [p.x, p.y, p.value]), [[0, 0, 100], [100, null, null], [200, 100, 50], [300, 200, 0]]);
  assert.deepEqual(logic.lineSegments(points), ['0.0,0.0', '200.0,100.0 300.0,200.0']);
  assert.deepEqual(logic.lineSegments(logic.linePoints([], 1, 300, 200)), []);
  // Одна точка — по центру, без деления на ноль.
  assert.equal(logic.linePoints(['5'], 10, 300, 200)[0].x, 150);
});

test('ближайший индекс по координате курсора', () => {
  assert.equal(logic.nearestIndex(0, 600, 7), 0);
  assert.equal(logic.nearestIndex(600, 600, 7), 6);
  assert.equal(logic.nearestIndex(260, 600, 7), 3);
  assert.equal(logic.nearestIndex(-50, 600, 7), 0);
  assert.equal(logic.nearestIndex(900, 600, 7), 6);
  assert.equal(logic.nearestIndex(100, 600, 1), 0);
});

test('подпись времени и покрытия окна', () => {
  assert.equal(logic.updatedLabel({updated_at: '2026-10-21T23:58:10+05:00', includes_today: false}), 'Обновлено 21.10, 23:58');
  assert.equal(logic.updatedLabel({updated_at: '2026-10-22T14:05:00+05:00', includes_today: true}), 'Сегодня ещё идёт · данные на 14:05');
  assert.equal(logic.updatedLabel({updated_at: null, includes_today: true}), 'Сегодня ещё идёт · нет данных');
  assert.equal(logic.updatedLabel({updated_at: null, includes_today: false}), 'Нет загруженных данных');
  const full = {days_with_data: 7, complete: true, days: Array(7).fill({partial: false})};
  assert.equal(logic.coverageLabel(full), '7 из 7 дн.');
  const open = {days_with_data: 7, complete: false, days: [...Array(6).fill({partial: false}), {partial: true}]};
  assert.equal(logic.coverageLabel(open), '7 из 7 дн. · есть незакрытые');
  assert.equal(logic.coverageLabel({days_with_data: 0, complete: false, days: Array(7).fill({})}), 'нет данных');
});
