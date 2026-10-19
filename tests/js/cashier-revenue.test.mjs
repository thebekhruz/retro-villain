import test from 'node:test';
import assert from 'node:assert/strict';
import logic from '../../retro/static/cashier-logic.js';

/* ТЗ «Выручка, оплаты и предоплаты» (08.10.2026): день 07.10 из живого iiko. */
const day = extra => ({
  revenue: '57003000', receipt_count: 101, average_receipt: '564386.13',
  daily_revenue: '59003000', received_prepayments_total: '2000000',
  payments: [{name: 'Демо', amount: '24464000'}],
  payment_groups: [
    {name: 'Оплата наличными', total: '28869000', types: [
      {name: 'Демо', paid: '24464000', redeemed: '1000000', total: '25464000'},
      {name: 'Наличные (Инкасса QR)', paid: '3405000', redeemed: '0', total: '3405000'}]},
    {name: 'Банковские карты', total: '34634000', types: [
      {name: 'UzCard', paid: '15429500', redeemed: '0', total: '15429500'},
      {name: 'Uzum', paid: '0', redeemed: '0', total: '0'},
      {name: 'Xumo', paid: '4801500', redeemed: '5500000', total: '10301500'},
      {name: 'Единый QR', paid: '0', redeemed: '0', total: '0'},
      {name: 'Я Rahmat', paid: '8903000', redeemed: '0', total: '8903000'}]}],
  full_total: '63503000', full_receipt_count: 101, full_average_receipt: '628742.57',
  groups_total: '63503000', groups_match: true, redeemed_total: '6500000', real_cash: '57003000',
  ...extra,
});

test('кассы iiko: только оплаты, доля от оплат и полученных авансов; нулевые типы скрыты', () => {
  const view = logic.revenueGroups(day());
  assert.equal(view.fallback, false);
  assert.equal(view.total, 59003000);
  assert.equal(view.receipts, 101);
  assert.deepEqual(view.groups.map(group => group.name), ['Оплата наличными', 'Банковские карты']);
  const [cash, cards] = view.groups;
  assert.equal(cash.total + cards.total + view.received, view.total);
  assert.equal(cash.percent, 47.2);
  assert.deepEqual(cards.shown.map(type => type.name), ['UzCard', 'Xumo', 'Я Rahmat']);
  assert.equal(cards.hidden, 2);
  assert.equal(cards.types.length, 5);
});

test('«Демо» — это наличные к бухгалтеру, инкассовый QR — нет', () => {
  const [cash] = logic.revenueGroups(day()).groups;
  assert.deepEqual(cash.types.map(type => [type.label, type.isCash, type.goesToSafe]),
    [['Наличные', true, false], ['Наличные (Инкасса QR)', false, true]]);
  assert.equal(cash.hasCash, true);
  assert.equal(cash.types[0].redeemed, 1000000);
});

test('дневная выручка = оплаты счетов + полученные авансы; зачёт не прибавляется', () => {
  const view = logic.revenueGroups(day());
  assert.equal(view.redeemed, 6500000);
  assert.equal(view.realCash, 57003000);
  assert.equal(view.total, view.realCash + view.received);
});

test('сверка с iiko: совпало — ✓, разошлось — разница со знаком', () => {
  assert.equal(logic.revenueGroups(day()).match, true);
  const off = logic.revenueGroups(day({daily_revenue: '59015000'}));
  assert.equal(off.match, false);
  assert.equal(off.difference, -12000);
});

test('iiko не отдал кассы: оплаты одним списком, дневной итог включает новые авансы', () => {
  const view = logic.revenueGroups(day({payment_groups: null, full_total: undefined}));
  assert.equal(view.fallback, true);
  assert.equal(view.total, 59003000);
  assert.equal(view.realCash, 57003000);
  assert.equal(view.redeemed, null);
  assert.equal(view.groups.length, 1);
  assert.equal(view.groups[0].types[0].label, 'Наличные');
  assert.equal(logic.revenueGroups(null), null);
});

test('неизвестные полученные авансы не превращаются в ноль или полную сумму счетов', () => {
  const view = logic.revenueGroups(day({daily_revenue: null, received_prepayments_total: null}));
  assert.equal(view.total, null);
  assert.equal(view.received, null);
  assert.equal(view.match, null);
  assert.equal(view.groups[0].percent, null);
  assert.equal(view.realCash, 57003000);
  assert.equal(logic.revenueGroups(day({daily_revenue: undefined})).total, null);
});

