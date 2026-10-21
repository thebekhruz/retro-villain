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

test('передали больше расчёта: галочка со стрелкой вверх и не замечание', () => {
  // 16.10: получено 45 110 000 при расчёте 45 108 500 — в кассе не было мелочи на сдачу.
  const extra = {...week.days[0], date: '2026-10-16', accounting: undefined, handover: {recorded: '45110000.00',
    expected: '45108500.00', status: 'mismatch', difference: '1500.00', confirmed: true, checked: true, shortfall: '0'}};
  const mark = logic.handoverMark(extra);
  assert.equal(mark.mark, '✓↑');
  assert.equal(mark.tone, 'extra');
  assert.equal(plain(mark.tip), 'Получено 45 110 000 при расчёте 45 108 500 · сверх расчёта 1 500');
  const unconfirmed = logic.handoverMark({...extra, handover: {...extra.handover, confirmed: false, checked: false}});
  assert.equal(plain(unconfirmed.tip), 'Передано 45 110 000 при расчёте 45 108 500 · сверх расчёта 1 500');
  assert.deepEqual(logic.accountantIssues({days: [extra]}, {}, accountant.dayChecks), []);
  // Недостача — по-прежнему знак опасности и замечание.
  assert.equal(logic.handoverMark(week.days[2]).mark, '⚠');
  assert.deepEqual(logic.accountantIssues({days: [week.days[2]]}, {}, accountant.dayChecks).map(item => item.text),
    ['Кассир передал меньше расчёта · 23.09']);
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

test('постоянные замечания объясняют своё правило (ТЗ 16.10, п. 4.2)', () => {
  const day = {...week.days[0], accounting: {date: '2026-09-21', expected_cashier: '1',
    ledger: {cash_balance: '10', accruals: [{id: 7, work_day: '2026-09-20', debt: '150000'}], payroll_confirmed: true,
      manual_debt_total: '300000'}, missing_rates: 2, payroll: {unknown_count: 0}, employees: []}};
  const issues = logic.accountantIssues({days: [day]}, {}, accountant.dayChecks);
  const rules = Object.fromEntries(issues.map(item => [item.text.split(' · ')[0], item.rule]));
  assert.match(rules['Невыданные смены'], /начислена, но выдана не полностью/);
  assert.match(rules['Без ставки'], /не указана ставка/);
  assert.match(rules['Неоплаченные расходы'], /в долг и ещё не оплаченные/);
});

test('дивиденды: статус, доли и подпись выдачи', () => {
  const view = logic.dividendView({week: '2026-W39', start: '2026-09-21', end: '2026-09-27', payout_day: '2026-09-28',
    target: '10000000.00', collected: '2000000.00', pace: '5714285.71', due: '4285714.29', behind: true, done: false, free_cash_week: null});
  // Отставание — от плана к сегодня, включая сегодня (Функционал §3.7).
  assert.equal(plain(view.status), 'Отстаём от плана на 3 714 286');
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

test('ТЗ 16.10, п. 4.2: прогресс недели к порогу Счёта Шефа и отметка об уведомлении', () => {
  const week = extra => ({total: '12400000.00', limit: '30000000.00', percent: 41, reached: false,
    recipient: true, last: null, ...extra});
  const below = logic.chefWeekView(week());
  assert.equal(below.progress, '12,4 млн из 30 млн');
  assert.equal(below.pct, 41);
  assert.equal(below.reached, false);
  assert.equal(below.status, 'При 30 млн за неделю придёт сообщение в Telegram');
  assert.equal(logic.chefWeekView(week({recipient: false})).status, 'Получатель уведомления не настроен');
  const reached = logic.chefWeekView(week({total: '31250000.00', percent: 100, reached: true,
    last: {status: 'sent', at: '2026-10-21T10:15:02.120000+05:00'}}));
  assert.equal(reached.title, 'Порог 30 млн за неделю достигнут');
  assert.equal(reached.progress, '31,3 млн из 30 млн');
  assert.equal(reached.status, 'Уведомление отправлено 21.10 10:15');
  assert.equal(logic.chefWeekView(week({reached: true, last: {status: 'failed', at: '2026-10-21T10:15:00+05:00'}})).status,
    'Уведомление не ушло, повторим через 15 минут');
  assert.equal(logic.chefWeekView(week({reached: true})).status, 'Уведомление уйдёт при следующей проверке');
  assert.equal(logic.chefWeekView(week({reached: true, recipient: false, last: {status: 'no_recipient'}})).status,
    'Получатель уведомления не настроен');
  assert.equal(logic.chefWeekView(null), null);
});

test('короткие суммы: миллионы и миллиарды', () => {
  assert.equal(logic.short(1438528898), '1,44 млрд');
  assert.equal(logic.short(42500000), '42,5 млн');
  assert.equal(logic.short(null), '—');
});

test('история дивидендов: собрано из цели и выдача в понедельник', () => {
  const view = logic.dividendView({start: '2026-09-28', end: '2026-10-04', payout_day: '2026-10-05', target: '10000000', collected: '0',
    history: [{start: '2026-09-21', end: '2026-09-27', payout_day: '2026-09-28', target: '10000000.00', collected: '10500000.00', paid_out: '10000000.00', done: true},
      {start: '2026-09-14', end: '2026-09-20', payout_day: '2026-09-21', target: null, collected: '3500000.00', paid_out: '0.00', done: false}]});
  assert.deepEqual(view.history.map(week => [week.range, week.text, week.paid, week.tone]), [
    ['21.09–27.09', 'собрано 10,5 млн из 10 млн', 'выдано 28.09 · 10 млн', 'ok'],
    ['14.09–20.09', 'собрано 3,5 млн · цели не было', '', 'none']]);
});

test('замечания 7b: недостача от кассира первой, выдано без входа, постоянные — один раз', () => {
  const accounting = date => ({date, expected_cashier: '1', missing_rates: 5, payroll: {unknown_count: 1}, employees: [],
    ledger: {cash_balance: '100', payroll_confirmed: true, manual_debt_total: '0', accruals: [
      {work_day: '2026-09-19', name: 'Старый', amount: '0', paid: '150000'},  // выдана до недели — не берём
      {work_day: '2026-09-22', name: 'Алина', amount: '0', paid: '180000'},
      {work_day: '2026-09-22', name: 'Жасур', amount: '180000', paid: '180000'}]}});
  const days = {start: '2026-09-21', days: [
    {...week.days[0], accounting: accounting('2026-09-21')},
    {...week.days[1], accounting: accounting('2026-09-22')},
    {...week.days[2], date: '2026-09-23', handover: {recorded: '8700000.00', expected: '9000000.00', status: 'mismatch',
      difference: '-300000.00', confirmed: true, shortfall: '300000'}, accounting: accounting('2026-09-23')}]};
  const spending = {shokh: {flagged: [{day: '2026-09-23', item: 'Лук', total: '100000', reason: 'нет фото'}]}};
  const issues = logic.accountantIssues(days, spending, accountant.dayChecks);
  assert.deepEqual(issues.map(item => item.text), [
    'От кассира получено меньше расчёта · 23.09', 'Выдано без входа: Алина · 22.09',
    'Без ставки · 23.09', 'Покупка Шоха: Лук · 23.09']);
  assert.match(plain(logic.handoverMark(days.days[2]).tip), /Получено 8 700 000 при расчёте 9 000 000 · недостача 300 000/);
});

test('T-494: подпись прогноза называет, сколько недель реально в среднем', () => {
  const note = weeks => logic.forecastNote({weeks: 8, sample_weeks: weeks});
  assert.equal(note({min: 8, max: 8}), 'Среднее за 8 недель по кассам Retro и Oxbridge, без банкетного зала. Сегодня выделен.');
  assert.equal(note({min: 1, max: 1}), 'Среднее за 1 неделю по кассам Retro и Oxbridge, без банкетного зала. Сегодня выделен.');
  assert.equal(note({min: 7, max: 8}), 'Среднее за 7–8 недель по кассам Retro и Oxbridge, без банкетного зала. Сегодня выделен.');
  assert.equal(note({min: 0, max: 0}), 'За последние восемь недель продаж в iiko нет.');
});

test('T-494: блюда — по справочнику меню, без справочника ничего не прячем', () => {
  const rows = [
    {name: 'Плов', revenue: 900, profit: 500}, {name: 'Мёд', revenue: 800, profit: 450},
    {name: 'Контейнер', revenue: 50, profit: 40}, {name: 'Новинка', revenue: 300, profit: 120},
    {name: 'Чай', revenue: 0, profit: 0}];
  const checked = {item_classes: {'Мёд': 'ingredient', 'Контейнер': 'stock', 'Новинка': 'unknown'}, item_classes_checked: true};
  // Нет в номенклатуре (unknown) — не прячем; без выручки — не блюдо продаж.
  assert.deepEqual(logic.soldDishes(checked, rows).map(row => row.name), ['Плов', 'Новинка']);
  const empty = {item_classes: {'Плов': 'unknown', 'Мёд': 'unknown'}, item_classes_checked: false};
  assert.deepEqual(logic.soldDishes(empty, rows).map(row => row.name), ['Плов', 'Мёд', 'Контейнер', 'Новинка']);
});

test('T-506: строка сегодняшнего дня — время чтения iiko, смена и «нет данных» вместо нулей', () => {
  const open = {cashier: {fetched_at: '2026-09-24T14:05:31+05:00', shift: {open: true, opened_at: '2026-09-24T09:01:00', closed_at: null}}};
  assert.equal(logic.todayStatus(open, '2026-09-24'), 'Сегодня ещё идёт · данные на 14:05');
  // Смены нет в ответе (не прочитана) — день считаем идущим, а не закрытым.
  assert.equal(logic.todayStatus({cashier: {fetched_at: '2026-09-24T14:05:31+05:00', shift: null}}, '2026-09-24'),
    'Сегодня ещё идёт · данные на 14:05');
  const closed = {cashier: {fetched_at: '2026-09-24T23:50:00+05:00', shift: {open: false, opened_at: '2026-09-24T09:01:00', closed_at: '2026-09-24T23:40:12'}}};
  assert.equal(logic.todayStatus(closed, '2026-09-24'),
    'Сегодня · смена закрыта, итог дня может пересчитаться iiko до утра · данные на 23:50');
  // Ответ прочитан вчера — с датой: старое время не выглядит сегодняшним.
  assert.equal(logic.todayStatus({cashier: {fetched_at: '2026-09-23T23:59:00+05:00'}}, '2026-09-24'),
    'Сегодня ещё идёт · данные на 23.09 23:59');
  assert.equal(logic.todayStatus({cashier: null, cashier_error: 'iiko недоступен'}, '2026-09-24'), 'Сегодня · нет данных iiko');
  assert.equal(logic.todayStatus(undefined, '2026-09-24'), '');
});

test('T-512: краткий отчёт кассира — продажи, оплаты, «К передаче» по шагам и отметка бухгалтера', () => {
  const day = {date: '2026-10-18', handover: {recorded: '31348000.00', expected: '31346500.00', status: 'mismatch', difference: '1500.00', confirmed: true},
    cashier: {register_revenue: '32492000.00', expected_handover: '31346500.00', shift: {open: false, closed_at: '2026-10-19T00:41:10'},
      report: {sales: '32492000.00', receipts_count: 207, average: '156966.18',
        payments: [{name: 'Наличные', amount: '31000000.00'}, {name: 'Uzcard', amount: '1492000.00'}],
        demo_cash: '31000000', cash_prepayment: '0', receipts: '646500', expenses: '300000', shokh: '0', usd_total: '0',
        handover: '31346500.00', issues: [], revenue: '40492000.00',
        prepayments: {count: 2, total: '8000000.00', methods: [{name: 'Payme', amount: '5000000.00'}, {name: 'Наличные', amount: '3000000.00'}]}}}};
  const view = logic.cashierBrief(day, '2026-10-19');
  assert.equal(view.title, 'Выручка за день');
  assert.equal(view.sales, 40492000);  // продажи + полученные предоплаты
  assert.equal(view.prepayments.title, 'Предоплаты получены · 2');
  assert.equal(plain(view.prepayments.total), '8 000 000');
  assert.deepEqual(view.prepayments.rows.map(row => row.label), ['Payme', 'Наличные']);
  assert.equal(plain(view.checks), '207 чеков · средний 156 966');
  assert.deepEqual(view.pill, {text: 'Смена закрыта 00:41', tone: 'ok'});
  assert.equal(plain(view.payments[0].value), '31 000 000');
  assert.equal(view.handover, 31346500);
  assert.deepEqual(view.steps.map(row => row.label), ['Наличные', '+ Предоплаты наличными', '+ Поступления', '− Расходы', '− На закуп', '− Доллары в сейф']);
  assert.equal(view.steps[1].muted, true);  // ноль приглушён
  assert.deepEqual(view.steps.at(-1), {label: '− Доллары в сейф', value: '$0', muted: true, details: []});  // без взносов — строка есть, приглушена (T-520)
  assert.equal(view.received.label, 'Бухгалтер подтвердил');
  assert.equal(view.received.mark, '✓↑');  // передали больше расчёта — норма (T-507)
});

test('T-512: касса сегодня — смена открыта, ещё не передано; доллары отдельной строкой; нет iiko — ошибка', () => {
  const today = {date: '2026-10-19', handover: {recorded: null, expected: '900000.00', status: 'pending', difference: null},
    cashier: {shift: {open: true, closed_at: null}, report: {sales: '1000000', receipts_count: 1, average: '1000000', payments: [],
      demo_cash: '900000', cash_prepayment: '0', receipts: '0', expenses: '0', shokh: '0', usd_total: '100', handover: null,
      issues: ['Нет курса Retro за этот день. Обновите курс USD перед передачей.'], revenue: null, prepayments: null}}};
  const view = logic.cashierBrief(today, '2026-10-19');
  assert.equal(view.title, 'Продажи по кассе');  // реестр предоплат не пришёл
  assert.equal(view.sales, 1000000);
  assert.equal(view.prepayments, null);
  assert.equal(view.notes[0], 'Предоплаты за день недоступны — выручка требует проверки.');
  assert.deepEqual(view.pill, {text: 'Смена открыта', tone: 'wait'});
  assert.equal(plain(view.checks), '1 чек · средний 1 000 000');
  assert.equal(view.received.value, 'ещё не передано');
  assert.equal(view.received.mark, '');
  assert.equal(view.handover, null);
  assert.equal(view.steps.at(-1).label, '− Доллары в сейф');
  assert.equal(view.steps.at(-1).value, '$100');
  assert.equal(view.notes.length, 2);
  assert.deepEqual(logic.cashierBrief({date: '2026-10-19', cashier: null, cashier_error: 'iiko не ответил'}, '2026-10-19'), {error: 'iiko не ответил'});
  assert.equal(logic.cashierBrief(null), null);
});

test('T-512: отчёт бухгалтера за сегодня — остаток, движение денег и сдан ли отчёт', () => {
  const day = {date: '2026-10-19', closing_balance: '425000', accounting: {
    ledger: {cash_flow: {opening_balance: '425000', received_from_cashier: null, other_receipts: '0', salary_paid: '1200000',
      other_outflows: '300000', closing_balance: '-1075000'},
    movements: [{type: 'opening'}, {type: 'salary_payment'}, {type: 'other_expense'}]}, day_report: null}};
  const view = logic.accountantBrief(day);
  assert.equal(view.balance, -1075000);
  assert.equal(view.ops, '2 операции за день');
  assert.deepEqual(view.pill, {text: 'Отчёт дня не сдан', tone: 'wait'});
  assert.deepEqual(view.rows.map(row => row.label), ['На утро', '+ От кассира', '+ Прочие поступления', '− Зарплаты', '− Прочие расходы']);
  assert.equal(view.rows[1].value, 'не записано');
  assert.equal(view.pendingNote, '');
  // Кассир передал 31 348 000, бухгалтер ещё не подтвердил: в книге остаток 425 000,
  // а у бухгалтера на руках — утро + от кассира.
  const pending = {date: '2026-10-19', accounting: {ledger: {cash_flow: {opening_balance: '425000', received_from_cashier: '31348000',
    received_counted: '0', handover_status: 'pending', other_receipts: '0', salary_paid: '0', other_outflows: '0', closing_balance: '425000'},
  movements: [{type: 'opening'}, {type: 'cashier_transfer'}]}, day_report: null}};
  const held = logic.accountantBrief(pending);
  assert.equal(held.balance, 31773000);
  assert.equal(plain(held.pendingNote), 'Включая 31 348 000 от кассира — бухгалтер ещё не подтвердил получение');
  pending.accounting.ledger.cash_flow.received_counted = '31348000';
  pending.accounting.ledger.cash_flow.closing_balance = '31773000';
  assert.equal(logic.accountantBrief(pending).balance, 31773000);  // подтвердил — не прибавляем дважды
  assert.equal(logic.accountantBrief(pending).pendingNote, '');
  day.accounting.day_report = {submitted_at: '2026-10-19T13:15:00+05:00', changed: false};
  assert.deepEqual(logic.accountantBrief(day).pill, {text: 'Отчёт сдан 13:15', tone: 'ok'});
  day.accounting.day_report.changed = true;
  assert.equal(logic.accountantBrief(day).pill.tone, 'bad');
  assert.deepEqual(logic.accountantBrief({date: '2026-10-19'}), {error: 'Нет данных бухгалтера за этот день'});
});

test('T-520: строки отчёта бухгалтера раскрываются операциями дня, доллары в сейфе — отдельной строкой', () => {
  const day = {date: '2026-10-20', closing_balance: '26321000', accounting: {
    ledger: {cash_flow: {opening_balance: '1095000', received_from_cashier: '56076000', received_counted: '56076000',
      other_receipts: '0', salary_paid: '12260000', other_outflows: '18590000', closing_balance: '26321000'},
    movements: [
      {type: 'opening', description: 'Остаток на начало дня', amount: '1095000'},
      {type: 'auto_cashier', description: 'Касса за 19.10.2026', amount: '56076000'},
      {type: 'salary_payment', description: 'Оклады · Иванова · за 2026-10-19', amount: '12260000'},
      {type: 'other_expense', description: 'Коммунальные · Газ', amount: '600000', item_code: 'utilities'},
      {type: 'procurement_advance', description: 'Выдано Шоху на закуп', amount: '17000000'},
      {type: 'reserve_transfer', description: 'Отложено в сейф', amount: '990000'},
    ]}, day_report: null},
    usd: {date: '2026-10-20', deposits: [{amount: '120', created_at: '2026-10-20T21:10:00+05:00'}], total: '120', safe_balance: '3450'}};
  const view = logic.accountantBrief(day);
  assert.deepEqual(view.rows.map(row => row.label), ['На утро', '+ От кассира', '+ Прочие поступления', '− Зарплаты', '− Прочие расходы']);
  assert.equal(view.rows[0].details, undefined);  // «На утро» не раскрывается
  assert.deepEqual(view.rows[1].details.map(item => item.label), ['Касса за 19.10.2026']);
  assert.deepEqual(view.rows[2].details, []);  // нечего раскрывать — обычная строка
  assert.deepEqual(view.rows[3].details.map(item => item.label), ['Оклады · Иванова · за 2026-10-19']);
  assert.deepEqual(view.rows[4].details.map(item => [item.label, plain(item.value)]),
    [['Коммунальные · Газ', '600 000'], ['Выдано Шоху на закуп', '17 000 000'], ['Отложено в сейф', '990 000']]);
  assert.equal(view.usd.label, 'Доллары в сейфе');
  assert.equal(plain(view.usd.value), '$3 450');
  assert.equal(view.usd.muted, false);
  assert.deepEqual(view.usd.details, [{label: 'Взнос кассира · 21:10', value: '$120'}]);
  // Сейф не пересчитан и взносов нет — «не задан», приглушено; нет данных о долларах — строки нет.
  assert.deepEqual(logic.accountantBrief({...day, usd: {deposits: [], total: '0', safe_balance: null}}).usd,
    {label: 'Доллары в сейфе', value: 'не задан', muted: true, details: []});
  assert.equal(logic.accountantBrief({...day, usd: null}).usd, null);
  // Касса: взносы дня за строкой долларов.
  const cashier = {date: '2026-10-20', handover: {recorded: null}, usd: day.usd,
    cashier: {shift: {open: true}, report: {sales: '1000000', payments: [], demo_cash: '900000', cash_prepayment: '0', receipts: '0',
      expenses: '0', shokh: '0', usd_total: '120', handover: null, issues: [], revenue: null, prepayments: null}}};
  const dollars = logic.cashierBrief(cashier, '2026-10-20').steps.at(-1);
  assert.equal(dollars.value, '$120');
  assert.equal(dollars.muted, false);
  assert.deepEqual(dollars.details, [{label: 'Взнос кассира · 21:10', value: '$120'}]);
});
