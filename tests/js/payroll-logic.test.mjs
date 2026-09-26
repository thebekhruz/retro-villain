import test from 'node:test';
import assert from 'node:assert/strict';
// UMD-модуль: из node приходит CJS-экспортом, в браузере ложится в globalThis.
import logic from '../../retro/static/payroll-logic.js';

/* Пустая ячейка и «не пришёл» — разные вещи: в первом случае смены не было
   вовсе, во втором она была, но начисление ноль. Красить их одинаково нельзя,
   иначе ведомость покажет прогул там, где человек просто не работал. */
test('пустой день и неявка различаются', () => {
  assert.deepEqual(logic.cellState(null), {kind: 'empty'});
  assert.equal(logic.cellState({status: 'missing', amount: '0', paid: '0', debt: '0'}).kind, 'missing');
});

test('состояние ячейки различает выдано, частично и долг', () => {
  const at = extra => logic.cellState(Object.assign(
    {status: 'on_time', amount: '200000', paid: '0', debt: '200000'}, extra)).kind;
  assert.equal(at({paid: '200000', debt: '0'}), 'paid');
  assert.equal(at({paid: '50000', debt: '150000'}), 'partial');
  assert.equal(at({}), 'owed');
  // Опоздание видно в ведомости, но долг от него не меняется.
  assert.equal(at({status: 'late'}), 'late');
  assert.equal(at({status: 'late', paid: '200000', debt: '0'}), 'paid');
});

const monthData = () => ({
  days: ['2026-09-01', '2026-09-02', '2026-09-03'],
  shift: [
    {name: 'Азиз Каримов', group: 'Управление', rate: '180000',
     accrued: '360000', paid: '180000', debt: '180000', cells: {
       '2026-09-01': {accrual_id: 1, status: 'on_time', amount: '180000', paid: '180000', debt: '0'},
       '2026-09-03': {accrual_id: 2, status: 'late', amount: '180000', paid: '0', debt: '180000'},
     }},
    {name: 'Жасур Алиев', group: 'Обслуживание зала', rate: '200000',
     accrued: '200000', paid: '0', debt: '200000', cells: {
       '2026-09-02': {accrual_id: 3, status: 'on_time', amount: '200000', paid: '0', debt: '200000'},
     }},
  ],
  paid_per_day: {'2026-09-01': '180000'},
  monthly: [{name: 'Бухгалтер', salary: '5000000'}],
  monthly_total: '5000000', monthly_paid: '2500000',
});

test('в сетке у каждого человека ячейка на каждый день месяца', () => {
  const rows = logic.sheet(monthData());
  assert.equal(rows.length, 2);
  rows.forEach(row => assert.equal(row.cells.length, 3));
  assert.deepEqual(rows[0].cells.map(c => c.kind), ['paid', 'empty', 'late']);
  assert.deepEqual(rows[1].cells.map(c => c.kind), ['empty', 'owed', 'empty']);
  // У заполненной ячейки есть начисление, к которому привязана выплата.
  assert.equal(rows[1].cells[1].accrualId, 3);
  assert.equal(rows[1].cells[0].accrualId, null);
  // День у ячейки сохраняется: выплату записываем на день смены.
  assert.equal(rows[0].cells[2].day, '2026-09-03');
});

test('итоги месяца складывают сменных и не путают их с окладами', () => {
  const totals = logic.totals(monthData());
  assert.equal(totals.accrued, 560000);
  assert.equal(totals.paid, 180000);
  assert.equal(totals.debt, 380000);
  assert.equal(totals.paid + totals.debt, totals.accrued);
  assert.equal(totals.people, 2);
  assert.equal(totals.owing, 2);
  // Оклады идут отдельно: сменами они не начисляются.
  assert.equal(totals.monthlyPaid, 2500000);
  assert.equal(totals.monthlyFund, 5000000);
  assert.equal(totals.monthlyPeople, 1);
});

test('в подвале — выдано из кассы по дням, включая нулевые', () => {
  assert.deepEqual(logic.dayTotals(monthData()), [
    {day: '2026-09-01', amount: 180000},
    {day: '2026-09-02', amount: 0},
    {day: '2026-09-03', amount: 0},
  ]);
});

test('пустой месяц не ломает расчёты', () => {
  const empty = {days: [], shift: [], paid_per_day: {}, monthly: []};
  assert.deepEqual(logic.sheet(empty), []);
  assert.deepEqual(logic.dayTotals(empty), []);
  const totals = logic.totals(empty);
  assert.equal(totals.accrued, 0);
  assert.equal(totals.people, 0);
});

test('переключение месяца не выпадает из года', () => {
  assert.equal(logic.shiftMonth('2026-09', -1), '2026-08');
  assert.equal(logic.shiftMonth('2026-01', -1), '2025-12');
  assert.equal(logic.shiftMonth('2026-12', 1), '2027-01');
});
