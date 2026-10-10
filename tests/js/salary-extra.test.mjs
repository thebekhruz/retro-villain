import test from 'node:test';
import assert from 'node:assert/strict';
import extra from '../../retro/static/salary-extra-logic.js';
import day from '../../retro/static/salary-day-logic.js';
import accountant from '../../retro/static/accountant-logic.js';

/* Ведомость октября с примерами ТЗ 09.10: Ихтиер (клетка 09.10 по ставке),
   Сельвина (без клетки), Карамат (временная, только доп. выплата). */
const month = extras => ({month: '2026-10', today: '2026-10-09', entry_start: '2026-10-02', closed: false,
  days: ['2026-10-08', '2026-10-09', '2026-10-10'],
  people: [
    {id: 1, name: 'Баходиров Ихтиер', role: 'Менеджер', group: 'Управление', rate: '360000',
      cells: {'2026-10-08': {amount: '360000'}, '2026-10-09': {amount: '360000'}}},
    {id: 3, name: 'Абдулганиева Сельвина', role: 'Хостес', group: 'Встреча гостей', rate: '360000', cells: {}},
    {id: 9, name: 'Карамат', role: 'Хостес', group: 'Встреча гостей', rate: '150000', temporary: true, cells: {}},
    {id: 7, name: 'Ушедший', role: 'Официант', archived: true, cells: {}}],
  extras: extras ?? [{id: 5, employee_id: 9, name: 'Карамат', role: 'Хостес', temporary: true, work_day: '2026-10-08',
    paid_day: '2026-10-09', amount: '150000', note: 'Подмена хостес', editable: true}]});

test('доп. выплата входит в «Выдано» сотрудника и итог дня выплаты, но не в клетку', () => {
  const view = day.matrix(month());
  const karamat = view.people.find(p => p.id === 9);
  assert.equal(karamat.paid, 150000);
  assert.equal(karamat.extra, 150000);
  assert.equal(karamat.cells[1].amount, 0, 'клетка Карамат 09.10 пустая');
  assert.equal(karamat.cells[1].extra, 150000);
  assert.equal(view.perDay['2026-10-09'], 360000 + 150000);
  assert.equal(view.perDay['2026-10-08'], 360000);
  assert.equal(view.total, 360000 * 2 + 150000);
  assert.equal(day.extraOf(month(), 9, '2026-10-09'), 150000);
  assert.equal(day.extraOf(month(), 1, '2026-10-09'), 0);
  assert.throws(() => day.matrix(month([{id: 1, employee_id: 9, paid_day: '2026-10-09', amount: 'abc'}])), /прочитать/);
});

test('кому можно записать: реестр с временными, без архива; поиск по подписи и по имени', () => {
  const list = extra.choices(month().people);
  assert.deepEqual(list.map(item => item.label),
    ['Абдулганиева Сельвина · Хостес', 'Баходиров Ихтиер · Менеджер', 'Карамат · Хостес · временный']);
  assert.equal(extra.findPerson(month().people, 'карамат · хостес · временный').id, 9);
  assert.equal(extra.findPerson(month().people, '  Баходиров   Ихтиер ').id, 1, 'одно такое имя — тоже находим');
  assert.equal(extra.findPerson(month().people, 'Ушедший'), null, 'архивному новую выплату не пишем');
  assert.equal(extra.findPerson(month().people, 'Карам'), null, 'кусок имени — не выбор');
  // Полные тёзки различаются номером, имя без подписи их не выбирает.
  const twins = [{id: 1, name: 'Алижон', role: 'Повар'}, {id: 2, name: 'Алижон', role: 'Повар'}];
  assert.deepEqual(extra.choices(twins).map(item => item.label), ['Алижон · Повар · №1', 'Алижон · Повар · №2']);
  assert.equal(extra.findPerson(twins, 'Алижон'), null);
  assert.equal(extra.findPerson(twins, 'Алижон · Повар · №2').id, 2);
});

test('та же выдача второй раз: выплата в клетке за день или за смену, такая же доп. выплата', () => {
  const data = month(), [ihtiyor, selvina, karamat] = data.people;
  const [cell] = extra.warnings(data, ihtiyor, '2026-10-08', '2026-10-09', 50000);
  assert.match(cell, /уже отмечена выплата в клетке: 09\.10 — 360\s000 сум \(смена 08\.10\)/);
  // Смена 07.10 выдана в клетке 08.10, а доп. выплату пишут 09.10 — тоже вопрос.
  assert.match(extra.warnings(data, ihtiyor, '2026-10-07', '2026-10-10', 1)[0], /08\.10 — 360\s000 сум \(смена 07\.10\)/);
  assert.deepEqual(extra.warnings(data, selvina, '2026-10-08', '2026-10-09', 100000), []);
  assert.match(extra.warnings(data, karamat, '2026-10-08', '2026-10-09', 150000)[0], /Такая доп\. выплата уже записана/);
  assert.deepEqual(extra.warnings(data, karamat, '2026-10-08', '2026-10-09', 150000, 5), [], 'правка самой записи — не повтор');
  assert.deepEqual(extra.warnings(data, karamat, '2026-10-08', '2026-10-09', 100000), []);
});

