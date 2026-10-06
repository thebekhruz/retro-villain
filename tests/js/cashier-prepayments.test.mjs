import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import logic from '../../retro/static/cashier-logic.js';

const entry = (extra = {}) => ({id:'advance-1', received_at:'2026-10-06T06:57:00Z', amount:'1000000',
  payment_method:null, comment:'', order_number:'543', ...extra});

test('prepayment records preserve real amounts and unknown payment types, with Tashkent time', () => {
  const result = logic.prepaymentsView({new_prepayment:'9999999', prepayments:[entry(), entry({
    id:'advance-2', received_at:'2026-10-06T13:01:00', amount:'200000.25', comment:'Гость', payment_method:'UzCard',
  })]});
  assert.equal(result.total, 1200000.25);
  assert.deepEqual(result.rows, [
    {time:'11:57', description:'Заказ № 543', paymentMethod:'Не указан', amount:1000000},
    {time:'13:01', description:'Гость', paymentMethod:'UzCard', amount:200000.25},
  ]);
});

test('missing and failed registry are not presented as a zero-prepayment day', () => {
  for (const input of [null, {}, {prepayments:null}]) assert.equal(logic.prepaymentsView(input).rows, null);
  assert.deepEqual(logic.prepaymentsView({prepayments:[]}), {rows:[], total:0, issue:null});
  assert.equal(logic.prepaymentsView({prepayments:[], prepayments_issue:'iiko недоступен'}).total, null);
  assert.equal(logic.prepaymentsView({prepayments:[entry({amount:null})]}).total, null);
});

const source = readFileSync(new URL('../../retro/static/app.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('function showPrepayments('), source.indexOf('function show(data)'));
function harness() {
  const elements = new Map();
  function element(tag = 'div') {
    return {tag, children:[], textContent:'', hidden:false, classList:{toggle(){}},
      append(...nodes){this.children.push(...nodes)}, replaceChildren(...nodes){this.children=[...nodes]}};
  }
  const context = vm.createContext({CashierLogic:logic, money:new Intl.NumberFormat('ru-RU'),
    document:{createElement:element}, $:id=>{
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    }});
  vm.runInContext(code, context);
  return {elements, render(data){context.data=data;vm.runInContext('showPrepayments(data)',context)}};
}

test('prepayment table escapes source text and clears previous-day rows before loading another day', () => {
  const h = harness();
  h.render({prepayments:[entry({comment:'<img src=x onerror=alert(1)>'})]});
  const rows = h.elements.get('prepayments');
  assert.equal(rows.children[0].children[1].textContent, '<img src=x onerror=alert(1)>');
  assert.equal(rows.children[0].children[1].children.length, 0);
  assert.equal(h.elements.get('prepayments-state').hidden, true);
  h.render(null);
  assert.equal(rows.children.length, 0);
  assert.equal(h.elements.get('prepayments-table-wrap').hidden, true);
  assert.equal(h.elements.get('prepayments-footer').hidden, true);
  h.render({prepayments:[]});
  assert.equal(h.elements.get('prepayments-state').textContent, 'За этот день предоплат нет.');
  assert.equal(h.elements.get('prepayments-total').textContent, '0');
  h.render({prepayments:null, prepayments_issue:'Не удалось загрузить iiko'});
  assert.equal(h.elements.get('prepayments-state').textContent, 'Не удалось загрузить iiko');
  assert.equal(h.elements.get('prepayments-footer').hidden, true);
});
