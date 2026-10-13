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
    visit(day){get('prepay-reserves-date').value=day;get('prepay-reserves-date').events.input();
      get('prepay-reserves-date').events.change();},
    refreshVisit(){get('prepay-reserves-day').events.submit({preventDefault(){}});},
    respond(index,data){requests[index].resolve({data});}};
}
const part = items => ({status:'ok',items,checked_at:'2026-10-12T18:00:00+05:00',issue:null});
const data = (start='2026-10-05',end='2026-11-04') => ({start,end,reservations:part([]),payments:part([]),matching_issue:null});
const visits = (day='2026-10-12',items=[]) => ({date:day,reservations:part(items)});
const reserve = (day='2026-10-16') => ({id:'reserve-1',estimated_start_time:day+'T19:00:00+05:00',
  guests_count:2,duration_minutes:180,tables:[{number:22,section:'Ресторан'}]});

test('range includes 31 days and allows future dates independently of the cashier day',()=>{
  const h=harness(), view=h.context.RetroPrepaymentSources;
  assert.equal(view.validRange('2099-01-01','2099-01-31'),true);
  assert.equal(view.validRange('2099-01-01','2099-02-01'),false);
  assert.equal(view.validRange('2026-10-12','2026-10-11'),false);
  assert.equal(view.shiftDay('2026-10-01',-1),'2026-09-30');
});

test('expansion loads only the current visit day, without a payment or report request',async()=>{
  const h=harness();await setImmediate();assert.equal(h.requests.length,0);
  h.open();await setImmediate();assert.equal(h.requests.length,1);
  assert.ok(h.requests[0].url.startsWith('/api/cashier/reservations?'));
  assert.ok(h.requests[0].url.includes('date=2026-10-12'));
  assert.equal(h.requests[0].options.method,undefined);
  h.respond(0,visits());await setImmediate();
  assert.match(h.get('prepay-reserves-status').textContent,/Броней на выбранную дату нет/);
  assert.match(h.get('prepay-reserves-summary').textContent,/12\.10\.2026 · Броней 0/);
});

test('old in-flight results cannot overwrite a newly selected period',async()=>{
  const h=harness();await setImmediate();h.open();await setImmediate();
  h.respond(0,visits());await setImmediate();
  h.submit();
  h.change('2026-10-15','2026-10-20');h.submit();
  assert.equal(h.requests[1].options.signal.aborted,true);
  assert.ok(h.requests[2].url.startsWith('/api/cashier/prepayment-sources/payments?'));
  h.respond(2,data('2026-10-15','2026-10-20'));await setImmediate();
  h.respond(1,data());await setImmediate();
  assert.match(h.get('prepay-sources-status').textContent,/15\.10\.2026 — 20\.10\.2026/);
  assert.equal(h.get('prepay-sources-load').disabled,false);
});

test('a failed source is visibly unavailable while other source remains usable',async()=>{
  const h=harness();await setImmediate();h.open();await setImmediate();
  h.respond(0,{date:'2026-10-12',reservations:{status:'unavailable',items:null,issue:'Не удалось получить брони iiko.'}});
  await setImmediate();h.submit();
  const response=data();
  response.payments=part([{payment_method:'Xumo',received_at:'2026-10-10T17:56:23+05:00',amount:'1638000',
    link_status:'unconfirmed',candidate_order:{order_number:'71314'}}]);
  h.respond(1,response);await setImmediate();
  assert.match(h.get('prepay-reserves-status').textContent,/Не удалось/);
  assert.doesNotMatch(h.get('prepay-reserves-status').textContent,/Броней на этот период нет/);
  assert.match(h.get('prepay-source-payments').textContent,/Xumo/);
  assert.match(h.get('prepay-source-payments').textContent,/связь с заказом не подтверждена/);
  assert.match(h.get('prepay-source-payments').textContent,/Возможно, заказ №71314/);
});

test('switching the visit date discards old responses and does not change the payment period',async()=>{
  const h=harness();await setImmediate();h.open();await setImmediate();
  const receiptStart=h.get('prepay-sources-from').value;
  h.visit('2026-10-16');
  assert.equal(h.requests[0].options.signal.aborted,true);
  h.respond(1,visits('2026-10-16',[reserve()]));await setImmediate();
  h.respond(0,visits());await setImmediate();
  assert.match(h.get('prepay-reserves-list').textContent,/16\.10\.2026 · 19:00/);
  assert.match(h.get('prepay-reserves-list').textContent,/Стол 22/);
  assert.match(h.get('prepay-reserves-summary').textContent,/Броней 1 · Гостей 2/);
  assert.equal(h.get('prepay-sources-from').value,receiptStart);
  assert.equal(h.requests.length,2);
});

test('refresh replaces a moved or cancelled reservation instead of appending duplicate cards',async()=>{
  const h=harness();await setImmediate();h.visit('2026-10-16');
  h.respond(0,visits('2026-10-16',[reserve()]));await setImmediate();
  assert.equal(h.get('prepay-reserves-list').children.length,1);
  h.refreshVisit();
  assert.equal(h.get('prepay-reserves-list').children.length,0);
  assert.ok(h.requests[1].url.includes('refresh=true'));
  h.respond(1,visits('2026-10-16'));await setImmediate();
  assert.equal(h.get('prepay-reserves-list').children.length,0);
  assert.match(h.get('prepay-reserves-status').textContent,/Броней на выбранную дату нет/);
});

test('visit date navigation and unavailable responses never show a false empty result',async()=>{
  const h=harness();await setImmediate();
  h.get('prepay-reserves-next').events.click();
  assert.equal(h.get('prepay-reserves-date').value,'2026-10-13');
  h.respond(0,{date:'2026-10-13',reservations:{status:'unavailable',items:null,issue:'Не удалось получить брони iiko.'}});
  await setImmediate();
  assert.doesNotMatch(h.get('prepay-reserves-summary').textContent,/Броней 0/);
  h.get('prepay-reserves-today').events.click();
  assert.equal(h.get('prepay-reserves-date').value,'2026-10-12');
  h.respond(1,visits('2026-10-11',[reserve('2026-10-11')]));await setImmediate();
  assert.match(h.get('prepay-reserves-status').textContent,/Не удалось загрузить брони/);
  assert.equal(h.get('prepay-reserves-list').children.length,0);
});
