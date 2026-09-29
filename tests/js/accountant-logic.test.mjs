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

/* ── Макет 2a: день выплат P показывает вчерашнюю смену S = P − 1 ─────── */
const staffRow = (id, name, extra = {}) => ({employee_id: id, name, role: 'официант', status: 'on_time',
  first_entry: '2026-09-28T09:21:00+05:00', rate: '180000', payable: '180000', ...extra});
const accrualRow = (id, employee_id, name, work_day, extra = {}) => ({id, employee_id, name, work_day, group: 'Зал',
  status: 'on_time', rate: '180000', amount: '180000', paid: '0', debt: '180000', ...extra});

test('до подтверждения смена собирается из реестра вчерашнего дня', () => {
  const staff = {employees: [staffRow(1, 'Жасур'), staffRow(2, 'Камола', {status: 'missing', first_entry: null, payable: '0'}),
    staffRow(3, 'Нилуфар', {status: 'manual_absent', first_entry: null, payable: '0'})]};
  const board = logic.shiftBoard({payday: '2026-09-29', staff, accruals: [], movements: []});
  assert.equal(board.S, '2026-09-28');
  assert.equal(board.confirmed, false);
  assert.deepEqual(board.own.map(r => r.kind), ['todo', 'none', 'none']);
  assert.equal(board.own[2].noHik, true);
  assert.equal(board.toPay.length, 1);
  assert.equal(board.totals.unconfirmedDebt, 180000);
});

test('после подтверждения видна выдача за сегодня, частичная — предупреждение', () => {
  const staff = {employees: [staffRow(1, 'Жасур'), staffRow(2, 'Шахзод', {status: 'late', first_entry: '2026-09-28T10:14:00+05:00'})]};
  const accruals = [accrualRow(10, 1, 'Жасур', '2026-09-28', {paid: '180000', debt: '0'}),
    accrualRow(11, 2, 'Шахзод', '2026-09-28', {status: 'late', paid: '160000', debt: '20000'}),
    accrualRow(5, 1, 'Жасур', '2026-09-09', {paid: '0', debt: '180000'})];
  const movements = [
    {id: 1, type: 'salary_payment', description: 'ЗП персонал · Жасур · за 2026-09-28', amount: '180000'},
    {id: 2, type: 'salary_payment', description: 'ЗП персонал · Шахзод · за 2026-09-28', amount: '160000'}];
  const board = logic.shiftBoard({payday: '2026-09-29', staff, accruals, movements});
  assert.equal(board.confirmed, true);
  const [stale, jasur, shahzod] = board.rows;
  assert.equal(stale.own, false);
  assert.equal(stale.kind, 'todo');
  assert.equal(jasur.kind, 'ok');
  assert.equal(jasur.paidToday, 180000);
  assert.equal(shahzod.kind, 'warn');
  assert.match(shahzod.note, /^−20\s000 к ставке$/);
  assert.equal(shahzod.late, 14);
  assert.equal(shahzod.time, '10:14');
  assert.deepEqual(logic.boardTabs(board.rows).map(([k, , n]) => k + n), ['all3', 'late1', 'todo1', 'err1', 'nohik0']);
});

test('подтверждение частичное: заблокированы только строки без расчёта', () => {
  const staff = {employees: [staffRow(1, 'Жасур'), staffRow(2, 'Без ставки', {rate: null, payable: null}),
    staffRow(3, 'Без привязки', {status: 'unlinked', first_entry: null, payable: null}),
    staffRow(4, 'Нет входов', {status: 'unavailable', first_entry: null, payable: null})]};
  const board = logic.shiftBoard({payday: '2026-09-29', staff, accruals: [], movements: []});
  assert.deepEqual(board.own.map(r => r.block), [null, 'rate', 'unlinked', 'hikvision']);
  assert.deepEqual(board.own.map(r => r.kind), ['todo', 'blocked', 'blocked', 'blocked']);
  assert.equal(board.toPay.length, 1);
  assert.deepEqual(logic.shiftBlocker(staff, board), {total: 3, rate: 1, unlinked: 1, hikvision: 1, unknown: 0});
  assert.equal(logic.boardTabs(board.rows).find(([k]) => k === 'err')[2], 3);
  // Сервер начислил Жасура — остальные всё ещё ждут, смена подтверждена частично.
  const partial = logic.shiftBoard({payday: '2026-09-29', staff, movements: [],
    accruals: [accrualRow(10, 1, 'Жасур', '2026-09-28')]});
  assert.equal(partial.confirmed, false);
  assert.equal(partial.partial, true);
  assert.equal(partial.own[0].accrualId, 10);
  assert.equal(partial.own[1].accrualId, null);
  assert.equal(logic.shiftBlocker(null, logic.shiftBoard({payday: '2026-09-29', staff: {employees: [staffRow(1, 'А')]}, accruals: [], movements: []})), null);
});

