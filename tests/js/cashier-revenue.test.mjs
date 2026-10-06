import test from 'node:test';
import assert from 'node:assert/strict';
import logic from '../../retro/static/cashier-logic.js';

const day = extra => ({
  payments: [{name:'Демо',amount:'1500000'}, {name:'Click/Payme Безналичный перевод',amount:'500000'}],
  new_prepayment:'1000000',cash_prepayment:'200000',prepayment_issue:null,
  ...extra,
});

test('revenue includes advances once, separately from closed sales and the detailed registry', () => {
  const source=day({prepayments:[{amount:'200000'},{amount:'800000'}]});
  const view=logic.revenueView(source);
  assert.equal(view.salesTotal,2000000);
  assert.equal(view.prepaymentTotal,1000000);
  assert.equal(view.total,3000000);
  assert.deepEqual(view.rows.slice(-2),[
    {name:'Предоплаты наличными',amount:200000,kind:'prepayment'},
    {name:'Предоплаты картой / безналом',amount:800000,kind:'prepayment'},
  ]);
  assert.deepEqual(source.payments[0],{name:'Демо',amount:'1500000'});
  const composition=logic.composition(view.rows,['#1','#2']);
  assert.equal(composition.reduce((total,row)=>total+row.value,0),view.total);
  assert.ok(Math.abs(composition.reduce((total,row)=>total+row.share,0)-1)<0.00001);
});

test('card-only advances do not increase the cash sale row', () => {
  const view=logic.revenueView(day({cash_prepayment:'0'}));
  assert.equal(view.rows[0].amount,1500000);
  assert.equal(view.rows.at(-2).amount,0);
  assert.equal(view.rows.at(-1).amount,1000000);
});

test('unknown advances keep closed sales visible without reporting a fake zero or total', () => {
  for(const extra of [{new_prepayment:null,cash_prepayment:null},{prepayment_issue:'Недоступно'},{new_prepayment:undefined}]) {
    const view=logic.revenueView(day(extra));
    assert.equal(view.salesTotal,2000000);
    assert.equal(view.prepaymentTotal,null);
    assert.equal(view.total,null);
    assert.ok(view.rows.slice(-2).every(row=>row.amount===null));
  }
});

test('an unknown or inconsistent cash split never produces negative card advances', () => {
  for(const cash of [null,undefined,'1200000','wrong']) {
    const view=logic.revenueView(day({cash_prepayment:cash}));
    assert.equal(view.prepaymentTotal,1000000);
    assert.equal(view.total,3000000);
    assert.ok(view.rows.slice(-2).every(row=>row.amount===null));
  }
});

test('no advances and an empty sale day are known zero values', () => {
  const view=logic.revenueView(day({payments:[],new_prepayment:'0',cash_prepayment:'0'}));
  assert.equal(view.total,0);
  assert.equal(view.salesTotal,0);
  assert.equal(view.prepaymentTotal,0);
  assert.equal(logic.revenueView(null).total,null);
});

test('amounts retain currency precision when combining sales and advances', () => {
  const view=logic.revenueView(day({payments:[{name:'Демо',amount:'0.1'}],new_prepayment:'0.2',cash_prepayment:'0.1'}));
  assert.equal(view.total,0.3);
  assert.equal(view.rows.at(-1).amount,0.1);
});
