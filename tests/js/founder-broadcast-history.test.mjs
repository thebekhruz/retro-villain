import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {setImmediate as tick} from 'node:timers/promises';
import '../../retro/static/founder-broadcast-logic.js';

/* Журнал рассылок (ТЗ 16.10, п. 5.3; T-504): чистые правила экрана и сценарий
   блока на заглушках DOM с фейковым API — без бота и без Telegram. */
const logic=globalThis.FounderBroadcastLogic;
const NOW=Date.parse('2026-10-17T09:30:00Z');// 14:30 по Ташкенту
const job=(patch={})=>({id:'9e3b7a55-1c2d-4e8f-a6b4-3d2c1b0a9f04',status:'completed',audience:33,sent:30,blocked:2,failed:1,pending:0,
  created_at:'2026-10-17T07:57:00Z',source:'dashboard',manual:true,author:'Карина',text:'Новое обеденное меню',recipients_known:true,
  resumable:false,resume_count:0,resumed_at:null,resumed_by:null,throttled_until:null,...patch});

test('источник: из панели, командой в боте, автоматическая, старое задание без источника',()=>{
  assert.equal(logic.sourceLabel(job()),'Из панели');
  assert.equal(logic.sourceLabel(job({source:'tg_admin'})),'Команда в боте');
  assert.equal(logic.sourceLabel(job({source:'auto',manual:false})),'Автоматическая');
  assert.equal(logic.sourceLabel(job({source:null,manual:null})),'Источник не записан');
  assert.equal(logic.isManual(job({source:'tg_admin'})),true);
  assert.equal(logic.isManual(job({source:'auto'})),false);
  // Старые задания запускал человек: автоматических до журнала не было.
  assert.equal(logic.isManual(job({source:null,manual:null})),true);
  assert.equal(logic.isManual(job({source:null,manual:false})),false);
});

test('итоги задания: отправлено, заблокировали, не дошло, в очереди',()=>{
  assert.deepEqual(logic.jobCounts(job({sent:13,blocked:1,failed:1,pending:18,audience:33})).map(([key,,value])=>[key,value]),
    [['sent',13],['blocked',1],['failed',1],['queued',18]]);
  // Бот прежней версии не присылает pending: считаем сами.
  const legacy=job();delete legacy.pending;
  assert.equal(logic.jobCounts({...legacy,sent:20,blocked:1,failed:0,audience:31})[3][2],10);
  assert.equal(logic.jobStatusText(job({status:'running',sent:10,blocked:1,failed:1})),'Идёт: 12 из 33');
  assert.equal(logic.jobStatusText(job({status:'interrupted'})),'Прервана');
  assert.equal(logic.jobTone(job({status:'queued'})),'running');
  assert.equal(logic.jobTone(job()),'done');
  assert.equal(logic.jobTone(job({status:'interrupted'})),'error');
});

test('время по Ташкенту, год только не текущий; ожидание Telegram только будущее',()=>{
  assert.equal(logic.momentText('2026-10-17T07:57:00Z',NOW),'17.10 12:57');
  assert.equal(logic.momentText('2026-10-16T19:30:00Z',NOW),'17.10 00:30');
  assert.equal(logic.momentText('2025-12-31T10:00:00Z',NOW),'31.12.2025 15:00');
  assert.equal(logic.momentText('2025-12-31T20:00:00Z',NOW),'01.01 01:00','в Ташкенте уже 2026');
  assert.equal(logic.momentText(null,NOW),'—');
  assert.equal(logic.throttleText(job({throttled_until:'2026-10-17T09:45:00Z'}),NOW),'Telegram просит подождать до 14:45');
  assert.equal(logic.throttleText(job({throttled_until:'2026-10-17T09:00:00Z'}),NOW),'');
  assert.equal(logic.throttleText(job(),NOW),'');
  assert.equal(logic.resumeText(job({resume_count:1,resumed_at:'2026-10-17T09:00:00Z'}),NOW),'Дослано 17.10 14:00');
  assert.equal(logic.resumeText(job({resume_count:2,resumed_at:'2026-10-17T09:00:00Z'}),NOW),'Досылали: 2 · последний раз 17.10 14:00');
  assert.equal(logic.resumeText(job(),NOW),'');
});

