import test from 'node:test';
import assert from 'node:assert/strict';
import logic from '../../retro/static/founder-cabinet-logic.js';
import accountant from '../../retro/static/accountant-logic.js';

// Intl разделяет разряды неразрывным пробелом — в ожиданиях пишем обычный.
const plain = text => String(text).replace(/\u00a0/g, ' ');

const day = (date, weekday, extra) => ({date, weekday, today: false, cashier: {retro: '30000000', school: '4000000', demo: '9000000'},
  orders: {retro: 100, school: 50}, handover: {recorded: '9000000.00', expected: '9000000.00', status: 'ok', difference: '0.00'},
  flows: {salary: '2000000', procurement: '3000000', other: '200000', dividends: '1500000', receipt: '0'},
  closing_balance: '12000000', ...extra});

const week = {start: '2026-09-21', end: '2026-09-27', days: [
  day('2026-09-21', 0),
  day('2026-09-22', 1, {cashier: null, cashier_error: 'iiko недоступен', handover: {recorded: '9000000.00', expected: null, status: 'unknown', difference: null}}),
  day('2026-09-23', 2, {handover: {recorded: '8700000.00', expected: '9000000.00', status: 'mismatch', difference: '-300000.00'}}),
  day('2026-09-24', 3, {today: true, handover: {recorded: null, expected: '1000000.00', status: 'pending', difference: null}}),
  {date: '2026-09-25', weekday: 4, future: true}, {date: '2026-09-26', weekday: 5, future: true},
  {date: '2026-09-27', weekday: 6, future: true},
]};
const forecast = {weekdays: [0, 1, 2, 3, 4, 5, 6].map(weekday => ({weekday, retro: '40000000', school: weekday === 6 ? null : '3000000', orders: 150}))};

test('неделя: прошлое — факт, будущее — прогноз со знаком ≈, деньги будущего пустые', () => {
  const rows = logic.weekRows(week, forecast);
  const retro = rows[0];
  assert.equal(retro.cells[0].text, '30 млн');
  assert.equal(retro.cells[1].text, '—');  // iiko не ответил — не ноль
  assert.equal(retro.cells[4].text, '≈ 40 млн');
  assert.equal(retro.total, '90 млн');  // три дня с данными
  assert.equal(rows[1].cells[6].text, '');  // прогноза Oxbridge на воскресенье нет
  assert.equal(rows[2].total, '600');  // чеки считаются штуками
  const salary = rows.find(row => row.label === '− Зарплаты');
  assert.equal(salary.cells[5].text, '');
  const balance = rows.at(-1);
  assert.equal(balance.balance, true);
  assert.equal(balance.total, '12 млн');  // остаток — последний, а не сумма
});

test('сверка передачи: совпало, недостача, день идёт, нет iiko', () => {
  assert.equal(logic.handoverMark(week.days[0]).mark, '✓');
  const short = logic.handoverMark(week.days[2]);
  assert.equal(short.mark, '⚠');
  assert.match(plain(short.tip), /8 700 000 при расчёте 9 000 000/);
  assert.equal(logic.handoverMark(week.days[3]).tone, 'wait');
  assert.equal(logic.handoverMark(week.days[1]).mark, '?');
  assert.equal(logic.handoverMark(week.days[5]).mark, '');
});

test('итоги недели не считают дни без iiko нулями', () => {
  const totals = logic.weekTotals(week);
  assert.equal(totals.days, 3);
  assert.equal(totals.retro, 90000000);
  assert.equal(totals.demoShare, 30);
});

test('замечания к бухгалтеру: недостача кассира, проверки дня и покупки Шоха', () => {
  const withAccounting = {days: [{...week.days[2], accounting: {date: '2026-09-23', expected_cashier: '1',
    ledger: {cash_balance: '-5', accruals: [], payroll_confirmed: true, manual_debt_total: '0'},
    missing_rates: 0, payroll: {unknown_count: 0}, employees: [],
    dividends_week: {behind: true, pace: '5', collected: '1'}}}]};
  const spending = {shokh: {flagged: [{day: '2026-09-23', item: 'Лук', total: '100000', reason: 'нет фото'}]}};
  const issues = logic.accountantIssues(withAccounting, spending, accountant.dayChecks);
  assert.deepEqual(issues.map(item => item.text), [
    'Кассир передал меньше расчёта · 23.09', 'Остаток ушёл в минус · 23.09', 'Покупка Шоха: Лук · 23.09']);
});

test('дивиденды: статус, доли и подпись выдачи', () => {
  const view = logic.dividendView({week: '2026-W39', start: '2026-09-21', end: '2026-09-27', payout_day: '2026-09-28',
    target: '10000000.00', collected: '2000000.00', pace: '5714285.71', due: '4285714.29', behind: true, done: false, free_cash_week: null});
  assert.equal(plain(view.status), 'Отстаём от плана на 2 285 714');
  assert.equal(view.pct, 20);
  assert.equal(view.range, '21.09–27.09');
  assert.equal(view.payout, 'Выдача в понедельник, 28 сентября');
  assert.equal(logic.dividendView({start: '2026-09-21', end: '2026-09-27', payout_day: '2026-09-28', target: null, collected: '0'}).status,
    'Цель на неделю не задана');
  assert.equal(logic.stepTarget(300000, -1), 0);
  assert.equal(logic.stepTarget(10000000, 1), 10500000);
  assert.equal(logic.parseAmount('10 000 000 сум'), 10000000);
});

test('уведомления о Счёте Шефа: только выше порога, за сегодня и вчера, без закрытых', () => {
  const chef = {bills: [{order_id: 'a', day: '2026-09-24', over: true}, {order_id: 'b', day: '2026-09-23', over: true},
    {order_id: 'c', day: '2026-09-20', over: true}, {order_id: 'd', day: '2026-09-24', over: false}]};
  assert.deepEqual(logic.chefAlerts(chef, '2026-09-24', '2026-09-23', ['b']).map(bill => bill.order_id), ['a']);
});

test('к вечеру: утро плюс касса минус то, что уйдёт', () => {
  const evening = logic.eveningCash({opening_balance: '10000000', cashier: {expected_handover: '5000000'},
    outlook: {salary_due: '2000000', procurement: '3000000', other: '500000'}});
  assert.equal(evening, 9500000);
  assert.equal(logic.eveningCash({opening_balance: null, outlook: {}}), null);
});

test('короткие суммы: миллионы и миллиарды', () => {
  assert.equal(logic.short(1438528898), '1,44 млрд');
  assert.equal(logic.short(42500000), '42,5 млн');
  assert.equal(logic.short(null), '—');
});
