import test from 'node:test';
import assert from 'node:assert/strict';
import '../../retro/static/founder-broadcast-logic.js';

const logic=globalThis.FounderBroadcastLogic;

test('broadcast draft must be non-empty and within Telegram text limit',()=>{
  assert.equal(logic.validateText('   ').ok,false);
  assert.equal(logic.validateText('x'.repeat(4097)).ok,false);
  assert.deepEqual(logic.validateText('  Новое меню  '),{ok:true,text:'Новое меню'});
});

test('broadcast operation ids are UUIDs and terminal states stop polling',()=>{
  assert.match(logic.operationId({randomUUID:()=> '12345678-1234-4123-8123-123456789012'}),/^[0-9a-f-]{36}$/);
  assert.equal(logic.isTerminal('running'),false);
  assert.equal(logic.isTerminal('completed'),true);
  assert.match(logic.statusText({status:'completed',sent:2,blocked:1,failed:0,audience:3}),/отправлено 2/);
});

test('targeted broadcast requires unique recipients that still exist',()=>{
  const available=['A'.repeat(32),'B'.repeat(32)];
  assert.equal(logic.validateRecipients([],available).ok,false);
  assert.equal(logic.validateRecipients(['A'.repeat(32),'missing'],available).ok,false);
  assert.deepEqual(logic.validateRecipients(['B'.repeat(32),'B'.repeat(32)],available),{
    ok:true,ids:['B'.repeat(32)],
  });
});

test('the same text to the same guests keeps one draft key: repeat sends reuse the operation id',()=>{
  const a=logic.draftKey('  Завтра плов с 12:00 ',['B'.repeat(32),'A'.repeat(32)]);
  assert.equal(a,logic.draftKey('Завтра плов с 12:00',['A'.repeat(32),'B'.repeat(32),'A'.repeat(32)]));
  assert.notEqual(a,logic.draftKey('Завтра плов с 13:00',['A'.repeat(32),'B'.repeat(32)]));
  assert.notEqual(a,logic.draftKey('Завтра плов с 12:00',['A'.repeat(32)]));
  // В ключе нет текста сообщения: он лежит в браузере учредителя.
  assert.doesNotMatch(a,/плов/);
});

test('repeat warning says how long ago the text was sent, in Russian plural forms',()=>{
  const now=Date.parse('2026-10-17T12:00:00Z'),minute=60000;
  assert.equal(logic.repeatWarning(now-20*1000,now),'Этот текст уже отправлен этим получателям только что.');
  assert.equal(logic.repeatWarning(now-minute,now),'Этот текст уже отправлен этим получателям 1 минуту назад.');
  assert.equal(logic.repeatWarning(now-3*minute,now),'Этот текст уже отправлен этим получателям 3 минуты назад.');
  assert.equal(logic.repeatWarning(now-12*minute,now),'Этот текст уже отправлен этим получателям 12 минут назад.');
  assert.equal(logic.repeatWarning(now-21*minute,now),'Этот текст уже отправлен этим получателям 21 минуту назад.');
  assert.equal(logic.repeatWarning(now-2*60*minute,now),'Этот текст уже отправлен этим получателям 2 часа назад.');
  assert.equal(logic.repeatWarning(now-5*24*60*minute,now),'Этот текст уже отправлен этим получателям 5 дней назад.');
});

test('sent journal keeps a week of drafts and forgets older ones',()=>{
  const now=Date.parse('2026-10-17T12:00:00Z');
  const journal=logic.rememberSent({old:{at:now-8*864e5},recent:{at:now-864e5}},'fresh',now);
  assert.deepEqual(Object.keys(journal).sort(),['fresh','recent']);
  assert.equal(journal.fresh.at,now);
});

/* Сценарий страницы целиком на заглушках DOM: отправили, рассылка готова,
   нажали «Отправить» снова — тот же operation_id, пока черновик тот же. */
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {setImmediate as tick} from 'node:timers/promises';