test('начало текста одной строкой; пояснения к получателю',()=>{
  assert.equal(logic.preview('Суп\n\nи   чай'),'Суп и чай');
  assert.equal(logic.preview('x'.repeat(200),10),'x'.repeat(9)+'…');
  assert.deepEqual(logic.deliveryNote({status:'failed',error:'delivery_unknown'}),
    {text:'Бот перезапустился во время отправки: сообщение могло дойти',raw:false});
  assert.deepEqual(logic.deliveryNote({status:'failed',error:'unsubscribed'}),{text:'Гость отписался до отправки',raw:false});
  assert.deepEqual(logic.deliveryNote({status:'failed',error:'Bad Request: message is too long'}),
    {text:'Bad Request: message is too long',raw:true});
  // Заблокировал — статус сам всё говорит.
  assert.equal(logic.deliveryNote({status:'blocked',error:'Forbidden: bot was blocked by the user'}),null);
  assert.equal(logic.deliveryStatusText('blocked'),'Заблокировал бота');
  assert.equal(logic.resumeLabel(18),'Дослать оставшимся: 18');
  assert.match(logic.resumeConfirm(1),/^Сообщение уйдёт 1 гостю из очереди\./);
  assert.match(logic.resumeConfirm(21),/^Сообщение уйдёт 21 гостю /);
  assert.match(logic.resumeConfirm(18),/^Сообщение уйдёт 18 гостям из очереди\. Тем, кто уже получил его/);
});

test('строка журнала панели (T-505) — в виде задания: те же итоги, без получателей и досылки',()=>{
  const job=logic.fromPanelJournal({operation_id:'12345678-1234-4123-8123-123456789012',job_id:'job_'+'x'.repeat(20),
    kind:'manual',sent_by:'Карина',sent_at:'2026-10-17T14:05:09+05:00',text:'Плов',recipients:4,status:'completed',
    audience:4,sent:3,blocked:1,failed:0,status_at:'2026-10-17T14:06:00+05:00'});
  assert.equal(job.id,'job_'+'x'.repeat(20));
  assert.equal(logic.sourceLabel(job),'Из панели');
  assert.equal(job.author,'Карина');
  assert.equal(job.recipients_known,false);assert.equal(job.resumable,false);
  assert.deepEqual(logic.jobCounts(job).map(([,,value])=>value),[3,1,0,0]);
  assert.equal(logic.momentText(job.created_at,NOW),'17.10 14:05');
  assert.equal(logic.panelStatusText(job,NOW),'Итог бота на 17.10 14:06');
  // Итог ещё не узнали: серая плашка, гостей в очереди — сколько выбрано.
  const unknown=logic.fromPanelJournal({operation_id:'x'.repeat(20),job_id:'',kind:'auto',sent_at:'2026-10-17T14:05:09+05:00',
    text:'Напоминание',recipients:2,status:'',audience:0,sent:0,blocked:0,failed:0,status_at:null});
  assert.equal(unknown.id,'x'.repeat(20));
  assert.equal(logic.sourceLabel(unknown),'Автоматическая');
  assert.equal(logic.jobTone(unknown),'unknown');
  assert.equal(logic.jobStatusText(unknown),'Неизвестно');
  assert.equal(logic.jobCounts(unknown)[3][2],2);
  assert.equal(logic.panelStatusText(unknown,NOW),'');
});

test('повтор по журналу бота: такой же текст за сутки из любого источника',()=>{
  const jobs=[
    job({id:'a'.repeat(20),text:'Завтра плов с 12:00',source:'tg_admin',created_at:'2026-10-17T07:30:00Z',sent:36}),
    job({id:'b'.repeat(20),text:'Живая музыка',created_at:'2026-10-15T07:30:00Z'}),
    job({id:'c'.repeat(20),text:'Ничего не ушло',status:'failed',sent:0,blocked:0,failed:0}),
  ];
  const twin=logic.historyTwin(jobs,'  Завтра плов с 12:00 ',NOW);
  assert.equal(twin.id,'a'.repeat(20));
  assert.equal(logic.twinWarning(twin,NOW),'Такой же текст уже рассылали 17.10 12:30 · Команда в боте.');
  assert.equal(logic.historyTwin(jobs,'Живая музыка',NOW),null,'старше суток');
  assert.equal(logic.historyTwin(jobs,'Ничего не ушло',NOW),null,'никому не отправлено');
  assert.equal(logic.historyTwin(jobs,'Другой текст',NOW),null);
  assert.equal(logic.historyTwin(undefined,'Завтра плов с 12:00',NOW),null);
});

