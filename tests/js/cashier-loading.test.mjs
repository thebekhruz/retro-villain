import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setImmediate} from 'node:timers/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../retro/static/app.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('async function load(options'), source.indexOf('function waitForRefresh('));
const TODAY = '2026-09-28';
function harness(saved = null) {
  const pending = [], rendered = [], messages = [], waits = [], elements = new Map();
  let clears = 0;
  const context = vm.createContext({
    generation:0, controller:null, snapshot:saved, config:{today:TODAY, configured:true}, demo:false,
    AbortController,
    $: id => {
      if (!elements.has(id)) elements.set(id, {value:TODAY, checkValidity:()=>true,
        setAttribute(){}, classList:{toggle(){}}});
      return elements.get(id);
    },
    document:{body:{classList:{remove(){},add(){}}}},
    clearSnapshot(){ clears++; context.snapshot = null; }, clearFinance(){}, clearUsdRate(){},
    message:text=>messages.push(text), formattedDay:day=>day, previousDay:()=> '2026-09-27',
    loadExpenses(){}, loadReceipts(){}, loadUsdRate(){},
    request:(url, signal)=>new Promise((resolve,reject)=>pending.push({url,signal,resolve,reject})),
    show:data=>{context.snapshot=data; rendered.push(data);},
    waitForRefresh:signal=>new Promise((resolve,reject)=>waits.push({signal,resolve,reject})),
  });
  vm.runInContext(code, context);
  return {context,pending,rendered,messages,waits, clears:()=>clears,
    run:()=>vm.runInContext('load()',context),
    respond:(n,value)=>pending[n].resolve({json:async()=>value}),
    day:value=>context.$('report-date').value=value};
}

test('same-day refresh retains previous figures until the fresh response arrives', async()=>{
  const h = harness({date:TODAY, revenue:'100'}), done=h.run();
  assert.equal(h.clears(),0);
  assert.equal(h.context.snapshot.revenue,'100');
  assert.ok(h.pending[0].url.includes('allow_stale=true'));
  h.respond(0,{date:TODAY,revenue:'200'});
  await done;
  assert.equal(h.context.snapshot.revenue,'200');
});

test('failed update keeps figures but marks them stale', async()=>{
  const h=harness({date:TODAY,revenue:'100'}), done=h.run();
  h.pending[0].reject(new Error('synthetic outage'));
  await done;
  assert.equal(h.context.snapshot.revenue,'100');
  assert.equal(h.context.snapshot.stale,true);
  assert.ok(h.messages.at(-1).includes('предыдущие данные'));
});

test('stale database response is rendered before a background refresh finishes', async()=>{
  const h=harness(),done=h.run();
  h.respond(0,{date:TODAY,revenue:'100',stale:true,refreshing:true});
  await setImmediate();
  assert.equal(h.context.snapshot.revenue,'100');
  assert.equal(h.waits.length,1);
  h.waits[0].resolve();
  await setImmediate();
  assert.ok(h.pending[1].url.includes('refresh=false'));
  h.respond(1,{date:TODAY,revenue:'200',stale:false,refreshing:false});
  await done;
  assert.equal(h.context.snapshot.revenue,'200');
});

test('changing date cancels polling and a late response cannot replace the new day', async()=>{
  const h=harness(),old=h.run();
  h.respond(0,{date:TODAY,revenue:'100',stale:true,refreshing:true});
  await setImmediate();
  h.waits[0].resolve();
  await setImmediate();
  h.day('2026-09-27');
  const current=h.run();
  assert.equal(h.pending[1].signal.aborted,true);
  h.respond(2,{date:'2026-09-27',revenue:'300',refreshing:false});
  await current;
  h.respond(1,{date:TODAY,revenue:'200',refreshing:false});
  await old;
  assert.equal(h.context.snapshot.date,'2026-09-27');
  assert.equal(h.context.snapshot.revenue,'300');
});
