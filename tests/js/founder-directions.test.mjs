import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

const source=readFileSync(new URL('../../retro/static/founder.js',import.meta.url),'utf8');
const renderSource=source.slice(source.indexOf('function render(data){'),source.indexOf('async function load('));
const clearSource=source.slice(source.indexOf('function clearResults(){'),source.indexOf('function invalidatePending(){'));

test('selecting Retro hides only unselected cards and leaves all direction controls usable',()=>{
  const node=()=>({hidden:false,textContent:'',classList:{toggle(){},remove(){}},replaceChildren(){}});
  const controls=Object.fromEntries(['retro','school','banquet'].map(key=>[key,node()]));
  const cards=Object.fromEntries(['retro','school','banquet'].map(key=>[key,node()]));
  const ids=new Map();
  const context=vm.createContext({
    Intl, Number, Date, Math, lastAnalytics:null,lastBookings:null,
    money:new Intl.NumberFormat('ru-RU'),exactMoney:new Intl.NumberFormat('ru-RU'),
    $:id=>{if(!ids.has(id))ids.set(id,node());return ids.get(id)},
    document:{querySelector(selector){const key=selector.match(/data-direction=(\w+)/)[1];return selector.startsWith('article')?cards[key]:controls[key]}},
    renderSalesBridge(){},renderRevenue(){},renderPayments(){},renderSeriesTable(){},setMessage(){},
    data:{pnl:{},dashboard_expenses:{},internal_costs:{},totals:{retro:'42463500',school:'3804000',banquet:'0',selected:'42463500'},
      directions:['retro'],updated_at:'2026-09-24T18:00:00+05:00',reconciled:true,discrepancy:'0',warnings:[]},
  });
  vm.runInContext(renderSource+clearSource+';render(data);',context);
  assert.equal(cards.retro.hidden,false);
  assert.equal(cards.school.hidden,true);
  assert.equal(cards.banquet.hidden,true);
  assert.ok(Object.values(controls).every(control=>!control.hidden));
  vm.runInContext('clearResults()',context);
  assert.ok(Object.values(controls).every(control=>!control.hidden));
  assert.ok(Object.values(cards).every(card=>!card.hidden));
});
