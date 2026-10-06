import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../retro/static/app.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('function transferDisclosure('), source.indexOf('function showPrepayments('));
const payload = {
  note:'Один тип оплаты в iiko', total:'500000', rows:[
    {order_number:'70713', received_at:'2026-10-06T10:45:00Z', amount:'300000', comment:'<img src=x onerror=alert(1)>'},
    {order_number:'70717', received_at:'2026-10-06T16:00:00+05:00', amount:'200000', comment:''},
  ],
};
function harness() {
  const calls = [], pending = [];
  function element(tag) {
    return {tag, children:[], textContent:'', hidden:false, attributes:{},
      append(...nodes){this.children.push(...nodes)}, replaceChildren(...nodes){this.children=[...nodes]},
      setAttribute(key,value){this.attributes[key]=value}, addEventListener(type,handler){this[type]=handler}};
  }
  const data={date:'2026-10-06',snapshot_id:'a'.repeat(32)};
  const context = vm.createContext({document:{createElement:element}, generation:1, snapshot:data,
    data, controller:new AbortController(), money:new Intl.NumberFormat('ru-RU'), URLSearchParams,
    request(url,signal){calls.push({url,signal});return new Promise((resolve,reject)=>pending.push({resolve,reject}))}});
  vm.runInContext(code,context);
  const disclosure=vm.runInContext("transferDisclosure(data, 'Click/Payme', 'details')",context);
  return {...disclosure,context,calls,pending,complete(result=payload){pending.shift().resolve({json:async()=>result})}};
}

test('payment details load on demand for the displayed snapshot and reuse the result',async()=>{
  const h=harness();
  assert.equal(h.calls.length,0);
  assert.equal(h.details.hidden,true);
  const opening=h.button.click();
  assert.equal(h.button.attributes['aria-expanded'],'true');
  const query=new URL(h.calls[0].url,'https://example.test').searchParams;
  assert.equal(query.get('date'),'2026-10-06');
  assert.equal(query.get('snapshot_id'),'a'.repeat(32));
  h.complete();await opening;
  assert.equal(h.details.children[1].children[0].textContent,'15:45 · Чек № 70713 · <img src=x onerror=alert(1)>');
  assert.equal(h.details.children[1].children[0].children.length,0);
  assert.equal(h.details.children.at(-1).children[1].textContent,new Intl.NumberFormat('ru-RU').format(500000)+' сум');
  await h.button.click();await h.button.click();
  assert.equal(h.calls.length,1);
});

test('collapsing while loading stays collapsed and does not duplicate the request',async()=>{
  const h=harness();const opening=h.button.click();
  await h.button.click();await h.button.click();await h.button.click();
  assert.equal(h.calls.length,1);
  h.complete();await opening;
  assert.equal(h.details.hidden,true);
  assert.equal(h.button.attributes['aria-expanded'],'false');
});

test('a late result cannot replace details after the selected date or snapshot changes',async()=>{
  for(const change of [h=>h.context.generation++,h=>h.context.snapshot={...h.context.data}]) {
    const h=harness();const opening=h.button.click();change(h);h.complete();await opening;
    assert.equal(h.details.children.length,1);
    assert.equal(h.details.children[0].textContent,'Загружаем платежи…');
  }
});

test('failed requests show the error and can be retried on the next expansion',async()=>{
  const h=harness();const opening=h.button.click();
  h.pending.shift().reject(new Error('Данные iiko изменились. Обновите день.'));await opening;
  assert.equal(h.details.children[0].textContent,'Данные iiko изменились. Обновите день.');
  await h.button.click();const retry=h.button.click();
  assert.equal(h.calls.length,2);h.complete();await retry;
  assert.equal(h.details.children.length,4);
});
