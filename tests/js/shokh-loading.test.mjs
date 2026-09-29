import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setImmediate} from 'node:timers/promises';
import test from 'node:test';
import vm from 'node:vm';
const source = readFileSync(new URL('../../retro/static/shokh.js', import.meta.url), 'utf8');
const homeCode = source.slice(source.indexOf('async function loadHome()'), source.indexOf('/* ── Флоу'));
const loadCode = source.slice(source.indexOf('async function loadCatalog()'), source.indexOf("$('reload-data').addEventListener"));
function harness(saved = {}) {
  const elements = new Map(), pending = [], storage = new Map(Object.entries(saved)), messages = [];
  const state = {catalogReady:false,home:null};
  const context = vm.createContext({state,
    $: id=>{if (!elements.has(id)) elements.set(id,{});return elements.get(id);},
    sessionStorage:{getItem:key=>storage.get(key),removeItem:key=>storage.delete(key)},
    api:path=>new Promise((resolve,reject)=>pending.push({path,resolve,reject})),
    renderHome:data=>{state.home=data;},message:text=>messages.push(text),
    renderSelectors(){},renderPoints(){},renderStep(){},show(){},startTimer(){},
  });
  vm.runInContext(homeCode + loadCode,context);
  return {state,pending,storage,messages, elements,
    run:()=>vm.runInContext('reloadData()',context),
    recover:()=>vm.runInContext('recoverPending()',context)};
}
test('catalog failure leaves the home balances visible and disables a new purchase',async()=>{
  const h=harness(),done=h.run(); await setImmediate();
  assert.deepEqual(h.pending.map(x=>x.path),['/home','/catalog']);
  h.pending[0].resolve({pocket:'500000'}); await setImmediate();
  assert.equal(h.state.home.pocket,'500000');
  h.pending[1].reject(new Error('iiko offline')); await done;
  assert.equal(h.state.home.pocket,'500000');
  assert.equal(h.elements.get('start-purchase').disabled,true);
  assert.equal(h.elements.get('iiko-status').textContent,'iiko offline');
});
test('page reload retains the original operation key while its first request may still be running',async()=>{
  const saved={draft:{operationId:'original',hasPhoto:false},tripId:7,date:'2026-09-28'};
  const h=harness({'shokh-pending-operation':'original','shokh-pending-draft':JSON.stringify(saved)});
  const done=h.recover();h.pending[0].resolve({purchase:null});await done;
  assert.equal(h.storage.get('shokh-pending-operation'),'original');
  assert.equal(h.state.recovery.draft.operationId,'original');
});
test('a confirmed prior reservation is recovered from the server after a lost response',async()=>{
  const h=harness({'shokh-pending-operation':'original','shokh-pending-draft':'{}'});
  const done=h.recover();h.pending[0].resolve({purchase:{id:4}});await done;
  assert.equal(h.storage.has('shokh-pending-operation'),false);
  assert.match(h.messages[0],/найдена в журнале/);
});