test('журнал: долг дня одной строкой, зарплата и выдачи Шоху — «Авто»', () => {
  const index = logic.catalogIndex([{code: 'utilities', label: 'Коммунальные и охрана', items: [{code: 'utilities', label: 'Газ, свет'}]},
    {code: 'administrative', label: 'Общие', items: [{code: 'admin_other', label: 'Прочие расходы'}]}]);
  const data = {date: '2026-09-29', reserves: {}, ledger: {
    cash_flow: {salary_paid: '360000', other_outflows: '4240000'},
    movements: [
      {id: 1, type: 'salary_payment', description: 'ЗП персонал · Жасур · за 2026-09-28', amount: '180000'},
      {id: 2, type: 'salary_payment', description: 'ЗП персонал · Шахзод · за 2026-09-28', amount: '180000'},
      {id: 57, type: 'procurement_advance', description: 'Шох: закуп за день', amount: '3000000', item_code: null},
      {id: 58, type: 'other_expense', description: 'Газ, свет · Электроэнергия', amount: '1000000', item_code: 'utilities'},
      {id: 59, type: 'other_expense', description: 'Прочие расходы · Канцтовары', amount: '240000', item_code: 'admin_other'}],
    debts_created_today: [{id: 1, day: '2026-09-29', item_code: 'utilities', description: 'Газ, свет · Электроэнергия', total: '1850000', paid: '1000000', debt: '850000'}],
    manual_debts: [{id: 1, day: '2026-09-29', item_code: 'utilities', description: 'Газ, свет · Электроэнергия', total: '1850000', paid: '1000000', debt: '850000'},
      {id: 7, day: '2026-09-20', item_code: 'admin_other', description: 'Прочие расходы · Ремонт', total: '500000', paid: '0', debt: '500000'}]}};
  const {rows, total} = logic.journal(data, index);
  assert.equal(total, 4600000);
  const byName = Object.fromEntries(rows.map(r => [r.name, r]));
  assert.equal(byName['Сменные за 28.09 · 2 чел.'].amount, 360000);
  assert.equal(byName['Выдано Шоху на закуп · подотчёт'].kind, 'auto');
  assert.deepEqual([byName['Электроэнергия'].amount, byName['Электроэнергия'].paid, byName['Электроэнергия'].debt], [1850000, 1000000, 850000]);
  assert.equal(byName['Электроэнергия'].cat, 'Коммунальные / охрана');
  // × удаляет ошибочный долг целиком — вместе с оплатой этого дня.
  assert.deepEqual(byName['Электроэнергия'].ops, [{operation: 'debt', id: 1}]);
  assert.equal(byName['Канцтовары'].cat, 'Административные');
  assert.equal(byName['Ремонт'].kind, 'carried');
  assert.equal(rows.filter(r => r.name === 'Электроэнергия').length, 1);
});

test('деньги на расходы: оклады и выдачи Шоху отделены от прочих', () => {
  const card = logic.cashCard({expected_cashier: '7450000', ledger: {cash_balance: '33340000',
    cash_flow: {opening_balance: '30130000', other_receipts: '0', salary_paid: '0', other_outflows: '4240000'},
    movements: [{type: 'procurement_advance', amount: '3000000'}, {type: 'other_expense', item_code: 'salary_monthly', amount: '500000'},
      {type: 'other_expense', item_code: 'utilities', amount: '740000'}]}});
  assert.deepEqual([card.shoh, card.monthly, card.other], [3000000, 500000, 740000]);
  assert.equal(card.opening + card.cashier - card.shift - card.monthly - card.shoh - card.other, card.end);
});

