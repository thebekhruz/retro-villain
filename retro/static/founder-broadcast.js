(function(){
  const $=id=>document.getElementById(id);
  const logic=globalThis.FounderBroadcastLogic;
  let audience=null,operationId=null,pollTimer=null,pendingRecipientIds=[];
  const selected=new Set();
  const setStatus=(text,error=false)=>{const node=$('broadcast-status');node.textContent=text;node.classList.toggle('is-error',error)};
  const api=async(url,options)=>RetroState.responseJson(await fetch(url,options),'Не удалось выполнить запрос рассылки.');
  const setBusy=value=>{
    ['broadcast-prepare','broadcast-confirm-send','broadcast-search','broadcast-select-all','broadcast-clear-all','broadcast-text']
      .forEach(id=>$(id).disabled=value);
    $('broadcast-recipient-list').querySelectorAll('input').forEach(node=>{node.disabled=value});
  };
  const draftChanged=()=>{operationId=null};

  function updateSelected(){
    $('broadcast-selected').textContent=String(selected.size);
    $('broadcast-prepare').disabled=!selected.size;
  }

  function renderRecipients(){
    const list=$('broadcast-recipient-list');list.replaceChildren();
    const query=$('broadcast-search').value.trim().toLocaleLowerCase('ru');
    const rows=(audience?.recipients||[]).filter(row=>
      !query||row.name.toLocaleLowerCase('ru').includes(query)||row.phone.includes(query));
    rows.forEach(row=>{
      const label=document.createElement('label');label.className='broadcast-recipient';
      const input=document.createElement('input');input.type='checkbox';input.checked=selected.has(row.id);input.value=row.id;
      input.addEventListener('change',()=>{input.checked?selected.add(row.id):selected.delete(row.id);draftChanged();updateSelected()});
      const body=document.createElement('span');const name=document.createElement('strong');name.textContent=row.name;
      const phone=document.createElement('small');phone.textContent=row.phone;body.append(name,phone);label.append(input,body);list.append(label);
    });
    $('broadcast-recipient-empty').hidden=rows.length>0;
    updateSelected();
  }

  async function loadAudience(){
    try{
      audience=await api('/api/founder/broadcast/audience');
      const available=new Set(audience.recipients.map(row=>row.id));
      for(const id of selected)if(!available.has(id))selected.delete(id);
      $('broadcast-audience').textContent=String(audience.subscribers);
      $('broadcast-profiles').textContent=String(audience.profiles);renderRecipients();
      setStatus(audience.subscribers?'Выберите получателей и подготовьте рассылку.':'Нет доступных получателей.');
      return audience;
    }catch(error){audience=null;selected.clear();$('broadcast-audience').textContent='—';$('broadcast-profiles').textContent='—';renderRecipients();setStatus(error.message,true);throw error}
  }

  function closeConfirm(){const panel=$('broadcast-confirm');panel.hidden=true;$('broadcast-prepare').focus()}
  function showJob(job){setStatus(logic.statusText(job),job.status==='failed'||job.status==='interrupted');$('broadcast-result').hidden=false;$('broadcast-result').textContent=logic.statusText(job)}
  async function poll(id){
    clearTimeout(pollTimer);
    try{
      const job=await api('/api/founder/broadcast/'+encodeURIComponent(id));showJob(job);
      if(logic.isTerminal(job.status)){setBusy(false);operationId=null;await loadAudience().catch(()=>{});return}
      pollTimer=setTimeout(()=>poll(id),1000);
    }catch(error){setStatus(error.message,true);setBusy(false)}
  }

  $('broadcast-search').addEventListener('input',renderRecipients);
  $('broadcast-select-all').addEventListener('click',()=>{(audience?.recipients||[]).forEach(row=>selected.add(row.id));draftChanged();renderRecipients()});
  $('broadcast-clear-all').addEventListener('click',()=>{selected.clear();draftChanged();renderRecipients()});
  $('broadcast-text').addEventListener('input',()=>{draftChanged();$('broadcast-count').textContent=String($('broadcast-text').value.length)});
  $('broadcast-form').addEventListener('submit',async event=>{
    event.preventDefault();const checked=logic.validateText($('broadcast-text').value);
    if(!checked.ok){setStatus(checked.error,true);$('broadcast-text').focus();return}
    try{
      const latest=await loadAudience();
      const recipients=logic.validateRecipients([...selected],latest.recipients.map(row=>row.id));
      if(!recipients.ok){setStatus(recipients.error,true);$('broadcast-search').focus();return}
      pendingRecipientIds=recipients.ids;$('broadcast-preview').textContent=checked.text;
      $('broadcast-confirm-count').textContent=String(pendingRecipientIds.length);$('broadcast-confirm').hidden=false;
      $('broadcast-confirm-send').focus();setStatus('Проверьте текст, получателей и подтвердите отправку.');
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
    const checked=logic.validateText($('broadcast-preview').textContent);
    const recipients=logic.validateRecipients(pendingRecipientIds,audience?.recipients.map(row=>row.id)||[]);
    if(!checked.ok||!recipients.ok){setStatus(recipients.error||checked.error,true);return}
    operationId=operationId||logic.operationId(globalThis.crypto);setBusy(true);$('broadcast-confirm').hidden=true;
    $('broadcast-result').hidden=false;$('broadcast-result').textContent='Запускаю рассылку…';
    try{
      const job=await api('/api/founder/broadcast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation_id:operationId,text:checked.text,recipient_ids:recipients.ids})});showJob(job);poll(job.id);
    }catch(error){setStatus(error.message,true);setBusy(false)}
  });
  loadAudience().catch(()=>{});
})();
