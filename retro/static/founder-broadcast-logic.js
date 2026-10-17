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
  /* ── Журнал рассылок (ТЗ 16.10, п. 5.3) ── */
  /** Ручная — с этого экрана; «автоматически» — задел под автоотправку. */
  function kindLabel(kind){return kind==='auto'?'автоматически':'вручную'}
  /** Превью текста одной строкой: переносы — в пробелы, длинное — с «…». */
  function journalPreview(text,max=90){
    const line=String(text||'').replace(/\s+/g,' ').trim();
    return line.length>max?line.slice(0,max-1).trimEnd()+'…':line;
  }
  /** Время журнала — как записал сервер (Ташкент), без пересчёта браузером. */
  function journalWhen(value,withYear=true){
    const match=/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(value||''));
    if(!match)return '—';
    return `${match[3]}.${match[2]}${withYear?'.'+match[1]:''} ${match[4]}:${match[5]}`;
  }
  /** Последний известный итог рассылки — те счётчики, что отдал бот. */
  function journalStatus(entry){
    const done=(entry.sent||0)+(entry.blocked||0)+(entry.failed||0),total=entry.audience||entry.recipients||0;
    if(entry.status==='queued')return `в очереди · получателей: ${total}`;
    if(entry.status==='running')return `отправляется: ${done} из ${total}`;
    if(entry.status==='completed')return `отправлено ${entry.sent||0} · заблокировали бота ${entry.blocked||0} · ошибок ${entry.failed||0}`;
    if(entry.status==='interrupted')return `прервана · обработано ${done} из ${total}`;
    if(entry.status==='failed')return `ошибка · обработано ${done} из ${total}`;
    return 'статус неизвестен';
  }
  function journalState(status){
    if(status==='completed')return 'done';
    if(status==='failed'||status==='interrupted')return 'error';
    return status==='queued'||status==='running'?'running':'unknown';
  }
  root.FounderBroadcastLogic={validateText,validateRecipients,operationId,statusText,draftKey,agoText,repeatWarning,
    rememberSent,kindLabel,journalPreview,journalWhen,journalStatus,journalState,isTerminal:status=>terminal.has(status)};
})(typeof globalThis==='undefined'?window:globalThis);