test('оклады: остаток месяца и переплата по выплатам за месяц', () => {
  const board = logic.monthlyBoard({monthly_employees: [{id: 1, name: 'Азиз', role: 'менеджер', salary: '8000000'},
    {id: 3, name: 'Фаррух', role: 'шеф', salary: '9000000'}],
    monthly_payments: {paid_by_employee: {'1': '6000000', '3': '9500000'}, today: [{id: 90, employee_id: 1, name: 'Азиз', amount: '1000000', day: '2026-09-29'}]}});
  assert.equal(board.remain, 2000000);
  assert.deepEqual(board.overpaid.map(p => p.name), ['Фаррух']);
  assert.equal(board.today[0].left, 2000000);
  assert.equal(board.todaySum, 1000000);
});

test('покупки Шоха: без фото и дороже обычного — замечания, принятые — без них', () => {
  const buys = [{id: 1, item: 'Лук', unit: 'кг', price: '5000', total: '100000', has_photo: true, price_above_usual: false, accepted_at: null, created_at: '2026-09-29T09:15:00+05:00', usual_price: null},
    {id: 2, item: 'Говядина', unit: 'кг', price: '104000', total: '1560000', has_photo: false, price_above_usual: true, price_delta_percent: '13', usual_price: '92000', accepted_at: null, created_at: '2026-09-29T09:31:00+05:00'},
    {id: 3, item: 'Хлеб', unit: 'шт', price: '5000', total: '50000', has_photo: false, price_above_usual: false, accepted_at: '2026-09-29T11:00:00+05:00', created_at: '2026-09-29T09:40:00+05:00', usual_price: null}];
  const shoh = logic.shohBoard({balance: '1640000', entries: [{kind: 'deposit', amount: '3000000'}]}, buys, [{id: 57, type: 'procurement_advance', amount: '3000000'}]);
  assert.deepEqual(shoh.buys.map(b => b.flags.map(f => f.t).join(' · ')), ['', 'Цена +13% · Нет фото', '']);
  assert.equal(shoh.spent, 1710000);
  assert.equal(shoh.given, 3000000);
  assert.equal(shoh.gives.length, 1);
});

test('проверки: ошибки впереди, «ждут выплату» — последними', () => {
  const board = logic.shiftBoard({payday: '2026-09-29', staff: {employees: [staffRow(1, 'Жасур')]}, accruals: [], movements: []});
  const monthly = {overpaid: [{id: 3, name: 'Фаррух', paid: 9500000, salary: 9000000}]};
  const issues = logic.financeIssues({data: {date: '2026-09-29', expected_cashier: null, dividends_week: null}, board, blocker: null, monthly,
    shoh: {buys: [], hand: -5}, cash: {end: -1}});
  assert.deepEqual(issues.map(i => i.lvl), ['err', 'err', 'err', 'todo', 'todo']);
  assert.equal(issues[0].text, 'Переплата оклада: Фаррух');
  assert.equal(issues[3].text, '1 сменный ждёт выплату за 28.09');
});

test('перечисления поставщику и время выдачи Шоху', () => {
  const shoh = logic.shohBoard({balance: '0', entries: []}, [],
    [{id: 57, type: 'procurement_advance', amount: '3000000', created_at: '2026-09-29T08:40:00+05:00'}],
    [{id: 1, supplier: 'Milk Pro', item: 'Молочная продукция', point: 'RETRO', amount: '1850000'}]);
  assert.equal(shoh.gives[0].time, '08:40');
  assert.equal(shoh.trSum, 1850000);
  assert.deepEqual(shoh.trs[0], {id: 1, supplier: 'Milk Pro', item: 'Молочная продукция', point: 'RETRO', amount: 1850000});
});

