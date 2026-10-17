(function(root){
  const terminal=new Set(['completed','failed','interrupted']);
  function validateText(value){
    const text=String(value||'').trim();
    if(!text)return {ok:false,error:'Введите текст рассылки.'};
    if(text.length>4096)return {ok:false,error:'Текст не должен превышать 4096 символов.'};
    return {ok:true,text};
  }
  function operationId(cryptoApi){
    if(cryptoApi?.randomUUID)return cryptoApi.randomUUID();
    const bytes=new Uint8Array(16);cryptoApi.getRandomValues(bytes);bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
    const hex=[...bytes].map(value=>value.toString(16).padStart(2,'0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }
  function validateRecipients(values,available){
    if(!Array.isArray(values)||!Array.isArray(available))return {ok:false,error:'Не удалось проверить получателей.'};
    const ids=[...new Set(values)];const known=new Set(available);
    if(!ids.length)return {ok:false,error:'Выберите хотя бы одного получателя.'};
    if(ids.length>2000||ids.some(id=>typeof id!=='string'||!known.has(id)))return {ok:false,error:'Список получателей изменился. Обновите выбор.'};
    return {ok:true,ids};
  }
  function statusText(job){
    if(job.status==='queued')return `Рассылка поставлена в очередь · получателей: ${job.audience}`;
    if(job.status==='running')return `Отправляется: ${job.sent+job.blocked+job.failed} из ${job.audience}`;
    if(job.status==='completed')return `Готово · отправлено ${job.sent}, заблокировали бота ${job.blocked}, ошибок ${job.failed}`;
    if(job.status==='interrupted')return 'Рассылка была прервана перезапуском сервиса. Повтор автоматически не выполнялся.';
    return 'Рассылка завершилась с ошибкой.';
  }
  /** Отпечаток черновика: тот же текст тем же получателям — тот же ключ.
   *  Пока ключ не изменился, повтор «Отправить» идёт с прежним operation_id,
   *  и бот не отправит сообщение второй раз. Сам текст в ключ не кладём. */
  function draftKey(text,ids){
    const value=String(text||'').trim()+'\n'+[...new Set(ids||[])].sort().join(',');
    let hash=0x811c9dc5;
    for(let index=0;index<value.length;index+=1){hash^=value.charCodeAt(index);hash=Math.imul(hash,0x01000193)}
    return (hash>>>0).toString(16).padStart(8,'0')+'-'+value.length;
  }
  function plural(count,one,few,many){
    const tens=count%100,units=count%10;
    if(units===1&&tens!==11)return one;
    if(units>=2&&units<=4&&(tens<12||tens>14))return few;
    return many;
  }
  function agoText(milliseconds){
    const minutes=Math.max(0,Math.floor(milliseconds/60000));
    if(minutes<1)return 'только что';
    if(minutes<60)return `${minutes} ${plural(minutes,'минуту','минуты','минут')} назад`;
    const hours=Math.floor(minutes/60);
    if(hours<24)return `${hours} ${plural(hours,'час','часа','часов')} назад`;
    const days=Math.floor(hours/24);
    return `${days} ${plural(days,'день','дня','дней')} назад`;
  }
  function repeatWarning(sentAt,now){return `Этот текст уже отправлен этим получателям ${agoText(now-sentAt)}.`}
  /** Журнал отправленного в этом браузере: ключ → когда ушло. Неделя, не дольше. */
  function rememberSent(history,key,at,keep=7*864e5){
    const next={};
    Object.entries(history||{}).forEach(([item,value])=>{if(Number(value?.at)>at-keep)next[item]=value});
    next[key]={at};
    return next;
  }

  /* ── Журнал рассылок (ТЗ 16.10, п. 5.3; T-504) ───────────────────────── */
  const SOURCES={dashboard:'Из панели',tg_admin:'Команда в боте',auto:'Автоматическая'};
  const JOB_STATUSES={queued:'В очереди',running:'Идёт',completed:'Готово',failed:'Ошибка',interrupted:'Прервана'};
  const DELIVERY_STATUSES={pending:'В очереди',sending:'Отправляется',sent:'Отправлено',blocked:'Заблокировал бота',failed:'Не дошло'};
  // Коды бота вместо текста Telegram (контракт T-503).
  const DELIVERY_ERRORS={
    network_error:'Нет связи с Telegram',
    delivery_unknown:'Бот перезапустился во время отправки: сообщение могло дойти',
    unsubscribed:'Гость отписался до отправки',
  };
  /** Ручная — запустил человек (из панели или командой в боте). У старых
   *  заданий источника нет: считаем ручной, автоматических тогда не было. */
  const isManual=job=>job?.source?job.source!=='auto':job?.manual!==false;
  function sourceLabel(job){
    if(SOURCES[job?.source])return SOURCES[job.source];
    return job?.manual===false?SOURCES.auto:'Источник не записан';
  }
  function jobStatusText(job){
    if(job.status==='running')return `Идёт: ${job.sent+job.blocked+job.failed} из ${job.audience}`;
    return JOB_STATUSES[job.status]||'Неизвестно';
  }
  /** Тон плашки статуса: идёт, готово, сбой. */
  function jobTone(job){
    if(job.status==='queued'||job.status==='running')return 'running';
    return job.status==='completed'?'done':'error';
  }
  /** Итоги задания в порядке экрана. «В очереди» — кому ещё не отправляли. */
  function jobCounts(job){
    const pending=Number.isInteger(job.pending)?job.pending:Math.max(0,job.audience-job.sent-job.blocked-job.failed);
    return [['sent','Отправлено',job.sent],['blocked','Заблокировали',job.blocked],['failed','Не дошло',job.failed],['queued','В очереди',pending]];
  }
  // Ташкент круглый год UTC+5: без летнего времени, без зависимости от
  // часового пояса браузера.
  const TASHKENT_MS=5*36e5;
  const pad=value=>String(value).padStart(2,'0');
  function tashkent(value){
    const ms=typeof value==='number'?value:Date.parse(value);
    if(!Number.isFinite(ms))return null;
    const day=new Date(ms+TASHKENT_MS);
    return {year:day.getUTCFullYear(),date:`${pad(day.getUTCDate())}.${pad(day.getUTCMonth()+1)}`,time:`${pad(day.getUTCHours())}:${pad(day.getUTCMinutes())}`};
  }
  /** «17.10 14:05»; год — только если не текущий. */
  function momentText(value,now){
    const at=tashkent(value);if(!at)return '—';
    const year=tashkent(now)?.year;
    return `${at.date}${at.year!==year?'.'+at.year:''} ${at.time}`;
  }
  /** Telegram ответил 429, бот ждёт. Прошедшее время не показываем. */
  function throttleText(job,now){
    const until=Date.parse(job?.throttled_until||'');
    if(!Number.isFinite(until)||until<=now)return '';
    return `Telegram просит подождать до ${tashkent(until).time}`;
  }
  function resumeText(job,now){
    if(!job?.resume_count)return '';
    const at=job.resumed_at?momentText(job.resumed_at,now):'';
    return job.resume_count===1?`Дослано ${at}`.trim():`Досылали: ${job.resume_count} · последний раз ${at}`.trim();
  }
  /** Начало текста одной строкой. */
  function preview(text,limit=140){
    const line=String(text||'').replace(/\s+/g,' ').trim();
    return line.length>limit?line.slice(0,limit-1).trimEnd()+'…':line;
  }
  const deliveryStatusText=status=>DELIVERY_STATUSES[status]||status;
  /** Пояснение к получателю: код бота — по-русски, текст Telegram — как есть (raw). */
  function deliveryNote(row){
    if(!row?.error||row.status==='blocked'||row.status==='sent')return null;
    if(DELIVERY_ERRORS[row.error])return {text:DELIVERY_ERRORS[row.error],raw:false};
    return {text:row.error,raw:true};
  }
  const resumeLabel=count=>`Дослать оставшимся: ${count}`;
  const resumeConfirm=count=>`Сообщение уйдёт ${count} ${plural(count,'гостю','гостям','гостям')} из очереди. Тем, кто уже получил его, заблокировал бота или остался с ошибкой, повторно ничего не уйдёт.`;
  /** Такой же текст уже уходил за последние сутки — из панели, командой в
   *  боте или с другого устройства. Журнал браузера этого не знает. */
  function historyTwin(jobs,text,now,within=864e5){
    const body=String(text||'').trim();if(!body)return null;
    return (jobs||[]).find(job=>{
      const at=Date.parse(job?.created_at||'');
      return typeof job?.text==='string'&&job.text.trim()===body&&Number.isFinite(at)&&now-at<=within
        &&(job.sent>0||job.status==='queued'||job.status==='running');
    })||null;
  }
  const twinWarning=(job,now)=>`Такой же текст уже рассылали ${momentText(job.created_at,now)} · ${sourceLabel(job)}.`;

  root.FounderBroadcastLogic={validateText,validateRecipients,operationId,statusText,draftKey,agoText,repeatWarning,
    rememberSent,isTerminal:status=>terminal.has(status),
    isManual,sourceLabel,jobStatusText,jobTone,jobCounts,momentText,throttleText,resumeText,preview,
    deliveryStatusText,deliveryNote,resumeLabel,resumeConfirm,historyTwin,twinWarning};
})(typeof globalThis==='undefined'?window:globalThis);