test('проверка ввода — как на сервере: даты, сумма, назначение', () => {
  const data = month(), person = data.people[2];
  const ok = {person, work: '2026-10-08', paid: '2026-10-09', amount: 150000, note: 'Подмена'};
  assert.equal(extra.check(ok, data), null);
  assert.match(extra.check({...ok, person: null}, data), /Выберите сотрудника/);
  assert.match(extra.check({...ok, paid: '2026-10-10'}, data), /будущим днём/);
  assert.match(extra.check({...ok, paid: '2026-10-01', work: '2026-09-30'}, data), /02\.10\.2026/);
  assert.match(extra.check({...ok, work: '2026-10-09', paid: '2026-10-08'}, data), /позже дня выплаты/);
  assert.match(extra.check({...ok, work: '2026-09-30'}, data), /01\.10\.2026/);
  assert.equal(extra.check({...ok, work: '2026-10-01', paid: '2026-10-02'}, data), null);
  assert.match(extra.check({...ok, amount: null}, data), /сумму/);
  assert.match(extra.check({...ok, amount: 0}, data), /сумму/);
  assert.match(extra.check({...ok, note: '  '}, data), /назначение/);
  assert.deepEqual(extra.defaults('2026-10-01'), {paid: '2026-10-01', work: '2026-09-30'});
  assert.equal(extra.parseAmount('150 000'), 150000);
});

test('список под ведомостью: новые сверху, тот же фильтр, пометка временного и архива', () => {
  const data = month([
    {id: 4, employee_id: 1, name: 'Баходиров Ихтиер', role: 'Менеджер', work_day: '2026-10-07', paid_day: '2026-10-08', amount: '50000', note: 'Премия'},
    {id: 5, employee_id: 9, name: 'Карамат', role: 'Хостес', temporary: true, work_day: '2026-10-08', paid_day: '2026-10-09', amount: '150000', note: 'Подмена'},
    {id: 6, employee_id: 7, name: 'Ушедший', role: 'Официант', work_day: '2026-10-08', paid_day: '2026-10-09', amount: '20000', note: ''}]);
  const all = extra.rows(data);
  assert.deepEqual(all.map(item => item.id), [6, 5, 4]);
  assert.equal(extra.total(all), 220000);
  const hosts = extra.rows(data, person => person.group === 'Встреча гостей');
  assert.deepEqual(hosts.map(item => item.id), [5]);
  assert.equal(extra.describe(hosts[0]), 'выплата 09.10 · смена 08.10 · Подмена');
  assert.equal(extra.roleLine(hosts[0]), 'Хостес · временный');
  assert.equal(extra.roleLine(all[0]), 'Официант · архив');
});

test('журнал «Финансов дня»: доп. выплаты — одна строка «Авто», раскрывается по людям; на дэшборде — к сменным', () => {
  const data = {date: '2026-10-09', reserves: {}, extra_payouts: [
    {id: 5, name: 'Карамат', temporary: true, work_day: '2026-10-08', amount: '150000', note: 'Подмена хостес'},
    {id: 6, name: 'Абдулганиева Сельвина', work_day: '2026-10-08', amount: '100000', note: 'Доплата'}],
  ledger: {cash_balance: '750000', cash_flow: {opening_balance: '1000000', other_receipts: '0', salary_paid: '0', other_outflows: '290000'},
    movements: [
      {id: 1, type: 'other_expense', item_code: 'salary_extra_payout', description: 'Доп. выплата · Карамат · смена 08.10 · Подмена хостес', amount: '150000'},
      {id: 2, type: 'other_expense', item_code: 'salary_extra_payout', description: 'Доп. выплата · Абдулганиева Сельвина · смена 08.10 · Доплата', amount: '100000'},
      {id: 3, type: 'other_expense', item_code: 'admin_other', description: 'Прочие расходы · Канцтовары', amount: '40000'}]}};
  const {rows, total} = accountant.journal(data, accountant.catalogIndex([]));
  assert.equal(total, 290000);
  const auto = rows.find(row => row.group === 'extra');
  assert.equal(auto.name, 'Доп. выплаты · 2 чел.');
  assert.equal(auto.cat, 'Зарплата');
  assert.equal(auto.amount, 250000);
  assert.deepEqual(auto.children.map(child => [child.name, child.amount, child.readonly]), [
    ['Карамат · временный · смена 08.10 · Подмена хостес', 150000, true],
    ['Абдулганиева Сельвина · смена 08.10 · Доплата', 100000, true]]);
  assert.equal(rows.filter(row => row.kind === 'expense').length, 1, 'отдельными расходами их нет');
  const card = accountant.cashCard({expected_cashier: '0', ...data});
  assert.deepEqual([card.shift, card.other], [250000, 40000]);
});

