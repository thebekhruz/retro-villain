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

/* ── 2b: одна сетка для помесячных и сменных ───────────────────────────── */
const TODAY = '2026-09-29';
const cell = extra => Object.assign({accrual_id: 7, status: 'on_time', amount: '180000', paid: '180000', debt: '0'}, extra);

test('ячейка сменного: выдано, опоздал, н/я, сумма ≠ ставке, выдано без входа', () => {
  const at = (extra, day = '2026-09-20') => logic.gridCell(cell(extra), {rate: '180000', day, today: TODAY});
  assert.equal(at({}).kind, 'paid');
  assert.equal(at({}).text, '✓');
  assert.equal(at({status: 'late'}).late, true);
  assert.equal(at({status: 'missing', amount: '0', paid: '0'}).text, 'н/я');
  assert.equal(at({status: 'manual_absent', amount: '0', paid: '0'}).kind, 'missing');
  // Сумма не равна ставке — показываем выданную сумму, а не галочку.
  const odd = at({amount: '200000', paid: '200000'});
  assert.equal(odd.kind, 'odd');
  assert.match(odd.text, /^200\s000$/);
  // Выдали, хотя входа не было, — ошибка.
  assert.equal(at({status: 'missing', amount: '0', paid: '180000'}).kind, 'nopass');
});

test('невыданная смена: вчерашняя — «к выдаче», старая — «✕»; обе можно выдать', () => {
  const owed = day => logic.gridCell(cell({paid: '0', debt: '180000'}), {rate: '180000', day, today: TODAY});
  assert.equal(owed('2026-09-28').kind, 'topay');
  assert.equal(owed('2026-09-28').text, 'к выдаче');
  assert.equal(owed('2026-09-10').kind, 'unpaid');
  assert.equal(owed('2026-09-10').text, '✕');
  assert.ok(owed('2026-09-28').payable && owed('2026-09-10').payable);
  assert.equal(owed('2026-09-10').debt, 180000);
});

test('неподтверждённая вчерашняя смена берётся из прохода и не выдаётся из сетки', () => {
  const pending = logic.gridCell(null, {rate: '180000', day: '2026-09-28', today: TODAY,
    pending: {status: 'late', payable: '180000'}});
  assert.equal(pending.kind, 'pending');
  assert.equal(pending.late, true);
  assert.equal(pending.payable, false);
  assert.equal(logic.gridCell(null, {day: '2026-09-28', today: TODAY,
    pending: {status: 'manual_absent', payable: '0'}}).text, 'н/я');
  assert.equal(logic.gridCell(null, {day: '2026-09-29', today: TODAY}).kind, 'future');
  assert.equal(logic.gridCell(null, {day: '2026-09-02', today: TODAY}).kind, 'empty');
});

const month2b = () => ({
  days: ['2026-09-27', '2026-09-28', '2026-09-29'],
  shift: [{employee_id: 5, name: 'Санжар Холматов', group: 'Обслуживание зала', rate: '150000',
    accrued: '300000', paid: '150000', debt: '150000', cells: {
      '2026-09-27': cell({accrual_id: 1, status: 'manual_present', amount: '150000', paid: '0', debt: '150000'}),
    }}],
  paid_per_day: {'2026-09-28': '150000'},
  monthly: [{id: 3, name: 'Фаррух Султанов', role: 'шеф-повар', salary: '9000000'},
    {id: 8, name: 'Лола Нурматова', role: 'техперсонал', salary: '3500000'}],
  monthly_cells: {'3': {'2026-09-27': '9500000'}},
  monthly_total: '12500000', monthly_paid: '9500000',
});

test('строка сменного: остаток включает вчерашнюю смену к выдаче, ручная отметка даёт «⊘ Hik»', () => {
  const [row] = logic.shiftRows(month2b(), {today: TODAY, pending: {'2026-09-28': {5: {status: 'on_time', payable: '150000', accrued: false, blocker: null}}}});
  // 27-е старше вчерашнего дня — это уже «✕ не выдана», вчерашнее 28-е ждёт подтверждения.
  assert.deepEqual(row.cells.map(c => c.kind), ['unpaid', 'pending', 'future']);
  assert.equal(row.rest, 300000);
  assert.equal(row.noHik, true);
});

test('строка помесячного: части оклада по дням, переплата и «нет выплат»', () => {
  const [chef, lola] = logic.monthlyRows(month2b(), {today: TODAY});
  assert.equal(chef.cells[0].amount, 9500000);
  assert.equal(chef.cells[0].over, true);
  assert.equal(chef.rest, -500000);
  assert.equal(chef.over, true);
  assert.equal(lola.none, true);
  assert.equal(lola.cells[2].today, true);
});

