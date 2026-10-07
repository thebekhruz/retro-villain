/* «Зарплата · день» — та же сетка, что «Зарплата · месяц»: клетка = день выплаты.
   Первый клик — ✓ выдано по ставке смены. Второй клик — поле суммы прямо в клетке
   (опоздал, штраф — выдать меньше); пусто или 0 — выплаты нет. Опоздавшие за смену
   подсвечены розовым, ставка при этом не меняется. Пишется сразу. */
(() => {
  const $=id=>document.getElementById(id), L=globalThis.SalaryDayLogic, B=globalThis.RetroBusy;
  const formatter=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2});
  const fmt=value=>formatter.format(value), dm=day=>day.slice(8,10)+'.'+day.slice(5,7);
  const node=(tag,cls,text)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text!=null)el.textContent=text;return el;};
  let current=null, today=null, selectedDay=null, sequence=0, controller=null, writes=0, queue=Promise.resolve();
  let editing=null;
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
    return {amount,rate,state:L.cellState(amount,rate),editable:L.canEdit(current,person,day),late:person.cells?.[day]?.late||null};
  }
  /* Клетка — ячейка «Зарплаты · месяц» (.pr-m): белая, сегодняшний столбец подсвечен,
     ✓ по ставке — на зелёном, другая сумма — число на жёлтом, опоздал — розовая
     (не выдано — со временем входа). */
  function paintCell(person,day,el=document.getElementById(cellId(person.id,day))){
    if(!el)return;
    const v=view(person,day), late=v.late&&v.state!=='odd';
    const keep=[...el.classList].filter(cls=>cls.startsWith('rm-')).map(cls=>' '+cls).join('');
    el.className='pr-m sd-m'+(v.state==='on'?' is-filled is-tick':v.state==='odd'?' is-odd':'')+(late?' is-late':'')
      +(day===today?' is-today':'')+(day>today?' is-future':'')+(v.editable?'':' is-locked')+keep;
    el.textContent=v.state==='on'?'✓':v.state==='odd'?fmt(v.amount):late?v.late:'';
    if(v.editable){
      el.setAttribute('aria-pressed',String(v.state!=='off'));
      el.setAttribute('aria-label',person.name+' · выплата '+dm(day)+' за смену '+dm(L.previousDay(day))
        +(v.late?' · опоздал, вход '+v.late:'')+' · '+(v.amount?'выдано '+fmt(v.amount)+' сум':'не выдано'));
    }
  }
  function makeCell(person,day){
    const editable=L.canEdit(current,person,day), el=node(editable?'button':'div');
    el.id=cellId(person.id,day);el.dataset.day=day;
    if(editable){el.type='button';el.dataset.person=String(person.id);el.dataset.busyKey='sd:'+person.id+':'+day;}
    paintCell(person,day,el);
    return el;
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
    editing=null;
    const grid=$('sheet-grid'), view=L.matrix(current);grid.replaceChildren();
    grid.style.setProperty('--days',String(current.days.length));
    $('sheet-empty').hidden=!!view.people.length;$('sheet-scroll').hidden=!view.people.length;
    // Шапка — как у «Зарплаты · месяц»: число и день недели, «сегодня», вчера светло-зелёным.
    const head=node('div','pr-row is-head');head.setAttribute('role','row');
    for(const [cls,title] of [['pr-c pr-c-name','Сотрудник'],['pr-c pr-c-sum','Ставка']]){const el=node('div',cls,title);el.setAttribute('role','columnheader');head.append(el);}
    const yesterday=L.previousDay(today);
    current.days.forEach(day=>{
      const cell=node('div','sd-header-cell');cell.setAttribute('role','columnheader');
      const future=day>today, button=node(future?'div':'button','pr-day'+(day===today?' is-today':'')+(day===yesterday?' is-shift':'')+(future?' is-future':''));button.dataset.day=day;
      button.append(node('strong','',String(Number(day.slice(8,10)))),node('span','',day===today?'сегодня':new Intl.DateTimeFormat('ru-RU',{weekday:'short',timeZone:'UTC'}).format(new Date(day+'T12:00:00Z'))));
      if(!future){button.type='button';button.setAttribute('aria-label','Выплата '+dm(day)+' за смену '+dm(L.previousDay(day)));button.addEventListener('click',()=>selectDay(day));}
      cell.append(button);head.append(cell);
    });
    const paidHead=node('div','pr-c pr-c-paid','Выдано');paidHead.setAttribute('role','columnheader');head.append(paidHead);grid.append(head);
    // В матрице клетки — массив для подсчёта; рисуем и пишем по исходным данным.
    view.people.forEach((summary,index)=>{
      const person=current.people[index], row=node('div','pr-row is-monthly');row.setAttribute('role','row');
      const who=node('div','pr-c pr-c-name');who.setAttribute('role','rowheader');who.append(node('span','pr-name',person.name),node('span','pr-role',person.archived?(person.role||'')+' · архив':person.role||''));
      const rate=person.rate==null||!Number(person.rate)?node('div','pr-c pr-c-sum is-norate','нет ставки'):node('div','pr-c pr-c-sum rm-num',fmt(Number(person.rate)));
      rate.setAttribute('role','cell');row.append(who,rate);
      summary.cells.forEach(cell=>{const wrap=node('div','sd-cell');wrap.setAttribute('role','cell');wrap.append(makeCell(person,cell.day));row.append(wrap);});
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
      current=data;today=data.today;selectedDay=data.days.includes(selectedDay)?selectedDay:data.days.includes(today)?today:data.days.at(-1);
      $('month-input').value=data.month;$('month-input').max=today.slice(0,7);$('month-label').textContent=monthTitle(month);$('crumb-month').textContent='Зарплата · день';
      render();$('salary-body').hidden=false;message('');$('connection').classList.remove('rm-status-skel');$('connection').textContent='Ручная ведомость · '+monthTitle(month);controls();
      const scrollDay=today<data.entry_start&&data.days.includes(data.entry_start)?data.entry_start:selectedDay;
      const focus=$('sheet-grid').querySelector('.pr-day[data-day="'+scrollDay+'"]');
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
    const work=commit(person,day,amount), el=document.getElementById(cellId(person.id,day));
    if(B&&el&&el.tagName==='BUTTON')B.button(el,work,{done:false}).catch(()=>{});
    return work;
  }

  /* ── Второй клик: поле суммы прямо в клетке, как ячейка «Зарплаты · месяц» ── */
  function startEdit(person,day,el,typed=''){
    const v=view(person,day);if(!v.editable||editing)return;
    selectDay(day);
    const input=node('input','pr-m sd-m is-editing'+(day===today?' is-today':''));
    input.id=el.id;input.inputMode='decimal';input.autocomplete='off';input.dataset.moneyHint='off';
    input.value=typed||(v.amount?String(v.amount):v.rate?String(v.rate):'');
    input.setAttribute('aria-label',person.name+' · выплата '+dm(day)+', сум');
    el.replaceWith(input);
    editing={person,day,input,sure:false,clean:v.amount};
    input.focus({preventScroll:true});
    if(typed)input.setSelectionRange(input.value.length,input.value.length);else input.select();
    input.addEventListener('keydown',event=>{
      if(event.key==='Enter'){event.preventDefault();finishEdit(true);}
      else if(event.key==='Escape'){event.preventDefault();closeEdit(true);}
    });
    input.addEventListener('input',()=>{if(editing)editing.sure=false;});
    input.addEventListener('blur',()=>finishEdit(false));
  }
  function closeEdit(refocus){
    if(!editing)return null;
    const {person,day,input}=editing;editing=null;
    const el=makeCell(person,day);
    if(input.isConnected)input.replaceWith(el);
    if(refocus)el.focus({preventScroll:true});
    return el;
  }
  /* Enter или уход из поля: пусто или 0 — выплаты нет; сумму сильно выше ставки
     (лишний ноль) записываем только после второго Enter. */
  function finishEdit(byEnter){
    if(!editing)return true;
    const {person,day,input}=editing, v=view(person,day), amount=L.parseAmount(input.value);
    if(amount===null){
      if(byEnter){message('Введите неотрицательную сумму цифрами, не более двух знаков после запятой.',true);return false;}
      closeEdit(false);message('Сумма не записана: введите её цифрами.',true);return false;
    }
    if(amount!==v.amount&&amount>(v.rate?v.rate*3:3000000)&&!editing.sure){
      const text=(v.rate?'Сумма в '+fmt(Math.round(amount/v.rate))+' раз больше ставки.':'Сумма больше 3 000 000 сум.');
      if(byEnter){editing.sure=true;message(text+' Нажмите Enter ещё раз, чтобы записать.',true);return false;}
      closeEdit(false);message(text+' Не записано — проверьте и введите ещё раз.',true);return false;
    }
    closeEdit(byEnter);
    return amount===v.amount?true:write(person,day,amount);
  }

  /* ── Клетки: мышь, палец, клавиатура ─────────────────────────────────── */
  const grid=$('sheet-grid');
  const cellOf=target=>target.closest?.('button.sd-m');
  const targetOf=el=>{const person=personOf(Number(el.dataset.person));return person?{person,day:el.dataset.day}:null;};
  grid.addEventListener('click',event=>{
    const el=cellOf(event.target);if(!el)return;
    const t=targetOf(el);if(!t)return;
    const v=view(t.person,t.day);if(!v.editable)return;
    selectDay(t.day);
    // Первый клик — ✓ по ставке; второй (или нет ставки) — поле суммы.
    if(!v.amount&&v.rate)write(t.person,t.day,v.rate);
    else startEdit(t.person,t.day,el);
  });
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
    if(/^\d$/.test(event.key)){event.preventDefault();startEdit(t.person,t.day,el,event.key);}
    else if((event.key==='Delete'||event.key==='Backspace')&&view(t.person,t.day).amount){event.preventDefault();write(t.person,t.day,0);}
  });

  function navigate(month){if(current&&globalThis.RetroSave?.confirmLeave()===false){$('month-input').value=current.month;return;}const work=loadMonth(month);if(B&&!$('salary-body').hidden)B.section($('salary-body'),work);}
  $('month-prev').addEventListener('click',()=>navigate(L.shiftMonth($('month-input').value,-1)));
  $('month-next').addEventListener('click',()=>navigate(L.shiftMonth($('month-input').value,1)));
  $('month-input').addEventListener('change',()=>navigate($('month-input').value));
  $('salary-refresh').addEventListener('click',()=>navigate($('month-input').value));
  // «Сохранить» дописывает сумму, набранную в клетке; галочки уже записаны.
  globalThis.RetroSave?.register(grid,async()=>editing?await finishEdit(true):true,
    {dirty:()=>!!editing&&L.parseAmount(editing.input.value)!==editing.clean});
  (async()=>{try{today=(await globalThis.RetroConfig).today;const params=new URLSearchParams(location.search);selectedDay=params.get('date');const month=params.get('month')||selectedDay?.slice(0,7)||today.slice(0,7);$('month-input').value=month;await loadMonth(month);}catch(error){$('salary-loading').hidden=true;message(error.message,true);}})();
})();