test('«Выдать пришедшим» — только своя смена и только тем, кому ещё ничего не выдано', () => {
  const board = logic.shiftBoard({payday: '2026-09-29',
    staff: {employees: [staffRow(1, 'Жасур'), staffRow(2, 'Шахзод'), staffRow(3, 'Нодира')]},
    accruals: [{id: 10, employee_id: 2, name: 'Шахзод', work_day: '2026-09-28', status: 'on_time', rate: '180000', amount: '180000', paid: '100000', debt: '80000'},
      {id: 11, employee_id: 9, name: 'Севара', work_day: '2026-09-09', status: 'on_time', rate: '200000', amount: '200000', paid: '0', debt: '200000'}],
    movements: []});
  assert.deepEqual(board.handOut.map(r => r.name), ['Жасур', 'Нодира']);
  assert.equal(board.toPay.length, 4);  // долги видны, но кнопка их не трогает
});

test('Шох: «на руках» и числа дня берутся с сервера, «Закуп · Шох» — среди выдач', () => {
  const shoh = logic.shohBoard({balance: '3640000', entries: [{kind: 'deposit', amount: '3000000'}, {kind: 'deposit', amount: '24000'}]},
    [{id: 1, item: 'Лук', unit: 'кг', price: '5000', total: '100000', has_photo: true, accepted_at: null, usual_price: null}],
    [{id: 57, type: 'procurement_advance', amount: '3000000'}, {id: 60, type: 'other_expense', item_code: 'proc_shoh', amount: '24000', description: 'Шох · мелочь'}],
    [], null, {pocket: '1860000.00', day_start: '640000.00', given_today: '3024000', spent_day: '1804000', reported_percent: 49});
  assert.deepEqual([shoh.start, shoh.given, shoh.spent, shoh.hand, shoh.reported], [640000, 3024000, 1804000, 1860000, 49]);
  assert.equal(shoh.start + shoh.given - shoh.spent, shoh.hand);
  assert.equal(shoh.gives.reduce((s, g) => s + g.amount, 0), 3024000);
  const card = logic.cashCard({expected_cashier: '1000', ledger: {cash_balance: '0', cash_flow: {other_outflows: '3024000'},
    movements: [{type: 'procurement_advance', amount: '3000000'}, {type: 'other_expense', item_code: 'proc_shoh', amount: '24000'}]}});
  assert.deepEqual([card.shoh, card.other], [3024000, 0]);
});

test('проверки: получено от кассира меньше расчёта — ошибка', () => {
  const board = logic.shiftBoard({payday: '2026-09-29', staff: {employees: []}, accruals: [], movements: []});
  const data = {date: '2026-09-29', expected_cashier: '550000', dividends_week: null,
    cashier_handover: {amount: '550000', confirmed_at: '2026-09-29T21:10:00+05:00', expected_amount: '850000', shortfall: '300000'},
    ledger: {cash_balance: '550000', cash_flow: {opening_balance: '0', other_outflows: '0', salary_paid: '0'}, movements: []}};
  const cash = logic.cashCard(data);
  assert.deepEqual([cash.confirmedAt, cash.calculation, cash.shortfall], ['21:10', 850000, 300000]);
  const issues = logic.financeIssues({data, board, blocker: null, monthly: {overpaid: []}, shoh: {buys: [], hand: 0}, cash});
  assert.equal(issues[0].lvl, 'err');
  assert.equal(issues[0].text, 'От кассира получено меньше расчёта');
  assert.equal(issues[0].target, 'cash');
});

test('проверки дня: отставание по дивидендам меряется от плана к сегодняшнему дню', () => {
  const items = logic.dayChecks({date: '2026-09-29', expected_cashier: '1', missing_rates: 0, employees: [], payroll: {unknown_count: 0},
    ledger: {cash_balance: '0', accruals: [], payroll_confirmed: true, manual_debt_total: '0'},
    dividends_week: {behind: true, pace: '2857142.86', due: '1428571.43', collected: '1500000'}});
  const behind = items.find(item => item.text === 'Отстаём от недельных дивидендов');
  assert.equal(behind.sub.amount, 1357143);
});