test('подвал складывает сменных и части окладов за день выдачи', () => {
  assert.deepEqual(logic.dayTotals(month2b()).map(d => d.amount), [9500000, 150000, 0]);
  const totals = logic.totals(month2b());
  assert.equal(totals.monthlyPaid, 9500000);
  // Осталось выдать — по людям: переплата шефу (500 000) не уменьшает оклад,
  // который должны Лоле (3 500 000). Так же считает итог в XLSX.
  assert.equal(totals.monthlyRest, 3500000);
  assert.equal(totals.monthlyOver, 500000);
});

test('проверки месяца: ошибки первыми, «нет выплат» — последним напоминанием', () => {
  const data = month2b();
  data.shift[0].cells['2026-09-10'] = cell({accrual_id: 2, status: 'on_time', amount: '150000', paid: '0', debt: '150000'});
  data.shift[0].cells['2026-09-12'] = cell({accrual_id: 3, status: 'missing', amount: '0', paid: '150000'});
  data.days = ['2026-09-10', '2026-09-12', ...data.days];
  const checks = logic.checks(data, {today: TODAY});
  assert.deepEqual(checks.map(c => c.lvl), ['err', 'err', 'warn', 'warn', 'todo']);
  assert.equal(checks[0].text, 'Переплата оклада: Фаррух Султанов');
  assert.equal(checks[1].text, 'Выдано без входа 12.09: Санжар Холматов');
  assert.equal(checks[2].text, 'Смена 10.09 не выдана: Санжар Холматов');
  assert.equal(checks[3].text, 'Смена 27.09 не выдана: Санжар Холматов');
  assert.equal(checks[4].text, 'Нет выплат с начала месяца: Лола Нурматова');
  assert.equal(checks[2].row, 's5');
  assert.equal(checks[2].day, '2026-09-10');
});

test('выдача из сетки — сегодняшней датой: старая смена не меняет кассу прошлого дня', () => {
  assert.equal(logic.payday('2026-09-28', TODAY), '2026-09-29'); // вчерашняя: то же, что смена + 1
  assert.equal(logic.payday('2026-09-10', TODAY), TODAY);
  assert.equal(logic.payday('2026-09-30', '2026-10-05'), '2026-10-05');
  assert.equal(logic.payday('2026-09-10'), '2026-09-11');
});

test('номер дня открывает день выдачи по плану: смена + 1, не позже сегодня', () => {
  assert.equal(logic.shiftScreenDay('2026-09-10', TODAY), '2026-09-11');
  assert.equal(logic.shiftScreenDay('2026-09-28', TODAY), '2026-09-29');
  assert.equal(logic.shiftScreenDay('2026-09-29', TODAY), '2026-09-29');
  assert.equal(logic.shiftScreenDay('2026-09-30', '2026-10-05'), '2026-10-01');
});

test('ячейка оклада: больше — доплата, меньше — правка последней выплаты, ноль — удаление всех', () => {
  const ops = [{id: 11, amount: 1000000}, {id: 12, amount: 500000}];
  assert.deepEqual(logic.monthlyEditPlan(ops, 1500000, 1500000), []);
  assert.deepEqual(logic.monthlyEditPlan(ops, 1500000, 2000000), [{action: 'add', amount: 500000}]);
  assert.deepEqual(logic.monthlyEditPlan(ops, 1500000, 1200000), [{action: 'update', id: 12, amount: 200000}]);
  assert.deepEqual(logic.monthlyEditPlan(ops, 1500000, 1000000), [{action: 'delete', id: 12}]);
  assert.deepEqual(logic.monthlyEditPlan(ops, 1500000, 400000), [{action: 'delete', id: 12}, {action: 'update', id: 11, amount: 400000}]);
  assert.deepEqual(logic.monthlyEditPlan(ops, 1500000, 0), [{action: 'delete', id: 12}, {action: 'delete', id: 11}]);
  // Выплат в данных меньше, чем в ячейке, — данные устарели, ничего не трогаем.
  assert.equal(logic.monthlyEditPlan([], 1500000, 0), null);
});

