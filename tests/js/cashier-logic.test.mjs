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

/* «К передаче» и «Касса за день» экран не считает (Функционал §1): формула
   одна, на сервере (till_summary). Во фронтенде её копии быть не должно. */
test('формулы передачи во фронтенде нет — числа приходят с сервера', () => {
  assert.equal(logic.handover, undefined);
  assert.equal(logic.totalInflow, undefined);
  assert.equal(logic.cashPayment, undefined);
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
});

test('пустая смена не ломает состав', () => {
  assert.deepEqual(logic.composition([], PALETTE), []);
  assert.deepEqual(logic.composition(undefined, PALETTE), []);
});

/* ── 5a: смена, передача, суммы в полях ─────────────────────────────── */
test('смена: открыта, закрыта со временем, после полуночи — с датой, без данных — ничего', () => {
  assert.deepEqual(logic.shiftLabel({open: true}, '2026-09-28'), {open: true, text: 'Смена открыта'});
  assert.deepEqual(logic.shiftLabel({open: false, closed_at: '2026-09-28T22:56:49'}, '2026-09-28'),
    {open: false, text: 'Смена закрыта 22:56'});
  assert.equal(logic.shiftLabel({open: false, closed_at: '2026-09-29T00:40:00'}, '2026-09-28').text,
    'Смена закрыта 29.09 00:40');
  assert.equal(logic.shiftLabel(null, '2026-09-28'), null);
  assert.equal(logic.shiftLabel({open: false, closed_at: null}, '2026-09-28'), null);
});

test('передача: нет, совпала, разошлась, записал бухгалтер', () => {
  assert.equal(logic.handoverView(null, 850000).state, 'none');
  assert.equal(logic.handoverView({amount: '850000', source: 'cashier'}, 850000).state, 'done');
  assert.deepEqual(logic.handoverView({amount: '850000', source: 'cashier'}, 830000),
    {state: 'diff', difference: -20000});
  assert.deepEqual(logic.handoverView({amount: '850000', source: 'auto'}, 900000.5),
    {state: 'diff', difference: 50000.5});
  // Приход бухгалтера кнопка кассира не трогает, даже если сумма другая.
  assert.equal(logic.handoverView({amount: '700000', source: 'accountant'}, 850000).state, 'accountant');
  // Пока расходы не загрузились — отметка есть, разницы нет.
  assert.deepEqual(logic.handoverView({amount: '850000', source: 'cashier'}, null), {state: 'done', difference: null});
});

/* «Отменить» доступна, пока бухгалтер не подтвердил сумму (Функционал 5a). */
test('передача подтверждена бухгалтером: отменить и переписать нельзя, недостача видна', () => {
  const confirmed = {amount: '800000', source: 'cashier', confirmed_at: '2026-09-29T21:40:00+05:00',
    expected_amount: '850000', shortfall: '50000'};
  assert.deepEqual(logic.handoverView(confirmed, 850000), {state: 'confirmed', difference: null, shortfall: 50000});
  // Кассир добавил расход после подтверждения — разница от расчёта, а не от полученного.
  assert.deepEqual(logic.handoverView(confirmed, 840000), {state: 'confirmed', difference: -10000, shortfall: 50000});
  // Подтверждение сильнее источника записи.
  assert.equal(logic.handoverView({...confirmed, source: 'accountant'}, 850000).state, 'confirmed');
  assert.equal(logic.handoverView({amount: null, source: null}, 850000).state, 'none');
});

test('сумма из поля: пробелы и запятая допустимы, мусор и ноль — нет', () => {
  assert.equal(logic.parseAmount('1 500 000'), 1500000);
  assert.equal(logic.parseAmount('120,5'), 120.5);
  assert.equal(logic.parseAmount('0'), null);
  assert.equal(logic.parseAmount('-5'), null);
  assert.equal(logic.parseAmount('1.234'), null);
  assert.equal(logic.parseAmount('сто'), null);
  assert.equal(logic.parseAmount(''), null);
});

test('skeletonMap: a figure stays a skeleton until every part it is computed from has arrived', () => {
  const all = logic.skeletonMap(new Set(logic.PARTS));
  assert.ok(Object.values(all).every(Boolean));
  const none = logic.skeletonMap([]);
  assert.ok(Object.values(none).every(value => value === false));
  // «К передаче» — из iiko, расходов, поступлений и выдач Шоху.
  const onlyShokh = logic.skeletonMap(['shokh']);
  assert.equal(onlyShokh.handover, true);
  assert.equal(onlyShokh['expense-total'], true);
  assert.equal(onlyShokh.revenue, false);
  assert.equal(onlyShokh['receipt-total'], false);
  const onlyRate = logic.skeletonMap(new Set(['rate']));
  assert.deepEqual(Object.keys(onlyRate).filter(id => onlyRate[id]).sort(), ['usd-official', 'usd-restaurant']);
});
