(function(){
  const $=id=>document.getElementById(id);
  const logic=globalThis.FounderBroadcastLogic;
  let audience=null,pollTimer=null,pendingRecipientIds=[];
  // operation_id живёт, пока не изменились текст и получатели: двойное нажатие,
  // повтор после обрыва связи или новая отправка того же черновика уходят с
  // тем же id, и бот второй раз не отправляет. Новый id — только при новом
  // черновике или по кнопке «Отправить ещё раз».
  let operation=null;
  const SENT_KEY='retro:founder-broadcast-sent';
  // Журнал отправленного — в браузере учредителя; без хранилища (приватное
  // окно) — хотя бы до перезагрузки страницы.
  let sentMemory={};
  const readSent=()=>{try{const stored=JSON.parse(localStorage.getItem(SENT_KEY)||'null');if(stored&&typeof stored==='object')return {...sentMemory,...stored}}catch{}return sentMemory};
  const writeSent=value=>{sentMemory=value;try{localStorage.setItem(SENT_KEY,JSON.stringify(value))}catch{}};
  const selected=new Set();
  const setStatus=(text,error=false)=>{const node=$('broadcast-status');node.textContent=text;node.classList.toggle('is-error',error)};
  const api=async(url,options)=>RetroState.responseJson(await fetch(url,options),'Не удалось выполнить запрос рассылки.');
  const Busy=globalThis.RetroBusy;
  /** Итог рассылки на месте: крутится, пока идёт; ✓ или ошибка — по статусу. */
  function resultState(kind){const node=$('broadcast-result');node.classList.toggle('is-running',kind==='running');node.classList.toggle('is-done',kind==='done');node.classList.toggle('is-error',kind==='error')}
  function audienceSkeleton(){const list=$('broadcast-recipient-list');list.replaceChildren(...Array.from({length:4},()=>{const row=document.createElement('div');row.className='broadcast-recipient fa-skel-recipient';row.setAttribute('aria-hidden','true');const box=document.createElement('span');box.className='rm-skel';const body=document.createElement('span');const a=document.createElement('span'),b=document.createElement('span');a.className=b.className='rm-skel';body.append(a,b);row.append(box,body);return row}));$('broadcast-audience').replaceChildren(Object.assign(document.createElement('span'),{className:'rm-skel is-val'}));$('broadcast-status').textContent=''}
  const setBusy=value=>{
    ['broadcast-prepare','broadcast-confirm-send','broadcast-search','broadcast-select-all','broadcast-clear-all','broadcast-text']
      .forEach(id=>$(id).disabled=value);
    $('broadcast-recipient-list').querySelectorAll('input').forEach(node=>{node.disabled=value});
    if(!value)updateSelected();
  };

  function updateSelected(){
    $('broadcast-selected').textContent=String(selected.size);
    $('broadcast-prepare').disabled=!selected.size;
    // Выбирать некого / снимать нечего — кнопка выглядит неактивной, а не «молчит».
    $('broadcast-select-all').disabled=!(audience?.recipients||[]).length||selected.size===(audience?.recipients||[]).length;
    $('broadcast-clear-all').disabled=!selected.size;
  }

  function renderRecipients(){
    const list=$('broadcast-recipient-list');list.replaceChildren();
    const query=$('broadcast-search').value.trim().toLocaleLowerCase('ru');
    const rows=(audience?.recipients||[]).filter(row=>
      !query||row.name.toLocaleLowerCase('ru').includes(query)||row.phone.includes(query));
    rows.forEach(row=>{
      const label=document.createElement('label');label.className='broadcast-recipient';
      const input=document.createElement('input');input.type='checkbox';input.checked=selected.has(row.id);input.value=row.id;
      input.addEventListener('change',()=>{input.checked?selected.add(row.id):selected.delete(row.id);updateSelected()});
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
  function showJob(job){const failed=job.status==='failed'||job.status==='interrupted';setStatus(logic.statusText(job),failed);$('broadcast-result').hidden=false;$('broadcast-result').textContent=logic.statusText(job);resultState(failed?'error':logic.isTerminal(job.status)?'done':'running')}
  /* Журнал рассылок (ТЗ 16.10, п. 5.3): что ушло через панель, кто отправил,
     вручную или автоматически, и последний известный итог бота. */
  function journalRow(entry){
    const item=document.createElement('li');item.className='broadcast-journal-item is-'+logic.journalState(entry.status);
    const meta=document.createElement('div');meta.className='broadcast-journal-meta';
    const when=document.createElement('time');when.dateTime=entry.sent_at||'';when.textContent=logic.journalWhen(entry.sent_at);
    const who=document.createElement('span');who.textContent=entry.sent_by||'—';
    const kind=document.createElement('span');kind.className='broadcast-kind is-'+(entry.kind==='auto'?'auto':'manual');kind.textContent=logic.kindLabel(entry.kind);
    const count=document.createElement('span');count.textContent=`гостей: ${entry.recipients}`;
    meta.append(when,who,kind,count);
    const text=document.createElement('p');text.className='broadcast-journal-text';text.textContent=logic.journalPreview(entry.text);text.title=entry.text||'';
    const status=document.createElement('div');status.className='broadcast-journal-status';
    const line=document.createElement('span');line.textContent=logic.journalStatus(entry);status.append(line);
    if(entry.status_at){const at=document.createElement('small');at.textContent='на '+logic.journalWhen(entry.status_at,false);status.append(at)}
    item.append(meta,text,status);return item;
  }
  async function loadJournal(){
    const error=$('broadcast-journal-error');
    try{
      const data=await api('/api/founder/broadcast/journal',{retroBusy:false});
      const entries=Array.isArray(data?.entries)?data.entries:[];
      $('broadcast-journal-list').replaceChildren(...entries.map(journalRow));
      $('broadcast-journal-empty').hidden=entries.length>0;error.hidden=true;
    }catch(problem){error.textContent=problem.message;error.hidden=false}
  }
  async function poll(id){
    clearTimeout(pollTimer);
    try{
      // Опрос каждую секунду — фоновый: верхнюю полосу не зажигает.
      const job=await api('/api/founder/broadcast/'+encodeURIComponent(id),{retroBusy:false});showJob(job);
      if(logic.isTerminal(job.status)){setBusy(false);loadJournal();await loadAudience().catch(()=>{});return}
      pollTimer=setTimeout(()=>poll(id),1000);
    }catch(error){setStatus(error.message,true);resultState('error');setBusy(false)}
  }

  $('broadcast-search').addEventListener('input',renderRecipients);
  $('broadcast-select-all').addEventListener('click',()=>{(audience?.recipients||[]).forEach(row=>selected.add(row.id));renderRecipients()});
  $('broadcast-clear-all').addEventListener('click',()=>{selected.clear();renderRecipients()});
  $('broadcast-text').addEventListener('input',()=>{$('broadcast-count').textContent=String($('broadcast-text').value.length)});
  $('broadcast-form').addEventListener('submit',async event=>{
    event.preventDefault();const checked=logic.validateText($('broadcast-text').value);
    if(!checked.ok){setStatus(checked.error,true);$('broadcast-text').focus();return}
    // «Предпросмотр» перечитывает аудиторию: кнопка крутится, пока она идёт.
    const fresh=loadAudience();
    if(Busy)Busy.button($('broadcast-prepare'),fresh.catch(()=>false),{done:false});
    try{
      const latest=await fresh;
      const recipients=logic.validateRecipients([...selected],latest.recipients.map(row=>row.id));
      if(!recipients.ok){setStatus(recipients.error,true);$('broadcast-search').focus();return}
      pendingRecipientIds=recipients.ids;$('broadcast-preview').textContent=checked.text;
      $('broadcast-confirm-count').textContent=String(pendingRecipientIds.length);
      // Тот же текст тем же гостям уже уходил: предупреждаем и просим нажать
      // другую кнопку, чтобы повтор не случился по привычке.
      const sent=readSent()[logic.draftKey(checked.text,pendingRecipientIds)],repeat=$('broadcast-repeat');
      repeat.hidden=!sent;repeat.textContent=sent?logic.repeatWarning(Number(sent.at),Date.now()):'';
      $('broadcast-confirm-send').textContent=sent?'Отправить ещё раз':'Отправить';
      $('broadcast-confirm').hidden=false;
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
    const key=logic.draftKey(checked.text,recipients.ids);
    const again=Boolean(readSent()[key])&&operation?.key===key&&operation.sent;
    if(!operation||operation.key!==key||again)operation={key,id:logic.operationId(globalThis.crypto),sent:false};
    const operationId=operation.id;
    // Окно подтверждения остаётся, пока сервер не принял рассылку: «Отправить»
    // крутится; дальше — ход рассылки в строке итога.
    const started=api('/api/founder/broadcast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation_id:operationId,text:checked.text,recipient_ids:recipients.ids})});
    if(Busy)Busy.button($('broadcast-confirm-send'),started,{done:false});
    try{
      const job=await started;
      if(operation?.id===operationId)operation.sent=true;
      writeSent(logic.rememberSent(readSent(),key,Date.now()));
      setBusy(true);$('broadcast-confirm').hidden=true;showJob(job);loadJournal();poll(job.id);
    }catch(error){setStatus(error.message,true);$('broadcast-result').hidden=false;$('broadcast-result').textContent=error.message;resultState('error');$('broadcast-confirm').hidden=true}
  });
  $('broadcast-journal-refresh').addEventListener('click',()=>{const done=loadJournal();if(Busy)Busy.button($('broadcast-journal-refresh'),done,{done:false})});
  audienceSkeleton();
  loadAudience().catch(()=>{});
  loadJournal();
})();
