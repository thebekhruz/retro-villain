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
  function statusText(job){
    if(job.status==='queued')return `Рассылка поставлена в очередь · получателей: ${job.audience}`;
    if(job.status==='running')return `Отправляется: ${job.sent+job.blocked+job.failed} из ${job.audience}`;
    if(job.status==='completed')return `Готово · отправлено ${job.sent}, заблокировали бота ${job.blocked}, ошибок ${job.failed}`;
    if(job.status==='interrupted')return 'Рассылка была прервана перезапуском сервиса. Повтор автоматически не выполнялся.';
    return 'Рассылка завершилась с ошибкой.';
  }
  root.FounderBroadcastLogic={validateText,operationId,statusText,isTerminal:status=>terminal.has(status)};
})(typeof globalThis==='undefined'?window:globalThis);
