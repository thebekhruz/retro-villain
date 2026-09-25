import test from 'node:test';
import assert from 'node:assert/strict';
// Модуль UMD: в браузере кладётся в globalThis, из node приходит как CJS,
// поэтому берём его default-экспортом, а не из globalThis.
import logic from '../../retro/static/accountant-logic.js';

/* «Финансы дня» отдаёт движения только за выбранный день, а баланс — на его
   конец. Остаток на начало приходится восстанавливать, и раньше он считался
   суммированием прошлых записей, которых в ответе нет — выходил ноль. */
test('остаток подотчёта на начало дня восстанавливается из конца минус движения дня', () => {
  const position = logic.shohPosition({balance: '1060000', entries: [
    {kind: 'withdrawal', amount: '340000', day: '2026-09-16'},
    {kind: 'deposit', amount: '900000', day: '2026-09-16'},
  ]});
  assert.equal(position.given, 900000);
  assert.equal(position.accepted, 340000);
  assert.equal(position.balance, 1060000);
  assert.equal(position.start, 500000);
  // Начало + выдано − принято обязано сходиться с остатком на конец.
  assert.equal(position.start + position.given - position.accepted, position.balance);
});

test('без начального остатка подотчёт не выдумывает ноль', () => {
  const position = logic.shohPosition({balance: null, entries: []});
  assert.equal(position.known, false);
  assert.equal(position.start, null);
  assert.equal(position.balance, null);
});

const employee = (name, extra = {}) => ({
  name, role: 'официант', status: 'on_time', first_entry: '2026-09-17T09:10:00+05:00',
  rate: '200000', payable: '200000', hikvision_registered: true, ...extra,
});
const accrual = (name, day, extra = {}) => ({
  id: name.length + day.length, name, work_day: day, group: 'Обслуживание зала',
  status: 'on_time', rate: '200000', amount: '200000', paid: '0', debt: '200000', ...extra,
});

/* Смену за вчера выдают сегодня. Пока строки собирались только из начислений
   подтверждённого дня, долг прошлой смены пропадал с экрана при переходе на
   следующий день — и проверка «невыданные смены» указывала в пустоту. */
test('непогашенный долг прошлой смены виден и на неподтверждённом дне', () => {
  const shift = logic.shiftRows({
    date: '2026-09-17',
    employees: [employee('Жасур Алиев')],
    payroll: {unknown_count: 0},
    ledger: {payroll_confirmed: false, accruals: [accrual('Гулноза Каюмова', '2026-09-16')]},
  });
  assert.equal(shift.confirmed, false);
  assert.equal(shift.stale.length, 1);
  assert.equal(shift.stale[0].name, 'Гулноза Каюмова');
  assert.equal(shift.stale[0].day, '2026-09-16');
  // Вход известен только за выбранный день, поэтому у чужой смены его не рисуем.
  assert.equal(shift.stale[0].showEntry, false);
  assert.equal(shift.stale[0].first_entry, null);
  // Строка долга идёт перед реестром дня: с неё начинают выдачу.
  assert.equal(shift.rows[0].name, 'Гулноза Каюмова');
  assert.equal(shift.rows[1].name, 'Жасур Алиев');
});

test('итог за смену считается по выбранному дню, долг прошлых — отдельно', () => {
  const shift = logic.shiftRows({
    date: '2026-09-17',
    employees: [employee('Жасур Алиев'), employee('Рустам Ким')],
    payroll: {unknown_count: 0},
    ledger: {payroll_confirmed: true, accruals: [
      accrual('Гулноза Каюмова', '2026-09-16', {debt: '320000', amount: '320000'}),
      accrual('Жасур Алиев', '2026-09-17', {paid: '200000', debt: '0'}),
      accrual('Рустам Ким', '2026-09-17'),
    ]},
  });
  // Начислено и выдано — только за 17-е, иначе день выглядел бы дороже.
  assert.equal(shift.totals.accrued, 400000);
  assert.equal(shift.totals.paid, 200000);
  assert.equal(shift.totals.settled, 1);
  assert.equal(shift.totals.count, 2);
  // К выдаче — всё, что должны, включая прошлую смену.
  assert.equal(shift.totals.owed, 520000);
  assert.equal(shift.totals.staleOwed, 320000);
});

