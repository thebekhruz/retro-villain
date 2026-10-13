import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setImmediate} from 'node:timers/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../retro/static/app.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('function load(options'), source.indexOf('function waitForRefresh('));
const registryCode = source.slice(source.indexOf('async function loadPrepay('), source.indexOf('function loadPrepayPeriod('));
const disclosureCode = source.slice(source.indexOf('const prepaySection ='), source.indexOf('\nfunction show(data)'));
const TODAY = '2026-09-28';
function harness(saved = null) {
  const pending = [], rendered = [], messages = [], waits = [], elements = new Map();
  const buttons = [], dims = [], expects = [], arrivals = [];
  let clears = 0;
  const context = vm.createContext({
    generation:0, controller:null, snapshot:saved, config:{today:TODAY, configured:true}, demo:false,
    prepayRequest:null, prepayData:null, prepayLoadedGeneration:-1, prepayReadyGeneration:-1,
    localStorage:{getItem:()=> 'true'}, // Старое сохранённое раскрытие больше не запускает загрузку.
    AbortController,
    $: id => {
      if (!elements.has(id)) elements.set(id, {id, value:TODAY, checkValidity:()=>true, events:{},
        addEventListener(name,fn){this.events[name]=fn;}, replaceChildren(){}, scrollIntoView(){},
        setAttribute(){}, classList:{toggle(){}, remove(){}, add(){}}});
      return elements.get(id);
    },
    document:{body:{classList:{remove(){},add(){}}}},
    clearSnapshot(){ clears++; context.snapshot = null; context.prepayData = null; }, clearFinance(){}, clearUsdRate(){},
    message:text=>messages.push(text), formattedDay:day=>day, shortDay:day=>day, previousDay:()=> '2026-09-27',
    loadExpenses(){}, loadReceipts(){}, loadUsdRate(){}, loadShokh(){}, loadUsd(){},
    RetroState:{responseJson:response=>response.json()},
    paintWaiting(){}, refreshPaymentGroups(){}, showPrepay(){},
    request:(url, signal, options={})=>new Promise((resolve,reject)=>pending.push({url,signal,options,resolve,reject})),
    // busy.js (T-393): кто крутится, что гаснет, чего ждём скелетом.
    Busy:{button:(el, work, opts)=>{buttons.push({id:el.id, opts}); return work;}},
    CashierLogic:{PARTS:['day','expenses','receipts','shokh','usd','rate']},
    waiting:new Set(),
    expect(parts){ expects.push([...parts]); context.waiting = new Set(parts); },
    arrive(part){ arrivals.push(part); context.waiting.delete(part); },
    dim(part, work){ dims.push(part); return work; },
    loadPart(part, current, work){ return work; },
    show:data=>{context.snapshot=data; rendered.push(data);},
    waitForRefresh:signal=>new Promise((resolve,reject)=>waits.push({signal,resolve,reject})),
  });
  vm.runInContext(registryCode + disclosureCode + code, context);
  return {context,pending,rendered,messages,waits,buttons,dims,expects,arrivals, clears:()=>clears,
    run:(options='')=>vm.runInContext(`load(${options})`,context),
    respond:(n,value)=>pending[n].resolve({json:async()=>value}),
    day:value=>context.$('report-date').value=value,
    open(value=true){const el=context.$('prepay-section');el.open=value;el.events.toggle();},
    link(){context.$('prepay-open').events.click({preventDefault(){}});},
    registryRequests:()=>pending.filter(item=>item.url.includes('/prepayments?'))};
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

test('first load of a day shows skeletons instead of «Загружаем…» and spins the refresh button', async()=>{
  const h=harness(), done=h.run();
  assert.deepEqual(h.expects.at(-1), ['day','expenses','receipts','shokh','usd','rate']);
  assert.deepEqual(h.dims, [], 'a new day is not dimmed — the old figures belong to another day');
  assert.equal(h.buttons[0].id, 'refresh');
  assert.equal(h.buttons[0].opts.done, false);
  assert.ok(!h.messages.some(text => /Загружаем|Обновляем/.test(text)), 'no loading banner');
  h.respond(0,{date:TODAY,revenue:'100'});
  assert.equal(await done, true);
  assert.ok(h.arrivals.includes('day'));
});

test('manual refresh of the same day dims the sections and ends with ✓', async()=>{
  const h=harness({date:TODAY,revenue:'100'}), done=h.run('{refresh:true}');
  assert.deepEqual(h.expects, []);
  assert.deepEqual(h.dims, ['day']);
  assert.equal(h.buttons[0].opts.done, true);
  assert.ok(h.pending[0].url.includes('refresh=true'));
  h.respond(0,{date:TODAY,revenue:'200'});
  assert.equal(await done, true);
});

test('background iiko polling is silent: no top progress bar on every poll', async()=>{
  const h=harness(), done=h.run();
  h.respond(0,{date:TODAY,revenue:'100',stale:true,refreshing:true});
  await setImmediate();
  assert.notEqual(h.pending[0].options.retroBusy, false, 'the first request is a real wait and shows the bar');
  h.waits[0].resolve();
  await setImmediate();
  assert.equal(h.pending[1].options.retroBusy, false);
  h.respond(1,{date:TODAY,revenue:'200',refreshing:false});
  await done;
});

test('a failed load resolves false so the refresh button shows no ✓', async()=>{
  const h=harness({date:TODAY,revenue:'100'}), done=h.run('{refresh:true}');
  h.pending[0].reject(new Error('synthetic outage'));
  assert.equal(await done, false);
});

const registry = day => ({date:day, received:[], credited:[], received_total:'0', credited_total:'0'});

test('closed registry stays lazy on entry, day refresh and date change despite saved disclosure', async()=>{
  const h=harness();
  assert.equal(h.context.$('prepay-section').open,false);
  for (const day of [TODAY,TODAY,'2026-09-27']) {
    h.day(day); const done=h.run('{refresh:true}');
    h.respond(h.pending.length-1,{date:day,revenue:'100'}); await done;
    assert.equal(h.registryRequests().length,0);
    assert.equal(h.context.waiting.has('prepay'),false);
  }
});

test('opening before the day arrives shows waiting, then makes one registry request', async()=>{
  const h=harness(),done=h.run();
  h.open();
  assert.equal(h.context.waiting.has('prepay'),true);
  assert.equal(h.registryRequests().length,0);
  h.respond(0,{date:TODAY,revenue:'100'}); await done;
  assert.equal(h.registryRequests().length,1);
  h.open(false); h.open(); h.link(); h.open();
  assert.equal(h.registryRequests().length,1,'toggle and card link share the pending request');
  h.respond(1,registry(TODAY)); await setImmediate();
  assert.equal(h.context.waiting.has('prepay'),false);
  h.open(false); h.open();
  assert.equal(h.registryRequests().length,1,'same-day reopening uses the loaded result');
});

test('registry error can retry on reopening without reloading the day', async()=>{
  const h=harness(),done=h.run();
  h.respond(0,{date:TODAY,revenue:'100'}); await done; h.link();
  h.pending[1].reject(new Error('registry unavailable')); await setImmediate();
  assert.equal(h.context.prepayData.failed,true);
  assert.equal(h.context.waiting.has('prepay'),false);
  h.open(false); h.open();
  assert.equal(h.registryRequests().length,2);
  h.respond(2,registry(TODAY)); await setImmediate();
  assert.equal(h.context.prepayData.failed,undefined);
});

test('changing day cancels registry work and ignores a late response', async()=>{
  const h=harness(),first=h.run();
  h.respond(0,{date:TODAY,revenue:'100'}); await first; h.open();
  h.day('2026-09-27');const next=h.run();
  assert.equal(h.pending[1].signal.aborted,true);
  h.respond(2,{date:'2026-09-27',revenue:'200'}); await next;
  h.respond(3,registry('2026-09-27')); await setImmediate();
  h.respond(1,registry(TODAY)); await setImmediate();
  assert.equal(h.context.prepayData.date,'2026-09-27');
});

test('background day update refreshes an open registry without accepting the older response', async()=>{
  const h=harness(),done=h.run(); h.open();
  h.respond(0,{date:TODAY,revenue:'100',refreshing:true}); await setImmediate();
  h.waits[0].resolve(); await setImmediate();
  h.respond(2,{date:TODAY,revenue:'200',refreshing:false}); await done;
  assert.equal(h.pending[1].signal.aborted,true);
  h.respond(3,{...registry(TODAY),marker:'fresh'}); await setImmediate();
  h.respond(1,{...registry(TODAY),marker:'old'}); await setImmediate();
  assert.equal(h.context.prepayData.marker,'fresh');
  assert.equal(h.context.waiting.has('prepay'),false);
});

test('closing during day polling prevents another registry fetch until reopened', async()=>{
  const h=harness(),done=h.run(); h.open();
  h.respond(0,{date:TODAY,revenue:'100',refreshing:true}); await setImmediate();
  h.respond(1,registry(TODAY)); await setImmediate(); h.open(false);
  h.waits[0].resolve(); await setImmediate();
  h.respond(2,{date:TODAY,revenue:'200',refreshing:false}); await done;
  assert.equal(h.registryRequests().length,1);
  h.open(); assert.equal(h.registryRequests().length,2);
  h.respond(3,registry(TODAY)); await setImmediate();
});

test('background update invalidates a still-pending registry even after it was collapsed', async()=>{
  const h=harness(),done=h.run(); h.open();
  h.respond(0,{date:TODAY,revenue:'100',refreshing:true}); await setImmediate(); h.open(false);
  h.waits[0].resolve(); await setImmediate();
  h.respond(2,{date:TODAY,revenue:'200',refreshing:false}); await done;
  assert.equal(h.pending[1].signal.aborted,true);
  h.respond(1,{...registry(TODAY),marker:'old'}); await setImmediate();
  assert.equal(h.context.prepayData,null);
  h.open(); assert.equal(h.registryRequests().length,2);
  h.respond(3,{...registry(TODAY),marker:'fresh'}); await setImmediate();
  assert.equal(h.context.prepayData.marker,'fresh');
});