/* ── Сценарий блока на заглушках DOM ─────────────────────────────────── */
function dom(){
  const doc={activeElement:null};
  const make=tag=>{
    const listeners={},classes=new Set(),attrs={};
    const node={tagName:String(tag||'div').toUpperCase(),children:[],hidden:false,disabled:false,type:'',textContent:'',dataset:{},style:{},
      classList:{add:(...names)=>names.forEach(name=>classes.add(name)),remove:(...names)=>names.forEach(name=>classes.delete(name)),
        toggle(name,force){const on=force===undefined?!classes.has(name):Boolean(force);on?classes.add(name):classes.delete(name);return on},
        contains:name=>classes.has(name),[Symbol.iterator]:()=>classes[Symbol.iterator]()},
      get className(){return [...classes].join(' ')},set className(value){classes.clear();String(value).split(/\s+/).filter(Boolean).forEach(name=>classes.add(name))},
      setAttribute(key,value){attrs[key]=String(value)},getAttribute:key=>attrs[key]??null,removeAttribute(key){delete attrs[key]},
      addEventListener(type,handler){(listeners[type]||=[]).push(handler)},
      fire(type,event={}){return Promise.all((listeners[type]||[]).map(handler=>handler({preventDefault(){},...event})))},
      append(...items){items.forEach(item=>this.children.push(typeof item==='string'?{text:item}:item))},
      replaceChildren(...items){this.children=[];this.append(...items)},
      contains(other){return other===this||this.children.some(child=>child.contains?.(other))},
      all(){return [this,...this.children.flatMap(child=>child.all?child.all():[])]},
      querySelector(selector){return this.all().slice(1).find(item=>item.classList.contains(selector.slice(1)))||null},
      querySelectorAll(){return []},
      focus(){doc.activeElement=this},scrollIntoView(){},
    };
    return node;
  };
  return {doc,make};
}
const text=node=>node?.text??((node.textContent||'')+node.children.map(text).join(''));
const find=(root,predicate)=>root.all().find(predicate);
const byClass=(root,name)=>root.all().filter(item=>item.classList?.contains(name));
const byText=(root,label)=>find(root,item=>item.tagName==='BUTTON'&&text(item).startsWith(label));

function page(answers){
  const {doc,make}=dom();
  const nodes=new Map(),requests=[],timers=[];
  const $=id=>{if(!nodes.has(id)){const node=make('div');node.id=id;nodes.set(id,node)}return nodes.get(id)};
  const kinds=['all','manual','auto'].map(kind=>{const chip=make('button');chip.dataset.kind=kind;$('broadcast-history-kinds').append(chip);return chip});
  doc.getElementById=$;doc.createElement=make;
  doc.querySelectorAll=selector=>selector.includes('data-kind')?kinds:[];
  const context=vm.createContext({
    console,Date,JSON,Object,Promise,Set,Map,Array,String,Number,Boolean,Math,encodeURIComponent,
    setTimeout:handler=>{timers.push(handler);return timers.length},clearTimeout(){},
    document:doc,RetroState:{responseJson:async value=>value},
    fetch:async(url,options={})=>{requests.push([options.method||'GET',url]);const answer=answers(url,options);if(answer instanceof Error)throw answer;return answer},
  });
  context.globalThis=context;
  vm.runInContext(readFileSync(new URL('../../retro/static/founder-broadcast-logic.js',import.meta.url),'utf8'),context);
  vm.runInContext(readFileSync(new URL('../../retro/static/founder-broadcast-history.js',import.meta.url),'utf8'),context);
  return {$,doc,requests,timers,kinds,context};
}
const settle=async()=>{for(let index=0;index<6;index+=1)await tick()};

