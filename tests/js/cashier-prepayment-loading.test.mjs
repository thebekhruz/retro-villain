import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../../retro/static/app.js',import.meta.url),'utf8');
const loader=source.slice(source.indexOf('async function loadPrepaymentMethods('),
  source.indexOf("$('prepayment-methods-retry').addEventListener"));
function screen(){
  const pending=[];
  const context=vm.createContext({generation:1,snapshot:{snapshot_id:'first',date:'2026-10-06'},
    prepaymentMethods:null,prepaymentMethodsKey:null,renders:0,
    renderPayments(){context.renders++},
    request(url){return new Promise((resolve,reject)=>pending.push({url,resolve,reject}))},
  });
  vm.runInContext(loader,context);
  return {context,pending,load:()=>context.loadPrepaymentMethods(context.snapshot,context.generation)};
}

test('details load independently and are requested only once for the displayed snapshot',async()=>{
  const {context,pending,load}=screen();
  const flight=load();
  assert.equal(context.renders,1);
  await load();
  assert.equal(pending.length,1);
  pending[0].resolve({json:async()=>({status:'ready',snapshot_id:'first'})});
  await flight;
  assert.equal(context.prepaymentMethods.status,'ready');
});

test('a response from a previously selected date cannot replace current details',async()=>{
  const {context,pending,load}=screen();
  const old=load();
  context.generation++;
  context.snapshot={snapshot_id:'second',date:'2026-10-07'};
  const current=load();
  pending[1].resolve({json:async()=>({status:'pending',note:'Wait for shift close'})});
  await current;
  pending[0].resolve({json:async()=>({status:'ready',snapshot_id:'first'})});
  await old;
  assert.equal(context.prepaymentMethods.status,'pending');
  assert.equal(context.snapshot.date,'2026-10-07');
});

test('failed details leave the displayed snapshot intact and permit a retry',async()=>{
  const {context,pending,load}=screen();
  const flight=load();
  pending[0].reject(new Error('iiko unavailable'));
  await flight;
  assert.equal(context.prepaymentMethods.status,'unavailable');
  assert.equal(context.snapshot.snapshot_id,'first');
  const retry=context.loadPrepaymentMethods(context.snapshot,1,undefined,true);
  assert.match(pending[1].url,/refresh=true/);
  pending[1].resolve({json:async()=>({status:'ready'})});
  await retry;
  assert.equal(context.prepaymentMethods.status,'ready');
});

test('demo or still refreshing day does not request live prepayment details',async()=>{
  const {context,pending,load}=screen();
  for(const flag of ['demo','stale','refreshing']){
    context.snapshot={snapshot_id:'first',[flag]:true};
    await load();
  }
  assert.equal(pending.length,0);
});
