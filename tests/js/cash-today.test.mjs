import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

/* «Баланс на сегодня» (T-470): одна карточка на двух зарплатных страницах,
   сумма — из /api/accountant/cash-today («Финансы дня»). */
const source=readFileSync(new URL('../../retro/static/cash-today.js',import.meta.url),'utf8');

function page(reply){
  const nodes={value:{textContent:''},note:{textContent:''}}, classes=new Set(), requests=[];
  const card={querySelector:selector=>selector==='[data-cash-value]'?nodes.value:nodes.note,
    classList:{toggle:(name,on)=>on?classes.add(name):classes.delete(name)}};
  const context=vm.createContext({Intl,Number,setTimeout,clearTimeout,setInterval:()=>0,
    window:{addEventListener(){}},
    document:{readyState:'complete',hidden:false,querySelector:()=>null,querySelectorAll:()=>[card],addEventListener(){}},
    fetch:url=>{requests.push(url);return reply();}});
  context.globalThis=context;
  vm.runInContext(source,context);
  return {context,nodes,classes,requests};
}
const settle=()=>new Promise(resolve=>setTimeout(resolve,0));

test('сумма, ноль и отсутствие данных',async()=>{
  let h=page(async()=>({ok:true,json:async()=>({date:'2026-10-15',cash_balance:'12345678.00'})}));await settle();
  assert.deepEqual(h.requests,['/api/accountant/cash-today']);
  assert.equal(h.nodes.value.textContent.replace(/\s/g,' '),'12 345 678 сум');
  assert.equal(h.nodes.note.textContent,'из «Финансов дня»');
  h=page(async()=>({ok:true,json:async()=>({cash_balance:'0'})}));await settle();
  assert.equal(h.nodes.value.textContent,'0 сум');
  h=page(async()=>({ok:true,json:async()=>({cash_balance:null})}));await settle();
  assert.equal(h.nodes.value.textContent,'—');
  assert.equal(h.nodes.note.textContent,'Нет данных из Финансов дня');
  assert.ok(h.classes.has('is-empty'));
  h=page(async()=>{throw new Error('offline');});await settle();
  assert.equal(h.nodes.value.textContent,'—');
});

test('минус показывается со знаком и красным',async()=>{
  const h=page(async()=>({ok:true,json:async()=>({cash_balance:'-250000'})}));await settle();
  assert.equal(h.nodes.value.textContent.replace(/\s/g,' '),'−250 000 сум');
  assert.ok(h.classes.has('is-negative'));
});