test('бот без журнала: те же карточки из журнала панели (T-505), без получателей и досылки',async()=>{
  const entry={operation_id:'12345678-1234-4123-8123-123456789012',job_id:'job_'+'x'.repeat(20),kind:'manual',sent_by:'Карина',
    sent_at:new Date(Date.now()-5*60000).toISOString(),text:'Завтра плов с 12:00',recipients:3,status:'running',audience:3,
    sent:1,blocked:0,failed:0,status_at:'2026-10-17T09:06:00+05:00'};
  let state='running';
  const h=page(url=>{
    if(url==='/api/founder/broadcast/jobs?kind=all')return {supported:false,jobs:[],total:0,next_cursor:null};
    if(url==='/api/founder/broadcast/journal')return {entries:[entry]};
    // Статус — по id задачи бота; старый бот текста и автора не отдаёт.
    if(url===`/api/founder/broadcast/${entry.job_id}`)return {id:entry.job_id,status:state,audience:3,sent:3,blocked:0,failed:0,
      pending:0,source:null,manual:null,author:null,text:null,recipients_known:true,resumable:false,resume_count:0,throttled_until:null};
    throw new Error('unexpected '+url);
  });
  await settle();
  assert.equal(h.$('broadcast-history-note').hidden,false);
  assert.equal(h.$('broadcast-history-note').textContent,
    'Последние 30 рассылок через эту панель. Получатели по статусам и досылка появятся после обновления бота.');
  assert.equal(h.$('broadcast-history-note').classList.contains('is-error'),false);
  assert.equal(h.$('broadcast-history-kinds').hidden,true);
  const [card]=byClass(h.$('broadcast-history-list'),'broadcast-job');
  assert.equal(text(byClass(card,'broadcast-job-source')[0]),'Из панели');
  assert.match(text(card),/Автор: Карина/);
  assert.match(text(card),/Итог бота на 17\.10 09:06/);
  assert.equal(byText(card,'Получатели'),undefined);
  assert.equal(byText(card,'Дослать'),undefined);
  assert.doesNotMatch(text(card),/Получатели этой рассылки не записаны/);
  // Повтор текста ловится и по журналу панели.
  assert.equal(h.context.FounderBroadcastHistory.jobs()[0].text,'Завтра плов с 12:00');
  // Идущую опрашиваем по id задачи бота; текст и автор не теряются.
  state='completed';await h.timers.at(-1)();await settle();
  assert.match(text(byClass(card,'broadcast-job-status')[0]),/^Готово$/);
  assert.match(text(card),/Завтра плов с 12:00/);
  assert.match(text(card),/Автор: Карина/);
  assert.equal(byText(card,'Получатели'),undefined,'карточка журнала панели — без получателей');
});

