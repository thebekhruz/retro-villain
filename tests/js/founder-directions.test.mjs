import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import FounderLogic from '../../retro/static/founder-logic.js';

const source=readFileSync(new URL('../../retro/static/founder.js',import.meta.url),'utf8');
const renderSource=source.slice(source.indexOf('function render(data){'),source.indexOf('async function load('));

test('selecting Retro hides only unselected cards and leaves all direction controls usable',()=>{
  const node=()=>({hidden:false,textContent:'',classList:{toggle(){},remove(){}},replaceChildren(){}});
  const controls=Object.fromEntries(['retro','school','banquet'].map(key=>[key,node()]));
  const cards=Object.fromEntries(['retro','school','banquet'].map(key=>[key,node()]));
  const ids=new Map();
  const context=vm.createContext({
    Intl, Number, Date, Math, FounderLogic, lastAnalytics:null,lastBookings:null,
    money:new Intl.NumberFormat('ru-RU'),exactMoney:new Intl.NumberFormat('ru-RU'),
    $:id=>{if(!ids.has(id))ids.set(id,node());return ids.get(id)},
    document:{querySelector(selector){const key=selector.match(/data-direction=(\w+)/)[1];return selector.startsWith('article')?cards[key]:controls[key]}},
    renderSalesBridge(){},renderRevenue(){},renderPayments(){},renderSeriesTable(){},renderScopeNotes(){},setMessage(){},
    data:{pnl:{},dashboard_expenses:{},internal_costs:{},totals:{retro:'42463500',school:'3804000',banquet:'0',selected:'42463500'},
      yandex_totals:{retro:'6800000',school:'0',banquet:'0'},
      directions:['retro'],updated_at:'2026-09-24T18:00:00+05:00',reconciled:true,discrepancy:'0',warnings:[]},
  });
  vm.runInContext(renderSource+';render(data);',context);
  assert.equal(cards.retro.hidden,false);
  assert.equal(cards.school.hidden,true);
  assert.equal(cards.banquet.hidden,true);
  assert.ok(Object.values(controls).every(control=>!control.hidden));
  // ТЗ 5.2: «в т.ч. Яндекс Еда» — из ответа iiko, «Всего» не меняется.
  // Время — из ответа сервера по Ташкенту, а не пояс браузера (T-506).
  assert.equal(ids.get('updated').textContent,'Обновлено 24.09, 18:00');
  assert.equal(ids.get('total-retro-yandex').textContent,new Intl.NumberFormat('ru-RU').format(6800000));
  assert.equal(ids.get('total-selected').textContent,new Intl.NumberFormat('ru-RU').format(42463500));
});

test('changing a filter keeps the previous numbers and marks them stale instead of wiping the page',()=>{
  const start=source.indexOf('function setStale(on){'),end=source.indexOf('/** Загрузить выбранное');
  const invalidate=source.slice(source.indexOf('function invalidatePending(event){'),source.indexOf("['start','end','granularity']"));
  const classes=new Set(),nodes=new Map();
  const node=()=>({hidden:true,textContent:'',classList:{remove(){}}});
  const context=vm.createContext({
    lastAnalytics:{totals:{selected:'1'}},lastBookings:null,lastGuests:null,controller:null,guestController:null,activePeriod:'30',
    gate:{invalidate(){}},guestGate:{invalidate(){}},setLoading(){},settleSkeletons(){},
    $:id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)},
    document:{querySelector:()=>({classList:{toggle:(name,on)=>on?classes.add(name):classes.delete(name)}}),
      querySelectorAll:()=>[]},
  });
  vm.runInContext(source.slice(start,end)+invalidate+";invalidatePending({target:{type:'date'}})",context);
  assert.ok(classes.has('is-stale'));
  assert.equal(nodes.get('stale').hidden,false);
  assert.match(nodes.get('stale').textContent,/прежняя выборка/);
  assert.equal(vm.runInContext('activePeriod',context),null);
  vm.runInContext('setStale(false)',context);
  assert.ok(!classes.has('is-stale'));
  assert.equal(nodes.get('stale').hidden,true);
});
