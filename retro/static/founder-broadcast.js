(function(){
  const $=id=>document.getElementById(id);
  const logic=globalThis.FounderBroadcastLogic;
  let audience=null,operationId=null,pollTimer=null;
  const setStatus=(text,error=false)=>{const node=$('broadcast-status');node.textContent=text;node.classList.toggle('is-error',error)};
  const api=async(url,options)=>RetroState.responseJson(await fetch(url,options),'Не удалось выполнить запрос рассылки.');
  const setBusy=value=>{['broadcast-prepare','broadcast-confirm-send'].forEach(id=>$(id).disabled=value);$('broadcast-text').disabled=value};

  async function loadAudience(){
    try{
      audience=await api('/api/founder/broadcast/audience');
      $('broadcast-audience').textContent=String(audience.subscribers);
      $('broadcast-profiles').textContent=String(audience.profiles);
      setStatus(audience.subscribers?'Готово к подготовке рассылки.':'Нет доступных получателей.');
      return audience;
    }catch(error){audience=null;$('broadcast-audience').textContent='—';$('broadcast-profiles').textContent='—';setStatus(error.message,true);throw error}
  }

  function closeConfirm(){const panel=$('broadcast-confirm');panel.hidden=true;$('broadcast-prepare').focus()}
  function showJob(job){setStatus(logic.statusText(job),job.status==='failed'||job.status==='interrupted');$('broadcast-result').hidden=false;$('broadcast-result').textContent=logic.statusText(job)}
  async function poll(id){
    clearTimeout(pollTimer);
    try{
      const job=await api('/api/founder/broadcast/'+encodeURIComponent(id));showJob(job);
      if(logic.isTerminal(job.status)){
        setBusy(false);operationId=null;await loadAudience().catch(()=>{});return;
      }
      pollTimer=setTimeout(()=>poll(id),1000);
    }catch(error){setStatus(error.message,true);setBusy(false)}
  }

  $('broadcast-text').addEventListener('input',()=>{$('broadcast-count').textContent=String($('broadcast-text').value.length)});
  $('broadcast-form').addEventListener('submit',async event=>{
    event.preventDefault();const checked=logic.validateText($('broadcast-text').value);
    if(!checked.ok){setStatus(checked.error,true);$('broadcast-text').focus();return}
    try{const latest=await loadAudience();if(!latest.subscribers){setStatus('Нет доступных получателей.',true);return}
      $('broadcast-preview').textContent=checked.text;$('broadcast-confirm-count').textContent=String(latest.subscribers);$('broadcast-confirm').hidden=false;$('broadcast-confirm-send').focus();setStatus('Проверьте текст и подтвердите отправку.');
    }catch{}
  });
  $('broadcast-confirm-cancel').addEventListener('click',closeConfirm);
  $('broadcast-confirm').addEventListener('keydown',event=>{
    if(event.key==='Escape'){event.preventDefault();closeConfirm();return}
    if(event.key==='Tab'){
      const first=$('broadcast-confirm-cancel'),last=$('broadcast-confirm-send');
      if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}
    }
  });
  $('broadcast-confirm-send').addEventListener('click',async()=>{
    const checked=logic.validateText($('broadcast-preview').textContent);if(!checked.ok)return;
    operationId=operationId||logic.operationId(globalThis.crypto);setBusy(true);$('broadcast-confirm').hidden=true;$('broadcast-result').hidden=false;$('broadcast-result').textContent='Запускаю рассылку…';
    try{
      const job=await api('/api/founder/broadcast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation_id:operationId,text:checked.text})});showJob(job);poll(job.id);
    }catch(error){setStatus(error.message,true);setBusy(false)}
  });
  loadAudience().catch(()=>{});
})();