test('журнал: ручные и автоматические различимы, досылка с подтверждением и одним запросом',async()=>{
  const interrupted=job({status:'interrupted',sent:13,blocked:1,failed:1,pending:18,resumable:true,
    throttled_until:new Date(Date.now()+10*60000).toISOString()});
  const auto=job({id:'auto_reminder_2026_10_16_1900',source:'auto',manual:false,author:null,text:'Напоминаем о брони'});
  const legacy=job({id:'5b1f0c7e-2c11-4c55-9a5e-1f0b4d7e9c01',source:null,manual:null,author:null,text:null,recipients_known:false});
  let resumed=0,state='queued';
  const h=page((url,options)=>{
    if(url.startsWith('/api/founder/broadcast/jobs?'))return {supported:true,jobs:[interrupted,auto,legacy],total:3,next_cursor:null};
    if(url.endsWith('/resume')){resumed+=1;return {job:{...interrupted,status:'queued',resumable:false,throttled_until:null},started:true}}
    if(url.includes('/deliveries'))return {job_id:interrupted.id,recipients_known:true,total:2,next_offset:null,recipients:[
      {id:'A'.repeat(32),name:'Азиза',phone:'••• 12 34',status:'sent',error:null,at:'2026-10-17T07:58:00Z'},
      {id:'B'.repeat(32),name:'',phone:'••• 45 67',status:'failed',error:'delivery_unknown',at:'2026-10-17T07:58:02Z'}]};
    if(url===`/api/founder/broadcast/${interrupted.id}`)return {...interrupted,status:state,resumable:false,pending:state==='completed'?0:18,
      sent:state==='completed'?31:13,throttled_until:null};
    throw new Error('unexpected '+url+' '+options.method);
  });
  await settle();
  const list=h.$('broadcast-history-list');
  const cards=byClass(list,'broadcast-job');
  assert.equal(cards.length,3);
  assert.deepEqual(cards.map(card=>text(byClass(card,'broadcast-job-source')[0])),['Из панели','Автоматическая','Источник не записан']);
  assert.deepEqual(cards.map(card=>card.classList.contains('is-auto')),[false,true,false]);
  // Текст гостя и имя автора — данные: переводчик их не трогает.
  assert.equal(byClass(cards[0],'broadcast-job-text')[0].getAttribute('data-i18n'),'off');
  assert.equal(find(byClass(cards[0],'broadcast-job-author')[0],item=>item.tagName==='B').getAttribute('data-i18n'),'off');
  assert.match(text(cards[0]),/Telegram просит подождать до \d\d:\d\d/);
  assert.match(text(cards[0]),/Рассылку прервал перезапуск бота/);
  assert.match(text(cards[2]),/Текст не сохранился/);
  assert.match(text(cards[2]),/Получатели этой рассылки не записаны\./);
  assert.equal(byText(cards[2],'Получатели'),undefined,'без записанных получателей кнопки нет');
  assert.equal(byText(cards[1],'Дослать'),undefined,'досылать можно только то, что бот назвал досылаемым');

  // «Дослать оставшимся: 18» — сначала подтверждение, запроса ещё нет.
  await byText(cards[0],'Дослать оставшимся: 18').fire('click');
  const confirm=byClass(cards[0],'broadcast-job-confirm')[0];
  assert.equal(confirm.hidden,false);
  assert.match(text(confirm),/Сообщение уйдёт 18 гостям из очереди/);
  assert.equal(resumed,0);
  await byText(confirm,'Вернуться').fire('click');
  assert.equal(confirm.hidden,true);
  assert.equal(resumed,0,'«Вернуться» ничего не шлёт');

  await byText(cards[0],'Дослать оставшимся: 18').fire('click');
  const send=byText(confirm,'Дослать');
  const pending=send.fire('click');
  assert.equal(send.disabled,true,'второе нажатие до ответа заблокировано');
  await pending;await settle();
  assert.equal(resumed,1);
  assert.deepEqual(h.requests.filter(([method])=>method==='POST'),[['POST',`/api/founder/broadcast/jobs/${interrupted.id}/resume`]]);
  assert.equal(confirm.hidden,true);
  assert.match(text(byClass(cards[0],'broadcast-job-message')[0]),/^Досылка запущена/);
  assert.equal(byText(cards[0],'Дослать оставшимся'),undefined);
  assert.match(text(cards[0]),/В очереди/);

  // Идущую досылку опрашиваем по id, пока не закончится.
  assert.ok(h.timers.length>0);
  state='completed';await h.timers.at(-1)();await settle();
  assert.match(text(byClass(cards[0],'broadcast-job-status')[0]),/^Готово$/);
  const polls=h.requests.filter(([,url])=>url===`/api/founder/broadcast/${interrupted.id}`).length;
  assert.equal(polls,1);

  // Получатели: все, затем «Не дошло»; имя пустое — «Без имени», телефон маской.
  await byText(cards[0],'Получатели').fire('click');await settle();
  assert.ok(h.requests.some(([,url])=>url===`/api/founder/broadcast/jobs/${interrupted.id}/deliveries?status=all&offset=0`));
  const detail=byClass(cards[0],'broadcast-job-detail')[0];
  assert.equal(detail.hidden,false);
  const rows=byClass(detail,'broadcast-delivery');
  assert.equal(rows.length,2);
  assert.match(text(rows[0]),/Азиза••• 12 34Отправлено/);
  assert.match(text(rows[1]),/Без имени••• 45 67Не дошло/);
  assert.match(text(rows[1]),/Бот перезапустился во время отправки/);
  assert.equal(find(rows[0],item=>item.tagName==='STRONG').getAttribute('data-i18n'),'off');
  await byText(detail,'Не дошло').fire('click');await settle();
  assert.ok(h.requests.some(([,url])=>url===`/api/founder/broadcast/jobs/${interrupted.id}/deliveries?status=failed&offset=0`));
});