test('срезы смены прячутся, когда в них никого нет', () => {
  const rows = [
    {status: 'on_time', rate: '1', debt: '0'},
    {status: 'late', rate: '1', debt: '5'},
  ];
  assert.deepEqual(logic.shiftTabs(rows).map(([key]) => key), ['all', 'owed', 'late']);
  assert.equal(logic.matchesTab(rows[1], 'owed'), true);
  assert.equal(logic.matchesTab(rows[0], 'owed'), false);
  assert.equal(logic.matchesTab(rows[1], 'late'), true);
  assert.equal(logic.matchesTab(rows[0], 'all'), true);
});

const dayFor = (ledger, extra = {}) => ({
  date: '2026-09-16', employees: [employee('Жасур Алиев')], missing_rates: 0,
  expected_cashier: '4200000', payroll: {unknown_count: 0},
  ledger: {payroll_confirmed: true, cash_balance: '100', accruals: [], manual_debt_total: '0', ...ledger},
  ...extra,
});

/* Сервер сам не пропускает переплату (422), у «не пришёл» начисление 0, а
   ставка и сумма пишутся в начисление одним куском. Такие проверки на экране
   были бы мёртвым кодом, поэтому их тут нет — и не должно появиться. */
test('проверки не выдумывают того, что сервер не допускает', () => {
  const items = logic.dayChecks(dayFor({
    accruals: [accrual('Жасур Алиев', '2026-09-16', {
      status: 'missing', amount: '0', rate: '200000', paid: '0', debt: '0'})],
  }));
  assert.deepEqual(items, []);
});

test('оклад, прошедший расходом, не считается ошибкой', () => {
  // salary_unallocated_on_day — нормальный путь для окладов, не замечание.
  const items = logic.dayChecks(dayFor({salary_unallocated_on_day: '750000'}));
  assert.deepEqual(items.map(i => i.text), []);
});

test('проверки ловят минус в кассе, долг прошлых смен и непереданную кассу', () => {
  const items = logic.dayChecks(dayFor(
    {cash_balance: '-5000', accruals: [accrual('Гулноза Каюмова', '2026-09-15', {debt: '320000'})],
     manual_debt_total: '250000'},
    {expected_cashier: null, missing_rates: 2}));
  assert.deepEqual(items.map(i => i.text), [
    'Остаток ушёл в минус', 'Невыданные смены', 'Без ставки',
    'Неоплаченные расходы', 'Касса не передана']);
  assert.equal(items[0].level, 'bad');
  // У долга прошлой смены есть строка, к которой ведёт клик.
  assert.equal(items[1].accrualId, accrual('Гулноза Каюмова', '2026-09-15').id);
  assert.equal(items[1].sub.amount, 320000);
  assert.equal(items[2].sub.count, 2);
});

test('неподтверждённая смена с готовым расчётом просит подтверждения', () => {
  const items = logic.dayChecks(dayFor({payroll_confirmed: false}));
  assert.deepEqual(items.map(i => i.text), ['Смена не подтверждена']);
});

/* Без данных кассира за день выплата всё равно упадёт на сервере, поэтому
   строки и «Выдать всем» должны запираться одним правилом. */
test('выплата запирается без данных кассира и на закрытом долге', () => {
  const owed = {debt: '200000'}, settled = {debt: '0'};
  assert.equal(logic.payLock({cash_balance: null}, owed), 'no-cash');
  assert.equal(logic.payLock({cash_balance: '100'}, settled), 'settled');
  assert.equal(logic.payLock({cash_balance: '100'}, owed), null);
});
