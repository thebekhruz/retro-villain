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

/* T-429 (ТЗ 09.10, Б-04): до выдачи клетка показывает посещаемость смены, как «Сотрудники». */
const nbsp=text=>text.replace(/ /g,' ');
test('посещаемость смены в клетке: время, «нет», «?», «был» и подсказка словами',()=>{
  const ihtiyor={status:'on_time',time:'09:31',source:'on_time'};
  const person={id:1,rate:'360000',cells:{'2026-10-09':{amount:'0',attendance:ihtiyor},'2026-10-10':{amount:'0'}}};
  assert.deepEqual(logic.attendanceOf(person,'2026-10-09'),ihtiyor);
  assert.equal(logic.attendanceOf(person,'2026-10-10'),null,'сегодняшняя смена не размечена');
  assert.equal(logic.attendanceOf(person,'2026-10-11'),null);
  assert.equal(logic.shiftMark(ihtiyor),'09:31');
  assert.equal(logic.shiftMark({status:'late',time:'10:58',source:'late'}),'10:58');
  assert.equal(logic.shiftMark({status:'absent',time:null,source:'missing'}),'нет');
  assert.equal(logic.shiftMark({status:'unknown',time:null,source:'unlinked'}),'?');
  assert.equal(logic.shiftMark({status:'manual',time:null,source:'manual_present'}),'был','время входа не придумываем');
  assert.equal(logic.shiftMark(null),'');
  assert.equal(logic.shiftTitle('2026-10-09',ihtiyor,0),'Смена 08.10: пришёл 09:31, вовремя');
  assert.equal(logic.shiftTitle('2026-10-09',{status:'late',time:'10:58',source:'late'},0),'Смена 08.10: пришёл 10:58, опоздал');
  assert.equal(nbsp(logic.shiftTitle('2026-10-08',{status:'absent',time:null,source:'missing'},360000)),
    'Смена 07.10: не пришёл · выдано 360 000 сум','после выдачи смена остаётся в подсказке');
  assert.equal(logic.shiftText({status:'absent',source:'manual_absent'}),'не был, отмечено вручную');
  assert.equal(logic.shiftText({status:'manual',source:'manual_present'}),'был, отмечено вручную');
  assert.equal(logic.shiftText({status:'unknown',source:'unlinked'}),'нет привязки к Hikvision');
  assert.equal(logic.shiftText({status:'unknown',source:'unavailable'}),'нет данных Hikvision');
  assert.equal(logic.shiftTitle('2026-10-09',null,360000),'','без сведений о смене подсказки нет');
});
test('выдача и её снятие не трогают посещаемость из ответа сервера — снял галочку, вернулось время',async()=>{
  const h=harness(),shift={status:'on_time',time:'09:31',source:'on_time'},tick=()=>new Promise(resolve=>setTimeout(resolve,0));
  h.person.cells['2026-10-07'].attendance=shift;
  let work=h.commit(350000);await tick();
  h.requests[0].resolve({ok:true,json:async()=>({amount:'350000',work_day:'2026-10-06',editable:true})});await work;
  assert.deepEqual(h.person.cells['2026-10-07'].attendance,shift);
  work=h.commit(0);await tick();
  h.requests[1].resolve({ok:true,json:async()=>({amount:'0',work_day:'2026-10-06',editable:true})});await work;
  assert.equal(h.person.cells['2026-10-07'].amount,'0');
  assert.equal(logic.shiftMark(logic.attendanceOf(h.person,'2026-10-07')),'09:31');
});

/* T-434: временная хостес Карамат с 08.10 по 10.10. Клетка выплаты — смена
   накануне: открыты 09.10, 10.10 и 11.10 (выплата за последнюю смену назавтра). */
