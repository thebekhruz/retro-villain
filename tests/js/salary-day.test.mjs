import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import logic from '../../retro/static/salary-day-logic.js';

const month=extra=>({month:'2026-10',today:'2026-10-07',entry_start:'2026-10-07',closed:false,
  days:['2026-10-06','2026-10-07','2026-10-08'],people:[{id:1,name:'Сотрудник',rate:'350000',cells:{'2026-10-06':{amount:'125000',editable:false},'2026-10-07':{amount:'50000'}}}],...extra});

test('manual entry opens on October 7; historic and future payouts stay read-only',()=>{
  const data=month(),person=data.people[0];
  assert.equal(logic.canEdit(data,person,'2026-10-06'),false);
  assert.equal(logic.canEdit(data,person,'2026-10-07'),true);
  assert.equal(logic.canEdit(data,person,'2026-10-08'),false);
  assert.equal(logic.canEdit({...data,today:'2026-10-06'},person,'2026-10-07'),false);
  assert.equal(logic.canEdit({...data,closed:true},person,'2026-10-07'),false);
  assert.equal(logic.canEdit(data,{...person,archived:true},'2026-10-07'),false);
  assert.equal(logic.canEdit(data,{...person,cells:{'2026-10-07':{amount:'50000',editable:false}}},'2026-10-07'),false);
});
test('matrix sums actual manual payments, not rates; missing days are zero',()=>{
  const view=logic.matrix(month());
  assert.equal(view.total,175000);
  assert.equal(view.people[0].paid,175000);
  assert.equal(view.perDay['2026-10-08'],0);
  assert.equal(view.people[0].cells[1].workDay,'2026-10-06');
  assert.equal(logic.previousDay('2026-11-01'),'2026-10-31');
  assert.equal(logic.shiftMonth('2026-12',1),'2027-01');
});
test('manual amount accepts arbitrary sums and zero reversals without accepting malformed input',()=>{
  assert.equal(logic.parseAmount('500 000,50'),500000.5);
  assert.equal(logic.parseAmount(''),0);
  assert.equal(logic.parseAmount('0'),0);
  for(const bad of ['-1','1.005','1 млн','Infinity','1e6'])assert.equal(logic.parseAmount(bad),null);
  assert.throws(()=>logic.matrix(month({people:[{id:1,cells:{'2026-10-07':{amount:null}}}]})),/прочитать/);
});

const source=readFileSync(new URL('../../retro/static/salary-day.js',import.meta.url),'utf8');
const code=source.slice(source.indexOf('function commit('),source.indexOf('async function loadMonth('));
function harness(){
  const requests=[],messages=[];
  const context=vm.createContext({L:logic,B:null,current:month(),inflight:new Map(),writes:0,queue:Promise.resolve(),
    controls(){},totals(){},fmt:value=>String(value),message:(...args)=>messages.push(args),
    dirty:input=>logic.parseAmount(input.value)!==logic.parseAmount(input.dataset.clean),
    fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})),
  });
  vm.runInContext(code,context);
  const input={value:'170000',dataset:{clean:'50000',busyKey:'sd:1:2026-10-07'},disabled:false};
  return {context,input,requests,messages,commit:()=>context.commit(1,'2026-10-07',input)};
}
test('manual cell uses one absolute PUT with expected value and never calls auto-accrual',async()=>{
  const h=harness(),work=h.commit();await Promise.resolve();
  assert.equal(h.requests.length,1);
  assert.equal(h.requests[0].url,'/api/accountant/salary-day/cell');
  assert.equal(h.requests[0].options.method,'PUT');
  assert.deepEqual(JSON.parse(h.requests[0].options.body),{date:'2026-10-07',employee_id:1,amount:'170000',expected_amount:'50000'});
  assert.equal(h.commit(),work,'duplicate events share one write');
  h.requests[0].resolve({ok:true,json:async()=>({amount:'170000',work_day:'2026-10-06',editable:true})});
  assert.equal(await work,true);
  assert.equal(h.input.dataset.clean,'170000');
  assert.equal(h.context.current.people[0].cells['2026-10-07'].amount,'170000');
  assert.equal(h.context.writes,0);
});
test('rejected write restores exact saved value and does not alter totals source',async()=>{
  const h=harness();h.input.dataset.clean='50 000';const work=h.commit();await Promise.resolve();
  h.requests[0].resolve({ok:false,json:async()=>({detail:'Сумма изменилась'})});
  assert.equal(await work,false);
  assert.equal(h.input.value,'50 000');
  assert.equal(h.context.current.people[0].cells['2026-10-07'].amount,'50000');
  assert.equal(h.input.disabled,false);
  assert.equal(h.messages.at(-1)[0],'Сумма изменилась');
});
test('clearing a cell writes zero with the same optimistic concurrency guard',async()=>{
  const h=harness();h.input.value='';const work=h.commit();await Promise.resolve();
  assert.equal(JSON.parse(h.requests[0].options.body).amount,'0');
  h.requests[0].resolve({ok:true,json:async()=>({amount:'0'})});await work;
  assert.equal(h.input.value,'');
});
