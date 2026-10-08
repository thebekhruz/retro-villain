import test from 'node:test';
import assert from 'node:assert/strict';
import logic from '../../retro/static/cashier-logic.js';

/* ТЗ «Выручка, оплаты и предоплаты» (08.10.2026): день 07.10 из живого iiko. */
const day = extra => ({
  revenue: '57003000', receipt_count: 101, average_receipt: '564386.13',
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

test('кассы iiko: суммы и проценты от выручки по чекам, нулевые типы спрятаны до «показать все»', () => {
  const view = logic.revenueGroups(day());
  assert.equal(view.fallback, false);
  assert.equal(view.total, 63503000);
  assert.equal(view.receipts, 101);
  assert.deepEqual(view.groups.map(group => group.name), ['Оплата наличными', 'Банковские карты']);
  const [cash, cards] = view.groups;
  assert.equal(cash.total + cards.total, view.total);
  assert.equal(cash.percent, 45.5);
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

test('реальная касса = выручка по чекам − зачтённые предоплаты, без двойного счёта', () => {
  const view = logic.revenueGroups(day());
  assert.equal(view.redeemed, 6500000);
  assert.equal(view.realCash, 57003000);
  assert.equal(view.total - view.redeemed, view.realCash);
});

test('сверка с iiko: совпало — ✓, разошлось — разница со знаком', () => {
  assert.equal(logic.revenueGroups(day()).match, true);
  const off = logic.revenueGroups(day({groups_total: '63491000', groups_match: false}));
  assert.equal(off.match, false);
  assert.equal(off.difference, -12000);
});

test('iiko не отдал кассы: оплаты продаж одним списком, реальная касса неизвестна, а не ноль', () => {
  const view = logic.revenueGroups(day({payment_groups: null, full_total: undefined}));
  assert.equal(view.fallback, true);
  assert.equal(view.total, 57003000);
  assert.equal(view.realCash, null);
  assert.equal(view.redeemed, null);
  assert.equal(view.groups.length, 1);
  assert.equal(view.groups[0].types[0].label, 'Наличные');
  assert.equal(logic.revenueGroups(null), null);
});

test('статус предоплаты: ждёт, зачтена с датой, возврат, дата события прошла', () => {
  const today = '2026-10-08';
  assert.equal(logic.prepaymentStatus({status: 'pending', event_day: '2026-10-09'}, today).key, 'pending');
  assert.deepEqual(logic.prepaymentStatus({status: 'credited', credited_day: '2026-10-07'}, today),
    {key: 'credited', text: 'зачтена', day: '2026-10-07'});
  assert.equal(logic.prepaymentStatus({status: 'refund', credited_day: '2026-10-07'}, today).key, 'refund');
  assert.equal(logic.prepaymentStatus({status: 'pending', event_day: '2026-10-06'}, today).key, 'overdue');
  assert.equal(logic.prepaymentStatus({status: 'pending', event_day: null}, today).key, 'pending');
});

test('сумма строки: «Зачтено» — сколько зачли, остальное — сколько внесли', () => {
  const row = {amount: '50000000', credited_amount: '48000000'};
  assert.equal(logic.prepaymentAmount(row, 'received'), 50000000);
  assert.equal(logic.prepaymentAmount(row, 'credited'), 48000000);
  assert.equal(logic.prepaymentAmount({amount: null, credited_amount: '1000'}, 'period'), 1000);
});

test('период по людям и по датам события: без имени и без даты — в конце', () => {
  const rows = [
    {order_number: '1', guest: 'Иванов', event_day: '2026-10-12', amount: '1000'},
    {order_number: '2', guest: '', event_day: '2026-10-09', amount: '500'},
    {order_number: '3', guest: 'иванов ', event_day: null, amount: '250'},
    {order_number: '4', guest: 'Ахмедов', event_day: '2026-10-09', amount: '100'}];
  const people = logic.prepaymentGroups(rows, 'people');
  assert.deepEqual(people.map(group => [group.title, group.total]), [['Ахмедов', 100], ['Иванов', 1250], ['', 500]]);
  const dates = logic.prepaymentGroups(rows, 'dates');
  assert.deepEqual(dates.map(group => [group.title, group.rows.length]), [['2026-10-09', 2], ['2026-10-12', 1], ['', 1]]);
});