test('временный: клетки вне периода заперты и пусты, итоги прежние, подсказка с периодом', () => {
  const cell = (day, extra) => ({amount: '0', work_day: logic.previousDay(day), ...extra});
  const karamat = {id: 9, name: 'Карамат', role: 'Хостес', rate: '150000', temporary: true, work_period: '08.10–10.10',
    cells: {'2026-10-08': cell('2026-10-08', {editable: false, outside: true}),
      '2026-10-09': cell('2026-10-09', {amount: '150000'}), '2026-10-11': cell('2026-10-11'),
      '2026-10-12': cell('2026-10-12', {editable: false, outside: true})}};
  const data = {month: '2026-10', today: '2026-10-12', entry_start: '2026-10-02', closed: false,
    days: ['2026-10-08', '2026-10-09', '2026-10-11', '2026-10-12'], people: [karamat], extras: []};
  assert.deepEqual(data.days.map(day => logic.canEdit(data, karamat, day)), [false, true, true, false]);
  assert.deepEqual(data.days.map(day => logic.isOutside(karamat, day)), [true, false, false, true]);
  assert.equal(logic.outsideTitle(karamat), 'Работает 08.10–10.10');
  assert.equal(logic.outsideTitle({...karamat, work_period: 'с 08.10'}), 'Работает с 08.10');
  assert.equal(logic.outsideTitle({...karamat, work_period: null}), '');
  assert.equal(logic.typeTag(karamat), 'временный · 08.10–10.10');
  assert.equal(logic.typeTag({...karamat, work_period: null}), 'временный');
  assert.equal(logic.typeTag({name: 'Сменный'}), '');
  const view = logic.matrix(data);
  assert.equal(view.total, 150000);
  assert.deepEqual(view.people[0].cells.map(c => c.editable), [false, true, true, false]);
});

/* T-428: открытая с вечера ведомость не оставляет вчерашнюю дату «сегодня». */
const calendarCode=source.slice(source.indexOf('async function loadMonth('),source.indexOf('/* Записать клетку;'));
function calendarHarness({today='2026-10-09',selected=today,sheetMonth=today.slice(0,7)}={}){
  const requests=[],messages=[],elements=new Map();
  const element=id=>{
    if(!elements.has(id))elements.set(id,{value:'',style:{},classList:{remove(){}},setAttribute(){},querySelector(){return null;}});
    return elements.get(id);
  };
  const clock={now:new Date('2026-10-09T19:00:01Z')};
  class ClockDate extends Date{constructor(...args){super(...(args.length?args:[clock.now]));}}
  const context=vm.createContext({L:logic,Date:ClockDate,AbortController,encodeURIComponent,
    current:month({month:sheetMonth,today}),today,selectedDay:selected,calendarDay:today,
    loading:false,writes:0,editing:null,dateCheck:null,sequence:0,controller:null,
    document:{hidden:false},dirty:[],extraBusy:false,
    $:element,monthTitle:value=>value,message:(...args)=>messages.push(args),controls(){},render(){},
    fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})),
  });
  vm.runInContext('globalThis.RetroSave={pending:()=>dirty};const extra={busy:()=>extraBusy};'+calendarCode,context);
  const reply=(index,data,ok=true)=>requests[index].resolve({ok,json:async()=>data});
  const sheet=(day,which=day.slice(0,7))=>month({month:which,today:day,entry_start:'2026-10-02',
    days:which==='2026-11'?['2026-11-01','2026-11-02']:['2026-10-08','2026-10-09','2026-10-10','2026-10-31']});
  return {context,clock,requests,messages,elements,reply,sheet,refresh:force=>context.refreshDate(force)};
}
const flush=()=>new Promise(resolve=>setTimeout(resolve,0));

test('Tashkent midnight, month and year boundaries are independent of device timezone',()=>{
  assert.equal(logic.tashkentDay(new Date('2026-10-09T18:59:59Z')),'2026-10-09');
  assert.equal(logic.tashkentDay(new Date('2026-10-09T19:00:00Z')),'2026-10-10');
  assert.equal(logic.tashkentDay(new Date('2026-10-31T19:00:00Z')),'2026-11-01');
  assert.equal(logic.previousDay('2027-01-01'),'2026-12-31');
});

test('overnight refresh selects the new payout day, reloads permissions, and does not write money',async()=>{
  const h=calendarHarness(),work=h.refresh();
  assert.equal(h.requests[0].url,'/api/config');
  h.reply(0,{today:'2026-10-10'});await flush();
  assert.equal(h.requests[1].url,'/api/accountant/salary-day/month?month=2026-10');
  assert.equal(h.elements.get('sheet-grid').inert,true);
  h.reply(1,h.sheet('2026-10-10'));assert.equal(await work,true);
  assert.equal(h.context.today,'2026-10-10');
  assert.equal(h.context.selectedDay,'2026-10-10');
  assert.equal(logic.previousDay(h.context.selectedDay),'2026-10-09');
  assert.equal(logic.canEdit(h.context.current,h.context.current.people[0],'2026-10-10'),true);
  assert.ok(h.requests.every(request=>!request.options.method&&request.options.cache==='no-store'));
  assert.equal(await h.refresh(),false,'no polling of the server while the date is unchanged');
  assert.equal(h.requests.length,2);
});