test('аванс без закрытых счетов даёт выручку; закрытие без доплаты не даёт повторной', () => {
  const received = logic.revenueGroups(day({revenue: '0', daily_revenue: '2000000',
    payment_groups: [], full_receipt_count: 0, redeemed_total: '0', full_average_receipt: null}));
  assert.equal(received.total, 2000000);
  assert.equal(received.receipts, 0);
  assert.equal(received.match, true);
  const credited = logic.revenueGroups(day({revenue: '0', daily_revenue: '0',
    received_prepayments_total: '0', payment_groups: [
      {name: 'Наличные', types: [{name: 'Демо', paid: '0', redeemed: '2000000', total: '2000000'}]}],
    redeemed_total: '2000000'}));
  assert.equal(credited.total, 0);
  assert.equal(credited.groups[0].total, 0);
  assert.equal(credited.match, true);
});

test('статус предоплаты: ждёт, зачтена с датой, возврат; дата события на статус не влияет', () => {
  const today = '2026-10-08';
  assert.equal(logic.prepaymentStatus({status: 'pending', event_day: '2026-10-09'}, today).key, 'pending');
  assert.deepEqual(logic.prepaymentStatus({status: 'credited', credited_day: '2026-10-07'}, today),
    {key: 'credited', text: 'зачтена', day: '2026-10-07'});
  assert.equal(logic.prepaymentStatus({status: 'refund', credited_day: '2026-10-07'}, today).key, 'refund');
  // «На дату» скрыта (13.10.2026): прошедшая дата больше не даёт сигнал «дата прошла».
  assert.equal(logic.prepaymentStatus({status: 'pending', event_day: '2026-10-06'}, today).key, 'pending');
  assert.equal(logic.prepaymentStatus({status: 'pending', event_day: null}, today).key, 'pending');
});

test('сумма строки: «Зачтено» — сколько зачли, остальное — сколько внесли', () => {
  const row = {amount: '50000000', credited_amount: '48000000'};
  assert.equal(logic.prepaymentAmount(row, 'received'), 50000000);
  assert.equal(logic.prepaymentAmount(row, 'credited'), 48000000);
  assert.equal(logic.prepaymentAmount({amount: null, credited_amount: '1000'}, 'period'), 1000);
});

test('период по людям: без имени — в конце', () => {
  const rows = [
    {order_number: '1', guest: 'Иванов', event_day: '2026-10-12', amount: '1000'},
    {order_number: '2', guest: '', event_day: '2026-10-09', amount: '500'},
    {order_number: '3', guest: 'иванов ', event_day: null, amount: '250'},
    {order_number: '4', guest: 'Ахмедов', event_day: '2026-10-09', amount: '100'}];
  const people = logic.prepaymentGroups(rows);
  assert.deepEqual(people.map(group => [group.title, group.total]), [['Ахмедов', 100], ['Иванов', 1250], ['', 500]]);
});

const click = 'Click/Payme Безналичный перевод';
const receiptDay = () => ({date: '2026-10-08', revenue: '50147800', daily_revenue: '50647800',
  received_prepayments_total: '500000', redeemed_total: '3400000',
  payment_groups: [
    {name: 'Наличные', types: [{name: 'Демо', paid: '29621000', redeemed: '3400000'},
      {name: 'Наличные (Инкасса QR)', paid: '1300000'}]},
    {name: 'Банковские карты', types: [{name: click, paid: '1010500'},
      {name: 'UzCard', paid: '12298500'}, {name: 'Xumo', paid: '2804000'},
      {name: 'Я Rahmat', paid: '1325800'}, {name: 'Яндекс Еда', paid: '1788000'}]}],
  prepayments: [
    {id: 'a', order_number: '71034', amount: '300000', received_at: '2026-10-08T17:37:00+05:00'},
    {id: 'b', order_number: '71067', amount: '200000', received_at: '2026-10-08T19:29:00+05:00'}]});
const receiptRegistry = () => ({date: '2026-10-08', received: receiptDay().prepayments.map(entry=>({
  order_number: entry.order_number, received_day: '2026-10-08', amount: entry.amount,
  credited_methods: [click], credited_amount: entry.amount, method: '', guest: ''}))});
const clickType = view => view.groups.flatMap(group=>group.types).find(item=>item.name===click);

test('8 октября: Click/Payme 1 010 500 + две предоплаты = 1 510 500, общий итог прежний', () => {
  const snapshot = receiptDay(), registry = receiptRegistry();
  const original = JSON.stringify({snapshot, registry});
  const view = logic.revenueGroups(snapshot, registry), type = clickType(view);
  assert.deepEqual([type.paid, type.received, type.total], [1010500, 500000, 1510500]);
  assert.deepEqual(type.advances.map(item=>item.amount), [300000, 200000]);
  assert.equal(view.groups[1].total, 19726800);
  assert.equal(view.unassignedTotal, 0);
  assert.equal(view.total, 50647800);
  assert.equal(view.realCash, 50147800);
  assert.equal(view.match, true);
  assert.equal(JSON.stringify({snapshot, registry}), original);
});

