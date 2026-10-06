/* «Зарплата · день» — сетка сменных как в макете 2b: клетка = день выплаты.
   Клик по клетке — ✓ выдано по ставке смены, ещё клик — снять. Другая сумма —
   правая кнопка мыши, долгое нажатие или цифра с клавиатуры. Пишется сразу. */
(() => {
  const $=id=>document.getElementById(id), L=globalThis.SalaryDayLogic, B=globalThis.RetroBusy;
  const formatter=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2});
  const fmt=value=>formatter.format(value), dm=day=>day.slice(8,10)+'.'+day.slice(5,7);
  const node=(tag,cls,text)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text!=null)el.textContent=text;return el;};
  let current=null, today=null, selectedDay=null, sequence=0, controller=null, writes=0, queue=Promise.resolve();
  let pop=null, pressTimer=0, pressed=null;
  const inflight=new Map();
  const monthTitle=month=>new Intl.DateTimeFormat('ru-RU',{month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(month+'-01T12:00:00Z'));
  function message(text,error=false){
    const el=$('salary-message');el.textContent=text;el.hidden=!text;el.classList.toggle('is-error',error);el.setAttribute('role',error?'alert':'status');
    // Клетку жмут внизу таблицы — строка сверху не видна, ошибку дублирует тост.
    if(error&&text)globalThis.RetroToast?.show(text,'error');
  }
  function controls(){['month-prev','month-next','month-input','salary-refresh'].forEach(id=>$(id).disabled=writes>0);if(today)$('month-next').disabled=writes>0||$('month-input').value>=today.slice(0,7);}
  const personOf=id=>current?.people.find(p=>p.id===id);
  const cellId=(personId,day)=>'sd-c-'+personId+'-'+day;
  function view(person,day){
    const amount=L.parseAmount(person.cells?.[day]?.amount??0)||0, rate=L.rateOf(person,day);
    return {amount,rate,state:L.cellState(amount,rate),editable:L.canEdit(current,person,day)};
  }
  /* Клетка в классах сетки «Зарплаты · месяц» (.pr-s): ✓ — по ставке, сумма на
     жёлтом — другая сумма, пусто — не выдано, серое — только просмотр. */
  function paintCell(person,day,el=document.getElementById(cellId(person.id,day))){
    if(!el)return;
    const v=view(person,day);
    const keep=[...el.classList].filter(cls=>cls.startsWith('rm-')||cls==='is-focus').map(cls=>' '+cls).join('');
    el.className='pr-s is-'+(v.state==='on'?'paid':v.state==='odd'?'odd':v.editable?'empty':'future')+keep;
    el.textContent=v.state==='on'?'✓':v.state==='odd'?fmt(v.amount):'';
    const label=person.name+' · выплата '+dm(day)+' за смену '+dm(L.previousDay(day))+' · '+(v.amount?'выдано '+fmt(v.amount)+' сум':'не выдано');
    if(!v.editable){
      el.title=current.closed?'Месяц закрыт — только для чтения':day<current.entry_start?'История — только для просмотра'
        :day>today?'Будущая выплата':person.archived?'Сотрудник в архиве':'Эта дата доступна только для просмотра.';
      return;
    }
    el.setAttribute('aria-pressed',String(v.state!=='off'));el.setAttribute('aria-label',label);
    el.title=v.amount?label+' · нажмите, чтобы снять':v.rate?label+' · нажмите — по ставке '+fmt(v.rate):label+' · ставки нет, введите сумму';
  }
  function totals(){
    const view=L.matrix(current);
    $('salary-total').textContent=fmt(view.total)+' сум';
    $('salary-selected').textContent=fmt(view.perDay[selectedDay]||0)+' сум';
    $('salary-people').textContent=fmt(view.people.length);
    $('selected-title').textContent='Выдано '+dm(selectedDay);
    $('selected-shift').textContent='За смену '+dm(L.previousDay(selectedDay));
    view.people.forEach(person=>{const el=$('sd-person-'+person.id);if(el)el.textContent=fmt(person.paid);});
    current.days.forEach(day=>{const el=$('sd-total-'+day);if(el)el.textContent=view.perDay[day]?fmt(view.perDay[day]):'';});
    if($('sd-grand'))$('sd-grand').textContent=fmt(view.total)+' сум';
  }
  function selectDay(day){
    selectedDay=day;
    $('sheet-grid').querySelectorAll('.pr-day').forEach(el=>{el.classList.toggle('is-selected',el.dataset.day===day);el.setAttribute('aria-pressed',String(el.dataset.day===day));});
    totals();
  }
  function render(){
    const grid=$('sheet-grid'), view=L.matrix(current);grid.replaceChildren();
    grid.style.setProperty('--days',String(current.days.length));
    $('sheet-empty').hidden=!!view.people.length;$('sheet-scroll').hidden=!view.people.length;
    const head=node('div','pr-row is-head');head.setAttribute('role','row');
    for(const [cls,title] of [['pr-c pr-c-name','Сотрудник'],['pr-c pr-c-sum','Ставка / смена']]){const el=node('div',cls,title);el.setAttribute('role','columnheader');head.append(el);}
    current.days.forEach(day=>{
      const cell=node('div','sd-header-cell');cell.setAttribute('role','columnheader');
      const button=node('button','pr-day'+(day===today?' is-today':'')+(day>today?' is-future':''));button.type='button';button.dataset.day=day;
      button.title='Выплата '+dm(day)+' · за смену '+dm(L.previousDay(day));
      button.append(node('strong','',String(Number(day.slice(8,10)))),node('span','',new Intl.DateTimeFormat('ru-RU',{weekday:'short',timeZone:'UTC'}).format(new Date(day+'T12:00:00Z'))),node('span','sd-day-sub','за '+dm(L.previousDay(day))));
      button.addEventListener('click',()=>selectDay(day));cell.append(button);head.append(cell);
    });
    const paidHead=node('div','pr-c pr-c-paid','Выдано');paidHead.setAttribute('role','columnheader');head.append(paidHead);grid.append(head);
    // В матрице клетки — массив для подсчёта; рисуем и пишем по исходным данным.
    view.people.forEach((summary,index)=>{
      const person=current.people[index], row=node('div','pr-row is-shift');row.setAttribute('role','row');
      const who=node('div','pr-c pr-c-name');who.setAttribute('role','rowheader');who.append(node('span','pr-name',person.name),node('span','pr-role',person.archived?(person.role||'')+' · архив':person.role||''));
      const rate=person.rate==null||!Number(person.rate)?node('div','pr-c pr-c-sum is-norate','нет ставки'):node('div','pr-c pr-c-sum rm-num',fmt(Number(person.rate)));
      rate.setAttribute('role','cell');row.append(who,rate);
      summary.cells.forEach(cell=>{
        const wrap=node('div','sd-cell');wrap.setAttribute('role','cell');
        const el=node(cell.editable?'button':'div');el.id=cellId(person.id,cell.day);el.dataset.day=cell.day;
        if(cell.editable){el.type='button';el.dataset.person=String(person.id);el.dataset.busyKey='sd:'+person.id+':'+cell.day;}
        paintCell(person,cell.day,el);wrap.append(el);row.append(wrap);
      });
      const paid=node('div','pr-c pr-c-paid rm-num',fmt(summary.paid));paid.id='sd-person-'+person.id;paid.setAttribute('role','cell');row.append(paid);grid.append(row);
    });
    const foot=node('div','pr-row is-foot');foot.setAttribute('role','row');
    const title=node('div','pr-c pr-c-label','Выдано за день');title.setAttribute('role','rowheader');foot.append(title,node('div','pr-c pr-c-sum pr-c-sumfoot'));
    current.days.forEach(day=>{const el=node('div','pr-t rm-num'+(day===today?' is-today':''));el.id='sd-total-'+day;el.setAttribute('role','cell');foot.append(el);});
    const grand=node('div','pr-c pr-c-grand rm-num');grand.id='sd-grand';grand.setAttribute('role','cell');foot.append(grand);grid.append(foot);
    selectDay(selectedDay);
    const entry=new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(current.entry_start+'T12:00:00Z')).replace(/\.$/,'');
    $('entry-note').textContent=current.closed?'Месяц закрыт — только для чтения':'Ручной ввод выплат с '+entry+'. В столбце указана дата выплаты за предыдущую смену. Прежние общие выплаты без сотрудника сохранены в «Финансах дня».';
  }
  /* Одна клетка — один PUT с абсолютной суммой и ожидаемой прежней (сервер
     сверяет её и не задвоит выплату). Клетка перекрашивается сразу; если
     сервер отказал — возвращается прежнее и показывается причина. */
  function commit(person,day,amount){
    const key='sd:'+person.id+':'+day;if(inflight.has(key))return inflight.get(key);
    const data=current, cell=person.cells?.[day]||{}, before=L.parseAmount(cell.amount??0);
    if(before===null){message('Не удалось прочитать сумму выплаты. Обновите ведомость.',true);return Promise.resolve(false);}
    if(amount===before)return Promise.resolve(true);
    if(!L.canEdit(data,person,day)){message('Эта дата доступна только для просмотра.',true);return Promise.resolve(false);}
    person.cells=person.cells||{};person.cells[day]={...cell,amount:String(amount)};
    writes++;controls();paintCell(person,day);totals();
    const work=queue.then(async()=>{
      try {
        const response=await fetch('/api/accountant/salary-day/cell',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({date:day,employee_id:person.id,amount:String(amount),expected_amount:String(before)})});
        const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Не удалось сохранить выплату.');
        const confirmed=result.amount==null?null:L.parseAmount(result.amount);if(confirmed===null)throw new Error('Не удалось проверить сохранённую сумму. Обновите ведомость.');
        if(current===data)person.cells[day]={...cell,amount:String(confirmed),work_day:result.work_day||cell.work_day||L.previousDay(day),editable:result.editable!==false};
        return true;
      } catch(error){if(current===data){person.cells[day]=cell;message(error.message,true);}return false;}
      finally{writes--;inflight.delete(key);controls();if(current===data){paintCell(person,day);totals();}}
    });
    queue=work.catch(()=>false);inflight.set(key,work);
    globalThis.RetroSave?.track(work);
    return work;
  }
  async function loadMonth(month){
    if(writes){if(current)$('month-input').value=current.month;message('Дождитесь сохранения выплат.',true);return false;}
    if(!/^\d{4}-(0[1-9]|1[0-2])$/.test(month)||month>today.slice(0,7)){if(current)$('month-input').value=current.month;message('Выберите текущий или прошедший месяц.',true);return false;}
    const token=++sequence;controller?.abort();controller=new AbortController();$('salary-body').setAttribute('aria-busy','true');$('sheet-grid').inert=true;
    try{
      const response=await fetch('/api/accountant/salary-day/month?month='+encodeURIComponent(month),{signal:controller.signal,cache:'no-store'});
      const data=await response.json();if(!response.ok)throw new Error(data.detail||'Не удалось загрузить ведомость.');
      if(token!==sequence)return false;
      closePop();
      current=data;today=data.today;selectedDay=data.days.includes(selectedDay)?selectedDay:data.days.includes(today)?today:data.days.at(-1);
      $('month-input').value=data.month;$('month-input').max=today.slice(0,7);$('month-label').textContent=monthTitle(month);$('crumb-month').textContent='Зарплата · день';
      render();$('salary-body').hidden=false;message('');$('connection').classList.remove('rm-status-skel');$('connection').textContent='Ручная ведомость · '+monthTitle(month);controls();
      const scrollDay=today<data.entry_start&&data.days.includes(data.entry_start)?data.entry_start:selectedDay;
      const focus=$('sheet-grid').querySelector('button.pr-day[data-day="'+scrollDay+'"]');
      if(focus){
        // Выбранный день виден целиком; слева — до двух прошлых дней, сколько влезет за
        // закреплёнными колонками. Ровно по границе дня: на телефоне прокрутка прилипает к дням.
        const scroll=$('sheet-scroll'), left=focus.parentElement.offsetLeft, width=focus.offsetWidth;
        const sticky=[...$('sheet-grid').querySelectorAll('.is-head>.pr-c-name,.is-head>.pr-c-sum')]
          .filter(el=>getComputedStyle(el).position==='sticky').reduce((sum,el)=>sum+el.offsetWidth,0);
        const before=Math.max(0,Math.min(2,Math.floor((scroll.clientWidth-sticky-width)/width)));
        scroll.scrollLeft=Math.max(0,left-sticky-before*width);
      }
      return true;
    }catch(error){if(token===sequence&&error.name!=='AbortError'){if(current)$('month-input').value=current.month;message(error.message,true);}return false;}
    finally{if(token===sequence){$('salary-loading').hidden=true;$('salary-body').setAttribute('aria-busy','false');$('sheet-grid').inert=false;}}
  }

  /* Записать клетку; медленную запись видно спиннером на самой клетке. */
  function write(person,day,amount){
    closePop();
    const work=commit(person,day,amount), el=document.getElementById(cellId(person.id,day));
    if(B&&el)B.button(el,work,{done:false}).catch(()=>{});
    return work;
  }
  function toggle(person,day,el){
    const v=view(person,day);if(!v.editable)return;
    selectDay(day);
    const target=L.toggleTarget(v.amount,v.rate);
    if(target===null)return openEditor(person,day,el);
    write(person,day,target);
  }

  /* ── Другая сумма: маленькое окно у клетки, как «Удалить?» в «Зарплате · месяц» ── */
  function closePop(){if(pop){pop.close();pop=null;}}
  function openEditor(person,day,anchor,typed=''){
    const v=view(person,day);if(!v.editable)return;
    closePop();selectDay(day);
    let sure=false;
    const box=node('div','pr-confirm sd-pop');box.setAttribute('role','dialog');box.setAttribute('aria-label','Сумма выплаты');
    const input=node('input');input.inputMode='decimal';input.autocomplete='off';input.dataset.saveIgnore='';
    input.value=typed||(v.amount?fmt(v.amount):v.rate?fmt(v.rate):'');input.dataset.clean=v.amount?fmt(v.amount):'';
    input.setAttribute('aria-label',person.name+' · выплата '+dm(day)+', сум');
    const error=node('span','sd-pop-error');error.hidden=true;error.setAttribute('role','alert');
    const actions=node('div','pr-confirm-actions'), keep=node('button','pr-confirm-keep','Отмена'), ok=node('button','sd-pop-save','Записать');
    keep.type=ok.type='button';actions.append(keep,ok);
    box.append(node('span','pr-confirm-q',person.name+' · выплата '+dm(day)),input,error,actions);
    document.body.append(box);
    const save=()=>{
      const amount=L.parseAmount(input.value);
      if(amount===null){error.textContent='Введите неотрицательную сумму цифрами, не более двух знаков после запятой.';error.hidden=false;input.focus();return false;}
      // Лишний ноль — частая опечатка: сумму сильно выше ставки переспрашиваем.
      if(!sure&&amount>(v.rate?v.rate*3:3000000)){
        sure=true;ok.textContent='Да, записать';error.hidden=false;input.focus();
        error.textContent=(v.rate?'Сумма в '+fmt(Math.round(amount/v.rate))+' раз больше ставки.':'Сумма больше 3 000 000 сум.')+' Проверьте и нажмите ещё раз.';
        return false;
      }
      return write(person,day,amount);
    };
    const place=()=>{
      if(!anchor.isConnected)return closePop();
      const r=anchor.getBoundingClientRect(), w=box.offsetWidth, h=box.offsetHeight;
      const top=r.bottom+h+8>innerHeight?r.top-h-6:r.bottom+6;
      box.style.top=Math.max(8,top)+'px';box.style.left=Math.min(Math.max(8,r.left+r.width/2-w/2),innerWidth-w-8)+'px';
    };
    const onKey=event=>{if(event.key==='Escape'){event.preventDefault();closePop();anchor.focus?.({preventScroll:true});}};
    const onDown=event=>{if(!box.contains(event.target)&&!anchor.contains(event.target))closePop();};
    input.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();save();}});
    input.addEventListener('input',()=>{error.hidden=true;sure=false;ok.textContent='Записать';});
    keep.addEventListener('click',()=>{closePop();anchor.focus({preventScroll:true});});ok.addEventListener('click',save);
    pop={anchor,save,dirty:()=>L.parseAmount(input.value)!==L.parseAmount(input.dataset.clean),close(){box.remove();anchor.classList.remove('is-focus');
      window.removeEventListener('scroll',place,true);window.removeEventListener('resize',place);
      document.removeEventListener('keydown',onKey);document.removeEventListener('pointerdown',onDown,true);}};
    anchor.classList.add('is-focus');place();
    window.addEventListener('scroll',place,true);window.addEventListener('resize',place);
    document.addEventListener('keydown',onKey);document.addEventListener('pointerdown',onDown,true);
    // Фокус — после текущего нажатия, иначе Enter из клетки тут же нажал бы кнопку.
    setTimeout(()=>{if(!box.isConnected)return;input.focus({preventScroll:true});if(typed)input.setSelectionRange(input.value.length,input.value.length);else input.select();},0);
  }

  /* ── Клетки: мышь, палец, клавиатура ─────────────────────────────────── */
  const grid=$('sheet-grid');
  const cellOf=target=>target.closest?.('button.pr-s');
  const targetOf=el=>{const person=personOf(Number(el.dataset.person));return person?{person,day:el.dataset.day}:null;};
  grid.addEventListener('click',event=>{
    const el=cellOf(event.target);if(!el)return;
    if(pressed===el){pressed=null;return;}   // долгое нажатие уже открыло сумму
    if(pop?.anchor===el){closePop();return;}
    const t=targetOf(el);if(t)toggle(t.person,t.day,el);
  });
  grid.addEventListener('contextmenu',event=>{
    const el=cellOf(event.target);if(!el)return;
    event.preventDefault();clearTimeout(pressTimer);
    const t=targetOf(el);if(t){if(pressed!==el)pressed=null;openEditor(t.person,t.day,el);}
  });
  grid.addEventListener('pointerdown',event=>{
    const el=cellOf(event.target);pressed=null;clearTimeout(pressTimer);
    if(!el||event.pointerType==='mouse')return;
    pressTimer=setTimeout(()=>{const t=targetOf(el);if(t){pressed=el;openEditor(t.person,t.day,el);}},450);
  });
  for(const type of ['pointerup','pointercancel','pointerleave'])grid.addEventListener(type,()=>clearTimeout(pressTimer));
  grid.addEventListener('scroll',()=>clearTimeout(pressTimer),true);
  function move(el,dx,dy){
    const ids=current.people.map(p=>p.id), days=current.days;
    let row=ids.indexOf(Number(el.dataset.person)), col=days.indexOf(el.dataset.day);
    for(;;){
      row+=dy;col+=dx;if(row<0||row>=ids.length||col<0||col>=days.length)return;
      const next=document.getElementById(cellId(ids[row],days[col]));
      if(next&&next.tagName==='BUTTON'){next.focus();next.scrollIntoView({block:'nearest',inline:'nearest'});selectDay(days[col]);return;}
    }
  }
  grid.addEventListener('keydown',event=>{
    const el=cellOf(event.target);if(!el||event.altKey||event.ctrlKey||event.metaKey)return;
    const arrows={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]};
    if(arrows[event.key]){event.preventDefault();move(el,...arrows[event.key]);return;}
    const t=targetOf(el);if(!t)return;
    if(/^\d$/.test(event.key)){event.preventDefault();openEditor(t.person,t.day,el,event.key);}
    else if((event.key==='Delete'||event.key==='Backspace')&&view(t.person,t.day).amount){event.preventDefault();write(t.person,t.day,0);}
  });

  function navigate(month){if(current&&globalThis.RetroSave?.confirmLeave()===false){$('month-input').value=current.month;return;}const work=loadMonth(month);if(B&&!$('salary-body').hidden)B.section($('salary-body'),work);}
  $('month-prev').addEventListener('click',()=>navigate(L.shiftMonth($('month-input').value,-1)));
  $('month-next').addEventListener('click',()=>navigate(L.shiftMonth($('month-input').value,1)));
  $('month-input').addEventListener('change',()=>navigate($('month-input').value));
  $('salary-refresh').addEventListener('click',()=>navigate($('month-input').value));
  // «Сохранить» дописывает сумму, набранную в окне у клетки; галочки уже записаны.
  globalThis.RetroSave?.register(grid,async()=>pop?await pop.save():true,{dirty:()=>!!pop?.dirty()});
  (async()=>{try{today=(await globalThis.RetroConfig).today;const params=new URLSearchParams(location.search);selectedDay=params.get('date');const month=params.get('month')||selectedDay?.slice(0,7)||today.slice(0,7);$('month-input').value=month;await loadMonth(month);}catch(error){$('salary-loading').hidden=true;message(error.message,true);}})();
})();