test('first payout of a new month opens the new month; past day selection stays put',async()=>{
  for(const selected of ['2026-10-31','2026-10-08']){
    const h=calendarHarness({today:'2026-10-31',selected});h.clock.now=new Date('2026-10-31T19:00:01Z');
    const work=h.refresh();h.reply(0,{today:'2026-11-01'});await flush();
    const target=selected==='2026-10-31'?'2026-11':'2026-10';
    assert.equal(h.requests[1].url,'/api/accountant/salary-day/month?month='+target);
    h.reply(1,h.sheet('2026-11-01',target));assert.equal(await work,true);
    assert.equal(h.context.current.month,target);
    assert.equal(h.context.selectedDay,target==='2026-11'?'2026-11-01':selected);
    assert.equal(h.elements.get('month-input').max,'2026-11');
  }
});

test('pending payout, amount editor and extra payout draft defer rollover without discarding data',async()=>{
  for(const state of [{writes:1},{editing:{input:'123000'}},{extraBusy:true},{dirty:['extra-form']},{loading:true}]){
    const h=calendarHarness();Object.assign(h.context,state);
    assert.equal(await h.refresh(true),false);
    assert.equal(h.requests.length,0);
    assert.equal(h.context.selectedDay,'2026-10-09');
    Object.assign(h.context,{writes:0,editing:null,extraBusy:false,dirty:[],loading:false});
    const work=h.refresh();h.reply(0,{today:'2026-10-10'});await flush();
    h.reply(1,h.sheet('2026-10-10'));assert.equal(await work,true);
    assert.equal(h.context.selectedDay,'2026-10-10');
  }
});

test('draft started during the date request or month reload is preserved until the next refresh',async()=>{
  for(const stage of ['config','month']){
    const h=calendarHarness(),before=h.context.current,work=h.refresh();
    if(stage==='config')h.context.editing={input:'123000'};
    h.reply(0,{today:'2026-10-10'});await flush();
    if(stage==='month'){h.context.dirty=['extra-form'];h.reply(1,h.sheet('2026-10-10'));}
    assert.equal(await work,false);
    assert.equal(h.context.current,before);
    assert.equal(h.context.today,'2026-10-09');
    assert.equal(h.context.selectedDay,'2026-10-09');
    assert.equal(h.context.loading,false);
  }
});

test('failed month reload keeps dates unchanged and retries; simultaneous focus events share a check',async()=>{
  const h=calendarHarness(),first=h.refresh(),second=h.refresh(true);
  assert.equal(h.requests.length,1);
  h.reply(0,{today:'2026-10-10'});await flush();
  h.reply(1,{detail:'Нет связи'},false);
  assert.deepEqual(await Promise.all([first,second]),[false,false]);
  assert.equal(h.context.today,'2026-10-09');
  assert.equal(h.context.selectedDay,'2026-10-09');
  assert.equal(h.elements.get('sheet-grid').inert,false);
  const retry=h.refresh();h.reply(2,{today:'2026-10-10'});await flush();
  h.reply(3,h.sheet('2026-10-10'));assert.equal(await retry,true);
});

test('server date wins over device clock; visibility/focus can check it again',async()=>{
  const h=calendarHarness(),work=h.refresh();
  h.reply(0,{today:'2026-10-09'});assert.equal(await work,false);
  assert.equal(h.context.today,'2026-10-09');
  assert.equal(await h.refresh(),false);
  assert.equal(h.requests.length,1);
  h.context.document.hidden=true;assert.equal(await h.refresh(true),false);
  h.context.document.hidden=false;
  const focus=h.refresh(true);h.reply(1,{today:'2026-10-10'});await flush();
  h.reply(2,h.sheet('2026-10-10'));assert.equal(await focus,true);
});