test('строка помесячного несёт выплаты дня, если сервер их прислал', () => {
  const data = month2b();
  assert.equal(logic.monthlyRows(data, {today: TODAY})[0].cells[0].ops, null);
  data.monthly_cell_ops = {'3': {'2026-09-27': [{id: 5, amount: '6000000'}, {id: 9, amount: '3500000'}]}};
  const [chef, lola] = logic.monthlyRows(data, {today: TODAY});
  assert.deepEqual(chef.cells[0].ops, [{id: 5, amount: 6000000}, {id: 9, amount: 3500000}]);
  assert.deepEqual(lola.cells[0].ops, []);
});

test('частично начисленный день: у кого препятствие — ждёт, остальные не заблокированы', () => {
  const data = month2b();
  data.days = ['2026-09-26', '2026-09-27', '2026-09-28', '2026-09-29'];
  data.confirmed_days = ['2026-09-26'];
  data.partial_days = ['2026-09-27'];
  data.shift.push({employee_id: 7, name: 'Отабек Эргашев', group: 'Обслуживание зала', rate: '150000',
    accrued: '0', paid: '0', debt: '0', cells: {}});
  const pending = {
    '2026-09-27': {7: {status: 'unlinked', payable: null, accrued: false, blocker: 'unlinked'}},
    '2026-09-28': {5: {status: 'late', payable: '150000', accrued: false, blocker: null},
      7: {status: 'on_time', payable: null, accrued: false, blocker: 'missing_rate'}},
  };
  const [sanjar, otabek] = logic.shiftRows(data, {today: TODAY, pending});
  // Закрытый день без начисления — просто не работал.
  assert.equal(otabek.cells[0].kind, 'empty');
  assert.deepEqual(otabek.cells.slice(1, 3).map(c => [c.kind, c.text]), [['blocked', 'нет привязки'], ['blocked', 'нет ставки']]);
  // Соседа по тому же дню препятствие не держит: он «к выдаче».
  assert.equal(sanjar.cells[2].kind, 'pending');
  assert.equal(otabek.rest, 0);
  // Частичный день без строки /staff — всё равно «ждёт», а не пусто.
  assert.equal(logic.shiftRows(data, {today: TODAY, pending: {}})[1].cells[1].kind, 'blocked');
  const checks = logic.checks(data, {today: TODAY, pending});
  assert.ok(checks.some(c => c.text === 'Не начислено 28.09: Отабек Эргашев' && /Сотрудниках/.test(c.sub)));
  assert.ok(checks.some(c => c.text === 'Не начислено 27.09: Отабек Эргашев' && /вручную/.test(c.sub)));
});

test('без Hikvision в месяце: к выдаче и долг до выплаты, метка остаётся после выплаты', () => {
  const day = '2026-09-28';
  const person = {employee_id: 40, name: 'Без устройства', group: 'Зал', rate: '170000',
    status: 'unlinked', first_entry: null, payable: '170000', accrued: false,
    blocker: null, hikvision_registered: false};
  const data = {days: [day], shift: [], confirmed_days: [], partial_days: []};
  const pending = {[day]: {40: person}};
  const [before] = logic.shiftRows(data, {today: TODAY, pending});
  assert.equal(before.cells[0].kind, 'pending');
  assert.equal(before.cells[0].text, 'к выдаче');
  assert.equal(before.rest, 170000);
  assert.equal(before.noHik, true);
  data.shift = [{...person, accrued: '170000', paid: '170000', debt: '0', cells: {
    [day]: {accrual_id: 3, status: 'unlinked', amount: '170000', rate: '170000',
      paid: '170000', debt: '0', payments: [{id: 4, day: TODAY, amount: '170000'}]},
  }}];
  const [after] = logic.shiftRows(data, {today: TODAY});
  assert.equal(after.cells[0].kind, 'paid');
  assert.equal(after.rest, 0);
  assert.equal(after.noHik, true);
  assert.equal(logic.gridCell(null, {day, today: TODAY, pending: {
    ...person, rate: null, payable: null, blocker: 'missing_rate',
  }}).kind, 'blocked');
});

test('новый в реестре без начислений за месяц всё равно виден в открытом дне', () => {
  const rows = logic.shiftRows(month2b(), {today: TODAY, pending: {'2026-09-28': {
    40: {employee_id: 40, name: 'Новый Официант', group: 'Обслуживание зала', rate: '170000', status: 'on_time', payable: '170000', accrued: false, blocker: null}}}});
  const added = rows.find(row => row.id === 40);
  assert.equal(added.name, 'Новый Официант');
  assert.equal(added.cells[1].kind, 'pending');
  assert.equal(added.rest, 170000);
});