/* T-434: Карамат работает с 08.10 по 10.10. В «Кому» её нет, если смена вне
   периода; набранное руками имя получает отказ словами, как у сервера. */
test('временный с периодом: в «Кому» — только за смены периода, отказ словами', () => {
  const data = month([]);
  const karamat = data.people.find(p => p.id === 9);
  Object.assign(karamat, {work_from: '2026-10-08', work_to: '2026-10-10', work_period: '08.10–10.10'});
  const labels = work => extra.choices(data.people, work).map(item => item.label);
  assert.ok(labels('2026-10-08').includes('Карамат · Хостес · временный · 08.10–10.10'));
  assert.ok(labels('2026-10-10').includes('Карамат · Хостес · временный · 08.10–10.10'));
  assert.ok(!labels('2026-10-07').some(label => label.startsWith('Карамат')));
  assert.ok(!labels('2026-10-11').some(label => label.startsWith('Карамат')));
  assert.equal(labels('2026-10-07').length, 2, 'сменные — в любой день');
  assert.equal(labels(undefined).length, 3, 'без даты смены — все');
  assert.equal(extra.inPeriod(karamat, '2026-10-09'), true);
  assert.equal(extra.inPeriod({work_from: '2026-10-08'}, '2026-12-01'), true, 'без конца — открыто вперёд');
  assert.equal(extra.inPeriod({work_to: '2026-10-10'}, '2026-10-11'), false);
  const base = {person: karamat, paid: '2026-10-09', amount: 150000, note: 'Подмена'};
  assert.equal(extra.check({...base, work: '2026-10-07'}, data),
    'Карамат работает с 08.10 по 10.10 — доп. выплату за смену 07.10 записать нельзя.');
  assert.equal(extra.check({...base, work: '2026-10-08'}, data), null);
  assert.equal(extra.outsideText({name: 'Карамат', work_from: '2026-10-08'}, '2026-10-07'),
    'Карамат работает с 08.10 — доп. выплату за смену 07.10 записать нельзя.');
  assert.equal(extra.outsideText({name: 'Карамат', work_to: '2026-10-10'}, '2026-10-11'),
    'Карамат работает по 10.10 — доп. выплату за смену 11.10 записать нельзя.');
  assert.equal(extra.outsideText({name: 'Карамат', work_from: '2026-10-08', work_to: '2026-10-08'}, '2026-10-09'),
    'Карамат работает только 08.10 — доп. выплату за смену 09.10 записать нельзя.');
  // Список под ведомостью и журнал «Финансов дня» — с периодом.
  assert.equal(extra.roleLine({role: 'Хостес', temporary: true, work_period: '08.10–10.10'}), 'Хостес · временный · 08.10–10.10');
  assert.equal(extra.roleLine({role: 'Хостес', temporary: true, person: karamat}), 'Хостес · временный · 08.10–10.10');
  const {rows} = accountant.journal({date: '2026-10-09', reserves: {}, extra_payouts: [
    {id: 5, name: 'Карамат', temporary: true, work_period: '08.10–10.10', work_day: '2026-10-08', amount: '150000', note: 'Подмена'}],
  ledger: {cash_balance: '0', cash_flow: {}, movements: [
    {id: 1, type: 'other_expense', item_code: 'salary_extra_payout', description: 'Доп. выплата · Карамат', amount: '150000'}]}},
  accountant.catalogIndex([]));
  assert.equal(rows.find(row => row.group === 'extra').children[0].name, 'Карамат · временный · 08.10–10.10 · смена 08.10 · Подмена');
});

test('shift grid defaults to selected shift and warns using actual shift/payment dates', () => {
  assert.deepEqual(extra.defaults('2026-10-09', 'shift'), {work:'2026-10-09',paid:'2026-10-10'});
  assert.deepEqual(extra.defaults('2026-10-31', 'shift'), {work:'2026-10-31',paid:'2026-11-01'});
  const data = {...month(), basis:'shift'};
  const person = {...data.people[0], cells: {'2026-10-08': {amount:'360000',paid_days:['2026-10-09']}}};
  assert.match(extra.warnings(data, person, '2026-10-08', '2026-10-10', 50000)[0], /08\.10 — 360\s000 сум \(смена 08\.10\)/);
  assert.equal(extra.warnings(data, person, '2026-10-07', '2026-10-09', 50000).length, 1);
  assert.deepEqual(extra.warnings(data, person, '2026-10-09', '2026-10-10', 50000), []);
});
