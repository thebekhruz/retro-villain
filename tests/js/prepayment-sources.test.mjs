import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setImmediate} from 'node:timers/promises';
import test from 'node:test';
import vm from 'node:vm';

const code = readFileSync(new URL('../../retro/static/prepayment-sources.js', import.meta.url), 'utf8');
function harness() {
  class Element {
    constructor() { this.children=[]; this.dataset={}; this.events={}; this.value=''; this.classList={toggle(){}}; }
    set textContent(value) { this.children=[String(value)]; }
    get textContent() { return this.children.map(x=>typeof x==='string'?x:x.textContent).join(''); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children=children; }
    setAttribute() {}
    addEventListener(name,handler) { this.events[name]=handler; }
    reportValidity() { return true; }
  }
  const elements = new Map(), requests = [];
  const get = id => {
    if (!elements.has(id)) elements.set(id,new Element());
    return elements.get(id);
  };
  const context = vm.createContext({document:{getElementById:get,createElement:()=>new Element()},
    location:{search:''}, URLSearchParams, AbortController, Intl, Date,
    RetroConfig:Promise.resolve({today:'2026-10-12'}),
    RetroState:{responseJson:async response=>response.data},
    fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),
  });
  vm.runInContext(code,context);
  return {get,requests,context,open(){get('prepay-sources').open=true;get('prepay-sources').events.toggle();},
    submit(){get('prepay-sources-period').events.submit({preventDefault(){}});},
    change(start,end){get('prepay-sources-from').value=start;get('prepay-sources-to').value=end;
      get('prepay-sources-period').events.input();},
    respond(index,data){requests[index].resolve({data});}};
}
const part = items => ({status:'ok',items,checked_at:'2026-10-12T18:00:00+05:00',issue:null});
const data = (start='2026-10-05',end='2026-11-04') => ({start,end,reservations:part([]),payments:part([]),matching_issue:null});

test('range includes 31 days and allows future dates independently of the cashier day',()=>{
  const h=harness(), view=h.context.RetroPrepaymentSources;
  assert.equal(view.validRange('2099-01-01','2099-01-31'),true);
  assert.equal(view.validRange('2099-01-01','2099-02-01'),false);
  assert.equal(view.validRange('2026-10-12','2026-10-11'),false);
  assert.equal(view.shiftDay('2026-10-01',-1),'2026-09-30');
});

test('does not query iiko until expanded and never submits a write',async()=>{
  const h=harness();await setImmediate();assert.equal(h.requests.length,0);
  h.open();await setImmediate();assert.equal(h.requests.length,1);
  assert.ok(h.requests[0].url.startsWith('/api/cashier/prepayment-sources?'));
  assert.ok(h.requests[0].url.includes('start=2026-10-06'));
  assert.equal(h.requests[0].options.method,undefined);
  h.respond(0,data());await setImmediate();
  assert.match(h.get('prepay-reserves-status').textContent,/Броней на этот период нет/);
});

test('old in-flight results cannot overwrite a newly selected period',async()=>{
  const h=harness();await setImmediate();h.open();await setImmediate();
  h.change('2026-10-15','2026-10-20');h.submit();
  assert.equal(h.requests[0].options.signal.aborted,true);
  h.respond(1,data('2026-10-15','2026-10-20'));await setImmediate();
  h.respond(0,data());await setImmediate();
  assert.match(h.get('prepay-sources-status').textContent,/15\.10\.2026 — 20\.10\.2026/);
  assert.equal(h.get('prepay-sources-load').disabled,false);
});

test('a failed source is visibly unavailable while other source remains usable',async()=>{
  const h=harness();await setImmediate();h.open();await setImmediate();
  const response=data();response.reservations={status:'unavailable',items:null,issue:'Не удалось получить брони iiko.'};
  response.payments=part([{payment_method:'Xumo',received_at:'2026-10-10T17:56:23+05:00',amount:'1638000',
    link_status:'unconfirmed',candidate_order:{order_number:'71314'}}]);
  h.respond(0,response);await setImmediate();
  assert.match(h.get('prepay-reserves-status').textContent,/Не удалось/);
  assert.doesNotMatch(h.get('prepay-reserves-status').textContent,/Броней на этот период нет/);
  assert.match(h.get('prepay-source-payments').textContent,/Xumo/);
  assert.match(h.get('prepay-source-payments').textContent,/связь с заказом не подтверждена/);
  assert.match(h.get('prepay-source-payments').textContent,/Возможно, заказ №71314/);
});
