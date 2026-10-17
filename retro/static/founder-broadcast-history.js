/* История рассылок (ТЗ 16.10, п. 5.3; T-504): журнал бота под формой рассылки.
   Ручные (из панели, командой в боте) и автоматические — разными плашками и
   фильтром. По нажатию — получатели со статусами; прерванную рассылку можно
   дослать тем, кому сообщение ещё не уходило, после подтверждения. Бот без
   журнала — спокойная пометка, форма рассылки работает как раньше. */
(function(){
  const $=id=>document.getElementById(id);
  const logic=globalThis.FounderBroadcastLogic;
  const Busy=globalThis.RetroBusy;
  const api=async(url,options)=>RetroState.responseJson(await fetch(url,options),'Не удалось загрузить историю рассылок.');
  const POLL_MS=3000;
  const FILTERS=[['all','Все'],['sent','Отправлено'],['blocked','Заблокировали'],['failed','Не дошло'],['queued','В очереди']];
  let kind='all',cursor=null,loaded=[],supported=true,pollTimer=null,generation=0;
  const cards=new Map();// id задания → {job, root, …}

  const el=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node};
  /** Данные гостя и автора — как пришли: переводчик их не трогает. */
  const data=(tag,className,text)=>{const node=el(tag,className,text);node.setAttribute('data-i18n','off');return node};
  const jobUrl=id=>'/api/founder/broadcast/jobs/'+encodeURIComponent(id);
  const setNote=(text,error=false)=>{const node=$('broadcast-history-note');node.hidden=!text;node.textContent=text||'';node.classList.toggle('is-error',error)};

  function skeleton(){
    $('broadcast-history-list').replaceChildren(...Array.from({length:3},()=>{
      const card=el('div','broadcast-job fa-skel-job');card.setAttribute('aria-hidden','true');
      for(let index=0;index<3;index+=1)card.append(el('span','rm-skel'));return card;
    }));
  }

  /* ── Карточка задания ───────────────────────────────────────────────── */
  function summary(entry){
    const {job}=entry,now=Date.now();
    entry.root.classList.toggle('is-auto',!logic.isManual(job));
    const head=el('div','broadcast-job-head');
    const source=el('span','broadcast-job-source',logic.sourceLabel(job));
    const time=el('time','broadcast-job-time',logic.momentText(job.created_at,now));if(job.created_at)time.dateTime=job.created_at;
    head.append(source,time);
    if(job.author){const author=el('span','broadcast-job-author');author.append(el('span','', 'Автор:'),' ',data('b','',job.author));head.append(author)}
    const status=el('span','source-status broadcast-job-status',logic.jobStatusText(job));
    status.classList.add('is-'+logic.jobTone(job));head.append(status);
    const text=job.text?data('p','broadcast-job-text',logic.preview(job.text)):el('p','broadcast-job-text is-missing','Текст не сохранился');
    const counts=el('dl','broadcast-job-counts');
    logic.jobCounts(job).forEach(([key,label,value])=>{const cell=el('div','is-'+key+(value>0?' has-value':''));cell.append(el('dt','',label),el('dd','',String(value)));counts.append(cell)});
    const notes=[];
    if(job.status==='interrupted')notes.push(el('p','broadcast-job-note','Рассылку прервал перезапуск бота. Сама она не продолжится.'));
    const throttle=logic.throttleText(job,now);if(throttle)notes.push(el('p','broadcast-job-note is-wait',throttle));
    const resumed=logic.resumeText(job,now);
    if(resumed){const note=el('p','broadcast-job-note');note.append(el('span','',resumed));if(job.resumed_by)note.append(' · ',data('span','',job.resumed_by));notes.push(note)}
    if(!job.recipients_known)notes.push(el('p','broadcast-job-note','Получатели этой рассылки не записаны.'));
    const actions=el('div','broadcast-job-actions');
    if(job.recipients_known){
      const open=el('button','button broadcast-job-open',entry.open?'Скрыть получателей':'Получатели');open.type='button';
      open.setAttribute('aria-expanded',String(Boolean(entry.open)));open.setAttribute('aria-controls',entry.detail.id);
      open.addEventListener('click',()=>toggle(entry));actions.append(open);
    }
    if(job.resumable){
      const resume=el('button','button primary broadcast-job-resume',logic.resumeLabel(job.pending));resume.type='button';
      resume.addEventListener('click',()=>askResume(entry));actions.append(resume);entry.resumeButton=resume;
    }else entry.resumeButton=null;
    // Опрос перерисовывает карточку: кнопка под фокусом остаётся под фокусом.
    const focused=entry.summary.contains(document.activeElement)?[...document.activeElement.classList].pop():null;
    entry.summary.replaceChildren(head,text,counts,...notes,...(actions.children.length?[actions]:[]));
    if(focused)entry.summary.querySelector('.'+focused)?.focus();
    if(!job.resumable)entry.confirm.hidden=true;
  }

  function create(job){
    const root=el('article','broadcast-job');root.dataset.job=job.id;
    const entry={job,root,summary:el('div','broadcast-job-summary'),confirm:el('div','broadcast-confirm broadcast-job-confirm'),
      message:el('p','broadcast-job-message'),detail:el('div','broadcast-job-detail'),open:false,filter:'all',rows:[],next:null};
    entry.detail.id='broadcast-job-detail-'+job.id;entry.detail.hidden=true;entry.confirm.hidden=true;entry.message.hidden=true;
    entry.message.setAttribute('role','status');entry.message.setAttribute('aria-live','polite');
    entry.confirm.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();entry.confirm.hidden=true;entry.resumeButton?.focus()}});
    root.append(entry.summary,entry.confirm,entry.message,entry.detail);
    cards.set(job.id,entry);return entry;
  }

  function showMessage(entry,text,error=false){entry.message.hidden=!text;entry.message.textContent=text||'';entry.message.classList.toggle('is-error',error)}

  /* ── Получатели ─────────────────────────────────────────────────────── */
  function deliveryRow(row,now){
    const item=el('li','broadcast-delivery is-'+row.status);
    const who=el('span','broadcast-delivery-who');
    who.append(row.name?data('strong','',row.name):el('strong','','Без имени'),data('small','',row.phone));
    const state=el('span','broadcast-delivery-state');
    state.append(el('b','',logic.deliveryStatusText(row.status)));
    if(row.at)state.append(el('small','',logic.momentText(row.at,now)));
    item.append(who,state);
    const note=logic.deliveryNote(row);
    if(note)item.append(note.raw?data('small','broadcast-delivery-error',note.text):el('small','broadcast-delivery-error',note.text));
    return item;
  }

  function renderDetail(entry){
    const {job}=entry,now=Date.now();
    const chips=el('div','quick-periods broadcast-delivery-filters');chips.setAttribute('role','group');chips.setAttribute('aria-label','Получатели по статусу');
    const totals=Object.fromEntries(logic.jobCounts(job).map(([key,,value])=>[key,value]));
    totals.all=job.audience;
    FILTERS.forEach(([value,label])=>{
      const chip=el('button','chip'+(entry.filter===value?' is-active':''));chip.type='button';chip.setAttribute('aria-pressed',String(entry.filter===value));
      chip.append(el('span','',label),' ',el('span','broadcast-chip-count',String(totals[value]??0)));
      chip.addEventListener('click',()=>{if(entry.filter===value)return;entry.filter=value;loadDeliveries(entry)});chips.append(chip);
    });
    const parts=[];
    if(job.text&&logic.preview(job.text)!==job.text.replace(/\s+/g,' ').trim())parts.push(data('pre','broadcast-job-full',job.text));
    parts.push(chips,el('p','founder-note broadcast-delivery-hint','«Отправлено» значит, что Telegram принял сообщение. Прочитал ли его гость, бот не знает.'));
    if(entry.error)parts.push(el('p','founder-message is-error',entry.error));
    else if(entry.loading&&!entry.rows.length){const list=el('ul','broadcast-delivery-list');list.setAttribute('aria-hidden','true');
      for(let index=0;index<3;index+=1){const row=el('li','broadcast-delivery fa-skel-delivery');row.append(el('span','rm-skel'),el('span','rm-skel'));list.append(row)}parts.push(list)}
    else if(!entry.rows.length)parts.push(el('p','founder-note','Таких получателей нет.'));
    else{const list=el('ul','broadcast-delivery-list');entry.rows.forEach(row=>list.append(deliveryRow(row,now)));parts.push(list)}
    if(entry.next!==null&&entry.rows.length){const more=el('button','button broadcast-more','Показать ещё');more.type='button';
      more.addEventListener('click',()=>{const work=loadDeliveries(entry,entry.next);if(Busy)Busy.button(more,work,{done:false})});parts.push(more)}
    entry.detail.replaceChildren(...parts);
  }

  async function loadDeliveries(entry,offset=0){
    const turn=(entry.turn||0)+1;entry.turn=turn;entry.error='';entry.loading=true;
    if(!offset){entry.rows=[];entry.next=null}
    renderDetail(entry);
    try{
      const page=await api(`${jobUrl(entry.job.id)}/deliveries?status=${encodeURIComponent(entry.filter)}&offset=${offset}`,{retroBusy:false});
      if(entry.turn!==turn)return;
      entry.rows=offset?[...entry.rows,...page.recipients]:page.recipients;entry.next=page.next_offset??null;
    }catch(error){if(entry.turn===turn)entry.error=error.message}
    finally{if(entry.turn===turn){entry.loading=false;renderDetail(entry)}}
  }

  function toggle(entry){
    entry.open=!entry.open;entry.detail.hidden=!entry.open;summary(entry);
    if(entry.open)loadDeliveries(entry);
  }

  /* ── Дослать оставшимся ─────────────────────────────────────────────── */
  function askResume(entry){
    const {job}=entry,panel=entry.confirm;
    const title=el('h3','',logic.resumeLabel(job.pending));
    const text=el('p','',logic.resumeConfirm(job.pending));
    const buttons=el('div');
    const cancel=el('button','button','Вернуться');cancel.type='button';
    const send=el('button','button primary','Дослать');send.type='button';
    cancel.addEventListener('click',()=>{panel.hidden=true;entry.resumeButton?.focus()});
    send.addEventListener('click',()=>resume(entry,send));
    buttons.append(cancel,send);panel.setAttribute('role','alertdialog');panel.setAttribute('aria-label',title.textContent);
    panel.replaceChildren(title,text,buttons);panel.hidden=false;showMessage(entry,'');send.focus();
  }

  async function resume(entry,button){
    const work=api(jobUrl(entry.job.id)+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    if(Busy)Busy.button(button,work,{done:false});
    button.disabled=true;
    try{
      const answer=await work;
      entry.job=answer.job;entry.confirm.hidden=true;summary(entry);
      showMessage(entry,answer.started?'Досылка запущена: сообщение получат только те, кому оно ещё не уходило.':'Досылать некому: всем получателям уже отправляли.');
      schedule();
    }catch(error){entry.confirm.hidden=true;showMessage(entry,error.message,true);summary(entry)}
    finally{button.disabled=false}
  }

  /* ── Список и опрос идущих ──────────────────────────────────────────── */
  function place(jobs,append){
    const list=$('broadcast-history-list');list.removeAttribute('aria-busy');
    if(!append){
      // Новые сверху; карточки, которых нет на первой странице, остаются ниже —
      // открытые получатели и подтверждения не сбрасываются.
      const fresh=new Set(jobs.map(job=>job.id));
      const rest=loaded.filter(job=>!fresh.has(job.id)&&cards.has(job.id));
      loaded=[...jobs,...rest];
    }else loaded=[...loaded,...jobs.filter(job=>!loaded.some(item=>item.id===job.id))];
    const nodes=loaded.map(job=>{const entry=cards.get(job.id)||create(job);entry.job=job;summary(entry);return entry.root});
    for(const [id,entry] of cards)if(!loaded.some(job=>job.id===id))cards.delete(id);
    list.replaceChildren(...nodes);
    if(!loaded.length)list.append(el('p','founder-note broadcast-history-empty',
      kind==='auto'?'Автоматических рассылок пока не было.':kind==='manual'?'Ручных рассылок пока не было.':'Рассылок пока не было.'));
  }

  async function load({append=false,quiet=false}={}){
    const turn=++generation;
    if(!append&&!quiet){skeleton();$('broadcast-history-list').setAttribute('aria-busy','true')}
    try{
      const page=await api(`/api/founder/broadcast/jobs?kind=${kind}${append&&cursor?'&cursor='+encodeURIComponent(cursor):''}`,{retroBusy:false});
      if(turn!==generation)return;
      supported=page.supported!==false;
      $('broadcast-history-kinds').hidden=!supported;
      if(!supported){cards.clear();loaded=[];$('broadcast-history-list').replaceChildren();$('broadcast-history-more').hidden=true;
        setNote('История появится после обновления бота.');return}
      setNote('');
      const deeper=quiet&&loaded.length>page.jobs.length;
      place(page.jobs,append);
      // Фоновое обновление первой страницы не теряет догруженные: «ещё»
      // продолжает с того места, где кончается список на экране.
      if(!deeper)cursor=page.next_cursor??null;
      $('broadcast-history-more').hidden=!cursor;
      schedule();
    }catch(error){
      if(turn!==generation)return;
      if(!quiet||!loaded.length){$('broadcast-history-list').replaceChildren();$('broadcast-history-more').hidden=true;loaded=[];cards.clear()}
      setNote(error.message,true);
    }
  }

  /** Идущие рассылки опрашиваем по одной, пока не закончатся. */
  function schedule(){
    clearTimeout(pollTimer);pollTimer=null;
    if([...cards.values()].some(entry=>!logic.isTerminal(entry.job.status)))pollTimer=setTimeout(poll,POLL_MS);
  }
  async function poll(){
    const active=[...cards.values()].filter(entry=>!logic.isTerminal(entry.job.status));
    await Promise.all(active.map(async entry=>{
      try{
        const job=await api(jobUrl(entry.job.id),{retroBusy:false});
        const finished=logic.isTerminal(job.status);
        entry.job=job;loaded=loaded.map(item=>item.id===job.id?job:item);summary(entry);
        // Получатели открыты — по окончании рассылки перечитываем их статусы.
        if(entry.open&&finished)loadDeliveries(entry);
      }catch{}
    }));
    schedule();
  }

  function setKind(next){
    if(next===kind)return;kind=next;cursor=null;loaded=[];cards.clear();
    document.querySelectorAll('#broadcast-history-kinds [data-kind]').forEach(chip=>{
      const active=chip.dataset.kind===kind;chip.classList.toggle('is-active',active);chip.setAttribute('aria-pressed',String(active));
    });
    load();
  }

  document.querySelectorAll('#broadcast-history-kinds [data-kind]').forEach(chip=>chip.addEventListener('click',()=>setKind(chip.dataset.kind)));
  $('broadcast-history-more').addEventListener('click',()=>{const work=load({append:true});if(Busy)Busy.button($('broadcast-history-more'),work,{done:false})});

  // Форма рассылки сообщает о новой рассылке; предпросмотр сверяет текст с журналом.
  globalThis.FounderBroadcastHistory={
    reload:()=>supported?load({quiet:true}):Promise.resolve(),
    jobs:()=>supported?[...loaded]:[],
  };
  load();
})();