test('сумма из ячейки оклада: только цифры, иначе null — не 15 сумов и не удаление', () => {
  assert.equal(logic.parseAmount(''), 0);
  assert.equal(logic.parseAmount('  '), 0);
  assert.equal(logic.parseAmount('500000'), 500000);
  assert.equal(logic.parseAmount('500 000'), 500000);
  assert.equal(logic.parseAmount('500 000'), 500000);
  assert.equal(logic.parseAmount('1,5 млн'), null);
  assert.equal(logic.parseAmount('abc'), null);
  assert.equal(logic.parseAmount('-5'), null);
  assert.equal(logic.parseAmount('12.5'), null);
});

test('переплата после правки ячейки: только при увеличении сверх оклада', () => {
  const person = {salary: 3500000, paid: 400000};
  assert.equal(logic.overpayAfter(person, {amount: 400000}, 3600000), 100000);
  assert.equal(logic.overpayAfter(person, {amount: 400000}, 3500000), 0);
  assert.equal(logic.overpayAfter({salary: 9000000, paid: 9500000}, {amount: 3500000}, 3000000), 0);
  assert.equal(logic.overpayAfter({salary: null, paid: 0}, {amount: 0}, 100), 0);
});

test('«сумма ≠ ставке» сверяется со ставкой дня смены, выданное можно отменить', () => {
  const at = extra => logic.gridCell(cell(Object.assign({accrual_id: 9, status: 'on_time', rate: '180000',
    amount: '180000', paid: '180000', debt: '0', payments: [{id: 77, day: '2026-09-11', amount: '180000'}]}, extra)),
  {rate: '200000', day: '2026-09-10', today: TODAY});
  // Сегодня ставка 200 000, но 10-го действовала 180 000 — выдано ровно.
  const paid = at({});
  assert.equal(paid.kind, 'paid');
  assert.equal(paid.cancellable, true);
  assert.deepEqual(paid.payments, [{id: 77, day: '2026-09-11', amount: 180000}]);
  const odd = at({paid: '150000', debt: '30000', payments: [{id: 78, day: '2026-09-11', amount: '150000'}]});
  assert.equal(odd.kind, 'odd');
  assert.equal(odd.rate, 180000);
  assert.equal(odd.payable, true);
  // Без выплат отменять нечего.
  assert.equal(at({paid: '0', debt: '180000', payments: []}).cancellable, false);
});

test('итоги окладов: выплачено — с сервера, осталось — по людям, переплата отдельно; удалённый из реестра не пропадает', () => {
  const data = month2b();
  data.monthly_cells['99'] = {'2026-09-28': '700000'};
  data.monthly_paid = '10500000'; // + 300 000 общим расходом без сотрудника
  const t = logic.totals(data);
  assert.equal(t.monthlyPaid, 10500000);
  assert.equal(t.monthlyRest, 3500000);
  assert.equal(t.monthlyOver, 500000);
  const rows = logic.monthlyRows(data, {today: TODAY});
  const gone = rows.find(row => row.gone);
  assert.equal(gone.name, 'Сотрудник удалён · №99');
  assert.equal(gone.paid, 700000);
  assert.equal(gone.rest, null);
  assert.equal(gone.cells[1].over, false);
  // Удалённый не попадает в «нет выплат» и в переплаты.
  assert.ok(!logic.checks(data, {today: TODAY}).some(item => item.row === 'm99'));
  // Удалённый в архиве — с именем из monthly_archived.
  data.monthly_archived = [{id: 99, name: 'Акмаль Рашидов', role: 'охрана', no_hikvision: false}];
  const named = logic.monthlyRows(data, {today: TODAY}).find(row => row.gone);
  assert.equal(named.name, 'Акмаль Рашидов');
  assert.equal(named.role, 'охрана · удалён из реестра');
});

test('«⊘ Hik» у окладника из реестра и проверка «Остаток ушёл в минус»', () => {
  const data = month2b();
  data.monthly[1].no_hikvision = true;
  assert.equal(logic.monthlyRows(data, {today: TODAY})[1].noHik, true);
  data.negative_cash = [{day: '2026-09-12', balance: '-150000'}];
  const list = logic.checks(data, {today: TODAY});
  const minus = list.find(item => item.text.startsWith('Остаток ушёл в минус'));
  assert.equal(minus.lvl, 'err');
  assert.equal(minus.day, '2026-09-12');
  assert.equal(minus.row, null);
  assert.equal(minus.sub.replace(/\s/g, ' '), 'На конец дня −150 000 сум');
  assert.equal(list[list.length - 1].lvl, 'todo');
});
