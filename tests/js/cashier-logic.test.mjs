import test from 'node:test';
import assert from 'node:assert/strict';
// UMD-модуль: из node приходит CJS-экспортом, в браузере ложится в globalThis.
import logic from '../../retro/static/cashier-logic.js';

const snapshot = (extra = {}) => ({
  revenue: '18000000', new_prepayment: '0', cash_prepayment: '500000',
  register_received_total: null,
  payments: [
    {name: 'Демо', amount: '4000000'},
    {name: 'Карта', amount: '12000000'},
    {name: 'Наличные (Инкасса QR)', amount: '2000000'},
  ],
  ...extra,
});

/* Формула повторяет серверную (cash_to_finance): наличные из iiko плюс
   предоплаты наличными плюс прочие поступления минус расходы. */
test('к передаче: наличные + предоплаты + поступления − расходы', () => {
  assert.equal(logic.handover(snapshot(), '300000', '100000'),
    4000000 + 500000 + 100000 - 300000);
});

test('передача уходит в минус, когда расходов больше наличных', () => {
  assert.equal(logic.handover(snapshot(), '9000000', '0'), 4000000 + 500000 - 9000000);
});

/* Неполная сумма на экране хуже прочерка: пока расходы или поступления не
   загрузились, считать нечего. */
test('без расходов, поступлений или смены сумма не выдумывается', () => {
  assert.equal(logic.handover(snapshot(), null, '0'), null);
  assert.equal(logic.handover(snapshot(), '0', null), null);
  assert.equal(logic.handover(null, '0', '0'), null);
});

test('смена без наличной оплаты считается нулём, а не пропуском', () => {
  const noCash = snapshot({payments: [{name: 'Карта', amount: '12000000'}]});
  assert.equal(logic.cashPayment(noCash), 0);
  assert.equal(logic.handover(noCash, '0', '0'), 500000);
});

test('весь приход берётся из регистра, если он пришёл', () => {
  // Регистр знает полную кассу смены, включая погашенные авансы.
  assert.equal(logic.totalInflow(snapshot({register_received_total: '19000000'}), '250000'),
    19000000 + 250000);
  // Без регистра — продажи плюс новые предоплаты.
  assert.equal(logic.totalInflow(snapshot({new_prepayment: '700000'}), '250000'),
    18000000 + 700000 + 250000);
  assert.equal(logic.totalInflow(null, '0'), null);
  assert.equal(logic.totalInflow(snapshot(), null), null);
});

const PALETTE = ['#a', '#b', '#c'];

test('состав оплат: доли от суммы способов, нулевые строки не рисуются', () => {
  const rows = logic.composition([
    {name: 'Демо', amount: '4000000'},
    {name: 'Карта', amount: '12000000'},
    {name: 'Пустой способ', amount: '0'},
  ], PALETTE);
  assert.deepEqual(rows.map(r => r.name), ['Демо', 'Карта']);
  assert.equal(rows[0].percent, 25);
  assert.equal(rows[1].percent, 75);
  assert.equal(Math.round((rows[0].share + rows[1].share) * 100) / 100, 1);
  assert.deepEqual(rows.map(r => r.color), ['#a', '#b']);
});

/* «Наличные (Инкасса QR)» приходят на счёт, а не в ящик, поэтому их нельзя
   мешать с наличными к передаче. */
test('инкассовый QR отличается от наличных к передаче', () => {
  const rows = logic.composition(snapshot().payments, PALETTE);
  const cash = rows.find(r => r.isCash);
  const collection = rows.find(r => r.goesToSafe);
  assert.equal(cash.name, 'Демо');
  assert.equal(collection.name, 'Наличные (Инкасса QR)');
  assert.equal(collection.isCash, false);
  // В сумму передачи инкасса не входит: там только «Демо».
  assert.equal(logic.handover(snapshot(), '0', '0'), 4000000 + 500000);
});

test('пустая смена не ломает состав', () => {
  assert.deepEqual(logic.composition([], PALETTE), []);
  assert.deepEqual(logic.composition(undefined, PALETTE), []);
});
