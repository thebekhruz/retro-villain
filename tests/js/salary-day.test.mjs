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

test('checkbox cell: rate of the shift day, three states, toggle target',()=>{
  const person={id:1,rate:'350000',cells:{'2026-10-07':{amount:'0',rate:'300000'},'2026-10-08':{amount:'0'}}};
  assert.equal(logic.rateOf(person,'2026-10-07'),300000,'server rate of the shift day wins');
  assert.equal(logic.rateOf(person,'2026-10-08'),350000,'falls back to the current rate');
  assert.equal(logic.rateOf({rate:null,cells:{}},'2026-10-07'),null);
  assert.equal(logic.rateOf({rate:'0',cells:{}},'2026-10-07'),null,'zero rate cannot be ticked');
  assert.equal(logic.rateOf({rate:'350000',cells:{'2026-10-07':{rate:null}}},'2026-10-07'),null,'explicit null from server means no rate that day');
  assert.equal(logic.cellState(0,300000),'off');
  assert.equal(logic.cellState(300000,300000),'on');
  assert.equal(logic.cellState(150000,300000),'odd');
  assert.equal(logic.cellState(150000,null),'odd');
  assert.equal(logic.toggleTarget(0,300000),300000);
  assert.equal(logic.toggleTarget(150000,300000),0,'any paid cell is cleared by a click');
  assert.equal(logic.toggleTarget(0,null),null,'no rate: amount must be typed');
});
test('pay-all ticks only editable, unpaid people who have a rate',()=>{
  const data=month({people:[
    {id:1,rate:'350000',cells:{'2026-10-07':{amount:'0'}}},
    {id:2,rate:'200000',cells:{'2026-10-07':{amount:'50000'}}},
    {id:3,rate:null,cells:{'2026-10-07':{amount:'0'}}},
    {id:4,rate:'150000',archived:true,cells:{'2026-10-07':{amount:'0'}}},
    {id:5,rate:'150000',cells:{'2026-10-07':{amount:'0',editable:false}}},
    {id:6,rate:'180000',cells:{}},
  ]});
  assert.deepEqual(logic.bulkTargets(data,'2026-10-07').map(t=>[t.person.id,t.amount]),[[1,350000],[6,180000]]);
  assert.deepEqual(logic.bulkTargets(data,'2026-10-06'),[],'history day is read-only');
  assert.deepEqual(logic.matrix(data).marked,{'2026-10-06':0,'2026-10-07':1,'2026-10-08':0});
});

const source=readFileSync(new URL('../../retro/static/salary-day.js',import.meta.url),'utf8');
const code=source.slice(source.indexOf('function commit('),source.indexOf('async function loadMonth('));
function harness(){
  const requests=[],messages=[],painted=[];
  const context=vm.createContext({L:logic,current:month(),inflight:new Map(),writes:0,queue:Promise.resolve(),
    controls(){},totals(){},paintCell:(person,day)=>painted.push([person.id,day,person.cells[day].amount]),
    message:(...args)=>messages.push(args),
    fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})),
  });
  vm.runInContext(code,context);
  const person=context.current.people[0];
  return {context,person,requests,messages,painted,commit:amount=>context.commit(person,'2026-10-07',amount)};
}
test('tick uses one absolute PUT with the expected value; the cell repaints at once',async()=>{
  const h=harness(),work=h.commit(350000);await Promise.resolve();
  assert.equal(h.requests.length,1);
  assert.equal(h.requests[0].url,'/api/accountant/salary-day/cell');
  assert.equal(h.requests[0].options.method,'PUT');
  assert.deepEqual(JSON.parse(h.requests[0].options.body),{date:'2026-10-07',employee_id:1,amount:'350000',expected_amount:'50000'});
  assert.deepEqual(h.painted[0],[1,'2026-10-07','350000'],'optimistic paint before the server answers');
  assert.equal(h.context.writes,1);
  assert.equal(h.commit(0),work,'a second click during the write shares it — no double payout');
  h.requests[0].resolve({ok:true,json:async()=>({amount:'350000',work_day:'2026-10-06',editable:true})});
  assert.equal(await work,true);
  assert.equal(h.person.cells['2026-10-07'].amount,'350000');
  assert.equal(h.person.cells['2026-10-07'].work_day,'2026-10-06');
  assert.equal(h.context.writes,0);
});
test('rejected write restores the saved cell and shows the reason',async()=>{
  const h=harness(),work=h.commit(350000);await Promise.resolve();
  h.requests[0].resolve({ok:false,json:async()=>({detail:'Сумма изменилась'})});
  assert.equal(await work,false);
  assert.equal(h.person.cells['2026-10-07'].amount,'50000');
  assert.equal(h.painted.at(-1)[2],'50000','repainted back');
  assert.equal(h.messages.at(-1)[0],'Сумма изменилась');
  assert.equal(h.context.writes,0);
});
test('untick writes zero with the same guard; same value or read-only day sends nothing',async()=>{
  const h=harness(),work=h.commit(0);await Promise.resolve();
  assert.equal(JSON.parse(h.requests[0].options.body).amount,'0');
  h.requests[0].resolve({ok:true,json:async()=>({amount:'0'})});await work;
  assert.equal(h.person.cells['2026-10-07'].amount,'0');
  assert.equal(await h.commit(0),true);
  assert.equal(await h.context.commit(h.person,'2026-10-06',350000),false);
  assert.equal(h.requests.length,1);
});