function page(){
  const nodes=new Map(),posts=[],stored=new Map();
  const element=()=>{const listeners={};return {hidden:false,disabled:false,value:'',textContent:'',children:[],
    classList:{toggle(){},add(){},remove(){}},dataset:{},style:{},
    addEventListener(type,handler){listeners[type]=handler},fire(type,event={}){return listeners[type]?.({preventDefault(){},...event})},
    append(...items){this.children.push(...items)},replaceChildren(...items){this.children=items},
    querySelectorAll(){return []},focus(){},setAttribute(){}}};
  const $=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id)};
  const recipients=[{id:'A'.repeat(32),name:'Азиз',phone:'+998 90 000 00 01'}];
  const context=vm.createContext({
    console,setTimeout:()=>0,clearTimeout(){},Date,JSON,Object,Promise,Set,Map,Array,String,Number,Boolean,Math,
    crypto:{randomUUID:(()=>{let n=0;return ()=>`00000000-0000-4000-8000-${String(++n).padStart(12,'0')}`})()},
    localStorage:{getItem:key=>stored.get(key)??null,setItem:(key,value)=>stored.set(key,value)},
    document:{getElementById:$,createElement:element},
    RetroState:{responseJson:async value=>value},
    fetch:async(url,options={})=>{
      if(url==='/api/founder/broadcast/audience')return {subscribers:1,profiles:1,recipients};
      if(options.method==='POST'){const body=JSON.parse(options.body);posts.push(body);return {id:body.operation_id,status:'completed',sent:1,blocked:0,failed:0,audience:1}}
      return {id:url.split('/').pop(),status:'completed',sent:1,blocked:0,failed:0,audience:1};
    },
  });
  context.globalThis=context;
  vm.runInContext(readFileSync(new URL('../../retro/static/founder-broadcast-logic.js',import.meta.url),'utf8'),context);
  vm.runInContext(readFileSync(new URL('../../retro/static/founder-broadcast.js',import.meta.url),'utf8'),context);
  return {$,posts,context};
}
async function sendDraft(h,text,label){
  h.$('broadcast-text').value=text;
  await h.$('broadcast-form').fire('submit');await tick();
  assert.equal(h.$('broadcast-confirm').hidden,false);
  assert.equal(h.$('broadcast-confirm-send').textContent,label);
  await h.$('broadcast-confirm-send').fire('click');await tick();await tick();
}

test('a finished broadcast warns before the same draft goes out again',async()=>{
  const h=page();await tick();
  h.$('broadcast-select-all').fire('click');
  await sendDraft(h,'Завтра плов с 12:00','Отправить');
  assert.equal(h.posts.length,1);
  assert.equal(h.$('broadcast-repeat').hidden,true);
  // Тот же текст тем же гостям: предупреждение и другая кнопка.
  h.$('broadcast-text').value='Завтра плов с 12:00';
  await h.$('broadcast-form').fire('submit');await tick();
  assert.equal(h.$('broadcast-repeat').hidden,false);
  assert.match(h.$('broadcast-repeat').textContent,/^Этот текст уже отправлен этим получателям только что\.$/);
  assert.equal(h.$('broadcast-confirm-send').textContent,'Отправить ещё раз');
  // Повтор по явной кнопке «Отправить ещё раз» — новая рассылка с новым id.
  await h.$('broadcast-confirm-send').fire('click');await tick();await tick();
  assert.equal(h.posts.length,2);
  assert.notEqual(h.posts[0].operation_id,h.posts[1].operation_id);
  // Другой текст — обычная отправка, без предупреждения.
  await sendDraft(h,'Послезавтра лагман','Отправить');
  assert.equal(h.$('broadcast-repeat').hidden,true);
  assert.equal(h.posts.length,3);
});

test('a retry of a send that did not answer keeps the same operation id',async()=>{
  const h=page();await tick();
  h.$('broadcast-select-all').fire('click');
  let fail=true;const original=h.context.fetch;
  h.context.fetch=async(url,options={})=>{
    if(options.method==='POST'&&fail){fail=false;h.posts.push(JSON.parse(options.body));throw new Error('Сеть недоступна')}
    return original(url,options);
  };
  await sendDraft(h,'Завтра плов с 12:00','Отправить');
  await sendDraft(h,'Завтра плов с 12:00','Отправить');
  assert.equal(h.posts.length,2);
  assert.equal(h.posts[0].operation_id,h.posts[1].operation_id);
});