test('поздний ответ реестра меняет только разбивку; чужой день не используется', () => {
  const pending = logic.revenueGroups(receiptDay());
  assert.equal(clickType(pending).total, 1010500);
  assert.equal(pending.unassignedTotal, 500000);
  const old = receiptRegistry(); old.date = '2026-10-07';
  assert.equal(clickType(logic.revenueGroups(receiptDay(), old)).total, 1010500);
  const failed = {...receiptRegistry(), failed:true};
  assert.equal(logic.revenueGroups(receiptDay(), failed).unassignedTotal, 500000);
});

test('смешанный, частичный и несовпадающий зачёт не назначает способ входящей предоплате', () => {
  for (const patch of [{credited_methods:[click,'Демо']}, {credited_amount:'100000'},
    {amount:'999999'}, {received_day:'2026-10-07'}]) {
    const registry = receiptRegistry(); Object.assign(registry.received[0], patch);
    const view = logic.revenueGroups(receiptDay(), registry);
    assert.equal(clickType(view).received, 200000);
    assert.equal(view.unassignedTotal, 300000);
    assert.equal(view.match, true);
  }
});

test('отдельные способы внесений одного заказа сохраняются; пометка реестра не заменяет iiko', () => {
  const snapshot = receiptDay(), registry = receiptRegistry();
  snapshot.prepayments[0].payment_method = 'Наличные';
  snapshot.prepayments[1].order_number = '71034';
  snapshot.prepayments[1].payment_method = click;
  const view = logic.revenueGroups(snapshot, registry);
  assert.equal(view.groups[0].types[0].received, 300000);
  assert.equal(clickType(view).received, 200000);
  assert.equal(view.match, true);
});

test('неуказанный способ остаётся видимым; ручное уточнение распределяет только соответствующий аванс', () => {
  const registry = receiptRegistry();
  registry.received[0].credited_methods = [];
  registry.received[0].method = 'Наличные';
  registry.received[1].credited_methods = [];
  registry.received[1].method = 'непонятная карта';
  const view = logic.revenueGroups(receiptDay(), registry);
  assert.equal(view.groups[0].types[0].received, 300000);
  assert.equal(view.unassignedTotal, 200000);
  assert.equal(view.unassigned[0].order_number, '71067');
  assert.equal(view.match, true);
});

test('справочник и текущая группа не удваивают аванс одного способа', () => {
  const snapshot = receiptDay();
  snapshot.payment_groups.unshift({name:'Старая группа',types:[{name:click,paid:'0'}]});
  const view = logic.revenueGroups(snapshot, receiptRegistry());
  assert.equal(view.groups[0].total, 0);
  assert.equal(view.groups[2].total, 19726800);
  assert.equal(view.match, true);
});

test('неполный список внесений не перераспределяет общий итог по способам', () => {
  const snapshot = receiptDay(); snapshot.prepayments.pop();
  const view = logic.revenueGroups(snapshot, receiptRegistry());
  assert.equal(view.unassignedTotal, 500000);
  assert.equal(clickType(view).received, 0);
  assert.equal(view.match, true);
});

test('зачёт в другой день не возвращает старый аванс в выручку или строку способа оплаты', () => {
  const snapshot = {...receiptDay(), date:'2026-10-11', prepayments:[], received_prepayments_total:'0',
    daily_revenue:'50147800', redeemed_total:'500000'};
  const view = logic.revenueGroups(snapshot, receiptRegistry());
  assert.equal(clickType(view).total, 1010500);
  assert.equal(view.unassignedTotal, 0);
  assert.equal(view.total, 50147800);
  assert.equal(view.match, true);
});

test('предоплата без оплаты счёта создаёт видимую строку своего способа', () => {
  const snapshot = {...receiptDay(), revenue:'0', daily_revenue:'500000', payment_groups:[],
    payments:[{name:click, amount:'0'}]};
  const view = logic.revenueGroups(snapshot, receiptRegistry());
  assert.equal(clickType(view).total, 500000);
  assert.equal(clickType(view).zero, false);
  assert.equal(view.groups[0].shown.length, 1);
  assert.equal(view.match, true);
});

test('получено несколькими взносами: подпись заказа не повторяет всю сумму на каждом взносе', () => {
  const snapshot = receiptDay(), registry = receiptRegistry();
  snapshot.prepayments[1].order_number = '71034';
  registry.received = [{...registry.received[0],amount:'500000',credited_amount:'500000'}];
  const view = logic.revenueGroups(snapshot, registry);
  assert.equal(clickType(view).received, 500000);
  assert.deepEqual(clickType(view).advances.map(item=>item.amount), [300000,200000]);
  assert.equal(view.match, true);
});

test('T-508: средний чек — на полный счёт, той же базы, что «Чеков», а не выручка ÷ чеки', () => {
  const view = logic.revenueGroups(day());
  assert.equal(view.average, 628742.57);
  assert.notEqual(Math.round(view.average), Math.round(view.total / view.receipts));
});