test('фильтр «Автоматические» спрашивает у сервера только автоматические',async()=>{
  const h=page(url=>({supported:true,jobs:url.includes('kind=auto')?[]:[job()],total:1,next_cursor:null}));await settle();
  await h.kinds[2].fire('click');await settle();
  assert.ok(h.requests.some(([,url])=>url==='/api/founder/broadcast/jobs?kind=auto'));
  assert.equal(h.kinds[2].classList.contains('is-active'),true);
  assert.equal(h.kinds[2].getAttribute('aria-pressed'),'true');
  assert.match(text(h.$('broadcast-history-list')),/Автоматических рассылок пока не было\./);
});

test('ошибка журнала видна, страница работает дальше',async()=>{
  const h=page(()=>new Error('Рассылка через booking-бот ещё не настроена.'));await settle();
  assert.equal(h.$('broadcast-history-note').textContent,'Рассылка через booking-бот ещё не настроена.');
  assert.equal(h.$('broadcast-history-note').classList.contains('is-error'),true);
});

test('узбекский: подписи журнала и шаблоны с данными',async()=>{
  globalThis.document={readyState:'loading',addEventListener(){}};
  await import('../../retro/static/i18n-uz.js');
  await import('../../retro/static/i18n.js');
  const {translate}=globalThis.RetroI18n;
  assert.equal(translate('Журнал рассылок'),'Tarqatmalar jurnali');
  assert.equal(translate('Команда в боте'),'Botdagi buyruq');
  assert.equal(translate('Идёт: 12 из 33'),'Ketmoqda: 12 / 33');
  assert.equal(translate('Дослать оставшимся: 18'),'Qolganlarga yuborish: 18');
  assert.equal(translate('Telegram просит подождать до 14:45'),"Telegram 14:45 gacha kutishni so'rayapti");
  assert.equal(translate('Такой же текст уже рассылали 17.10 12:30 · Команда в боте.'),
    'Xuddi shu matn 17.10 12:30 da yuborilgan · Botdagi buyruq.');
  assert.match(translate(logic.resumeConfirm(18)),/^Xabar navbatdagi 18 mehmonga yuboriladi\./);
  // Каждая подпись блока — без русских букв в переводе.
  for(const label of ['Из панели','Автоматическая','Источник не записан','Прервана','Отправлено','Заблокировали','Не дошло',
    'В очереди','Получатели','Скрыть получателей','Заблокировал бота','Отправляется','Автор:','Текст не сохранился',
    'Нет связи с Telegram','Гость отписался до отправки','Бот перезапустился во время отправки: сообщение могло дойти',
    'Рассылку прервал перезапуск бота. Сама она не продолжится.','Получатели этой рассылки не записаны.',
    '«Отправлено» значит, что Telegram принял сообщение. Прочитал ли его гость, бот не знает.','Таких получателей нет.',
    'Досылка запущена: сообщение получат только те, кому оно ещё не уходило.','Досылать некому: всем получателям уже отправляли.',
    'Автоматических рассылок пока не было.','Ручных рассылок пока не было.','Рассылок ещё не было.','Показать ещё',
    'Журнал рассылок','Обновить','Итог бота на 17.10 14:06','Не удалось загрузить журнал рассылок.',
    'Последние 30 рассылок через эту панель. Получатели по статусам и досылка появятся после обновления бота.',
    'Ручные запускает человек: из панели или командой в боте. Автоматические уходят по расписанию без нажатия.',
    'Дослано 17.10 14:00','Досылали: 2 · последний раз 17.10 14:00','Эту рассылку дослать нельзя: бот не сохранил её получателей.']){
    const uz=translate(label);
    assert.ok(uz&&!/[А-Яа-яЁё]/.test(uz),`${label} → ${uz}`);
  }
});
