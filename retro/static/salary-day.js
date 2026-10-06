/* «Зарплата · день» — таблица с галочками, без автоначисления.
   Клик по клетке — выдано по ставке смены, ещё клик — снять. Другая сумма —
   ✎ в клетке, правая кнопка мыши, долгое нажатие или цифра с клавиатуры.
   Каждая отметка записывается сразу; последнее действие можно отменить. */
(() => {
  const $=id=>document.getElementById(id), L=globalThis.SalaryDayLogic, B=globalThis.RetroBusy;
  const formatter=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2});
  const fmt=value=>formatter.format(value), dm=day=>day.slice(8,10)+'.'+day.slice(5,7);
  const node=(tag,cls,text)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text!=null)el.textContent=text;return el;};
  let current=null, today=null, selectedDay=null, sequence=0, controller=null, writes=0, queue=Promise.resolve();
  let undo=null, pop=null, pressTimer=0, pressed=null, lastError='';
  const inflight=new Map();
  const monthTitle=month=>new Intl.DateTimeFormat('ru-RU',{month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(month+'-01T12:00:00Z'));
  function message(text,error=false){
    const el=$('salary-message');el.textContent=text;el.hidden=!text;el.classList.toggle('is-error',error);el.setAttribute('role',error?'alert':'status');
    // Клетку жмут внизу длинной таблицы — строка сверху не видна, ошибку дублирует тост.
    if(error&&text){lastError=text;globalThis.RetroToast?.show(text,'error');}
  }
  function controls(){
    ['month-prev','month-next','month-input','salary-refresh'].forEach(id=>$(id).disabled=writes>0);
    if(today)$('month-next').disabled=writes>0||$('month-input').value>=today.slice(0,7);
    $('sd-undo').disabled=writes>0;
  }
  const personOf=id=>current?.people.find(p=>p.id===id);
  const cellId=(personId,day)=>'sd-c-'+personId+'-'+day;
  function view(person,day){
    const amount=L.parseAmount(person.cells?.[day]?.amount??0)||0, rate=L.rateOf(person,day);
    return {amount,rate,state:L.cellState(amount,rate),editable:L.canEdit(current,person,day)};
  }
  /* Клетка: класс, содержимое и подписи — одна функция и для первой
     отрисовки, и для перекраски после записи. */
  function paintCell(person,day,el=document.getElementById(cellId(person.id,day))){
    if(!el)return;
    const v=view(person,day), shift=dm(L.previousDay(day));
    // Классы busy.js (rm-*) и открытой всплывашки переживают перекраску.
    const keep=[...el.classList].filter(cls=>cls.startsWith('rm-')||cls==='is-editing').map(cls=>' '+cls).join('');
    el.className='sd-tick is-'+v.state+keep+(v.editable?'':' is-locked')+(day>today?' is-future':'')+(day===selectedDay?' is-selected':'');
    if(!v.editable){
      el.textContent=v.state==='on'?'✓':v.state==='odd'?fmt(v.amount):day<current.entry_start&&day<=today?'—':'';
      el.title=current.closed?'Месяц закрыт — только для чтения':day<current.entry_start?'История — только для просмотра'
        :day>today?'Будущая выплата':person.archived?'Сотрудник в архиве':'Эта дата доступна только для просмотра.';
      return;
    }
    el.replaceChildren(v.state==='odd'?node('span','sd-amt',fmt(v.amount)):node('span','sd-box',v.state==='on'?'✓':''));
    el.setAttribute('aria-pressed',String(v.state!=='off'));
    el.setAttribute('aria-label',person.name+' · выплата '+dm(day)+' за смену '+shift+' · '+
      (v.amount?'выдано '+fmt(v.amount)+' сум':'не выдано'));
    el.title=v.amount?'Выдано '+fmt(v.amount)+(v.rate&&v.state==='odd'?' (ставка '+fmt(v.rate)+')':'')+' · нажмите, чтобы снять'
      :v.rate?'Нажмите — выдать по ставке '+fmt(v.rate):'Ставки нет — нажмите и введите сумму';
  }
  function totals(){
    const view=L.matrix(current), marked=view.marked[selectedDay]||0;
    $('salary-total').textContent=fmt(view.total)+' сум';
    $('salary-selected').textContent=fmt(view.perDay[selectedDay]||0)+' сум';
    $('selected-title').textContent='Выдано '+dm(selectedDay);
    $('selected-shift').textContent='За смену '+dm(L.previousDay(selectedDay));
    $('marked-title').textContent='Отмечено '+dm(selectedDay);
    $('salary-people').textContent=marked+' из '+view.people.length;
    $('marked-left').textContent='Без выплаты: '+(view.people.length-marked);
    view.people.forEach(person=>{const el=$('sd-person-'+person.id);if(el)el.textContent=fmt(person.paid);});
    current.days.forEach(day=>{const el=$('sd-total-'+day);if(el)el.textContent=view.perDay[day]?fmt(view.perDay[day]):'—';});
    if($('sd-grand'))$('sd-grand').textContent=fmt(view.total);
    const targets=L.bulkTargets(current,selectedDay), bulk=$('sd-bulk');
    bulk.disabled=!targets.length;
    $('sd-bulk-sub').textContent=targets.length?dm(selectedDay)+' · '+targets.length+' чел.':
      view.people.some(p=>L.canEdit(current,p,selectedDay))?'все отмечены':'день только для просмотра';
  }
  function selectDay(day){
    if(day===selectedDay&&$('sheet-grid').querySelector('.is-selected'))return totals();
    selectedDay=day;
    $('sheet-grid').querySelectorAll('[data-day]').forEach(el=>{
      el.classList.toggle('is-selected',el.dataset.day===day);
      if(el.classList.contains('pr-day'))el.setAttribute('aria-pressed',String(el.dataset.day===day));
    });
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
      const person=current.people[index], row=node('div','pr-row is-monthly');row.setAttribute('role','row');
      const who=node('div','pr-c pr-c-name');who.setAttribute('role','rowheader');who.append(node('span','pr-name',person.name),node('span','pr-role',person.role||''));
      if(person.archived)who.append(node('span','pr-role','Архив'));
      const rate=person.rate==null||!Number(person.rate)?node('div','pr-c pr-c-sum is-norate','нет ставки'):node('div','pr-c pr-c-sum',fmt(Number(person.rate)));
      rate.setAttribute('role','cell');row.append(who,rate);
      summary.cells.forEach(cell=>{
        const wrap=node('div','sd-cell');wrap.setAttribute('role','cell');
        const el=node(cell.editable?'button':'div');el.id=cellId(person.id,cell.day);el.dataset.day=cell.day;
        if(cell.editable){
          el.type='button';el.dataset.person=String(person.id);el.dataset.busyKey='sd:'+person.id+':'+cell.day;
          const edit=node('button','sd-edit','✎');edit.type='button';edit.tabIndex=-1;edit.setAttribute('aria-label','Другая сумма');edit.title='Другая сумма';
          wrap.append(el,edit);
        } else wrap.append(el);
        paintCell(person,cell.day,el);
        row.append(wrap);
      });
      const paid=node('div','pr-c pr-c-paid',fmt(summary.paid));paid.id='sd-person-'+person.id;paid.setAttribute('role','cell');row.append(paid);grid.append(row);
    });
    const foot=node('div','pr-row is-foot');foot.setAttribute('role','row');
    const title=node('div','pr-c pr-c-label','Выдано за день');title.setAttribute('role','rowheader');foot.append(title,node('div','pr-c pr-c-sum pr-c-sumfoot'));
    current.days.forEach(day=>{const el=node('div','pr-t'+(day===today?' is-today':''));el.id='sd-total-'+day;el.setAttribute('role','cell');foot.append(el);});
    const grand=node('div','pr-c pr-c-grand');grand.id='sd-grand';grand.setAttribute('role','cell');foot.append(grand);grid.append(foot);
    const day=selectedDay;selectedDay=null;selectDay(day);
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
      closePop();setUndo(null);
      current=data;today=data.today;selectedDay=data.days.includes(selectedDay)?selectedDay:data.days.includes(today)?today:data.days.at(-1);
      $('month-input').value=data.month;$('month-input').max=today.slice(0,7);$('month-label').textContent=monthTitle(month);$('crumb-month').textContent='Зарплата · день';
      render();$('salary-body').hidden=false;message('');$('connection').classList.remove('rm-status-skel');$('connection').textContent='Ручная ведомость · '+monthTitle(month);controls();
      const scrollDay=today<data.entry_start&&data.days.includes(data.entry_start)?data.entry_start:selectedDay;
      const focus=$('sheet-grid').querySelector('button[data-day="'+scrollDay+'"]');
      if(focus){
        // Выбранный день виден целиком; слева от него — до двух прошлых дней, сколько влезет
        // за закреплёнными колонками (на планшете и телефоне закреплено только имя).
        const scroll=$('sheet-scroll'), left=focus.parentElement.offsetLeft, width=focus.offsetWidth;
        const sticky=[...$('sheet-grid').querySelectorAll('.is-head>.pr-c-name,.is-head>.pr-c-sum')]
          .filter(el=>getComputedStyle(el).position==='sticky').reduce((sum,el)=>sum+el.offsetWidth,0);
        // Ровно по границе дня: на телефоне прокрутка прилипает к началам дней (scroll-snap).
        const before=Math.max(0,Math.min(2,Math.floor((scroll.clientWidth-sticky-width)/width)));
        scroll.scrollLeft=Math.max(0,left-sticky-before*width);
      }
      return true;
    }catch(error){if(token===sequence&&error.name!=='AbortError'){if(current)$('month-input').value=current.month;message(error.message,true);}return false;}
    finally{if(token===sequence){$('salary-loading').hidden=true;$('salary-body').setAttribute('aria-busy','false');$('sheet-grid').inert=false;}}
  }

  /* ── Изменения с отменой ─────────────────────────────────────────────── */
  function setUndo(entry){
    undo=entry;const button=$('sd-undo');button.hidden=!entry;
    if(entry){button.title=entry.label;$('sd-undo-text').textContent=entry.label;}
  }
  /* Записать список изменений и запомнить, как вернуть. Клетку, которую
     пишем, крутит busy.js — у медленной сети виден спиннер на месте. */
  async function change(items,label){
    closePop();
    const plan=items.map(({person,day,amount})=>({person,day,to:amount,from:L.parseAmount(person.cells?.[day]?.amount??0)||0}))
      .filter(item=>item.from!==item.to);
    if(!plan.length)return true;
    const results=await Promise.all(plan.map(item=>{
      const work=commit(item.person,item.day,item.to);
      const el=document.getElementById(cellId(item.person.id,item.day));
      if(B&&el)B.button(el,work,{done:false}).catch(()=>{});
      return work;
    }));
    const done=plan.filter((_,index)=>results[index]===true);
    if(done.length)setUndo({label,items:done});
    if(done.length<plan.length&&plan.length>1)message('Записано '+done.length+' из '+plan.length+'. '+lastError,true);
    return done.length===plan.length;
  }
  function revert(){
    if(!undo||writes)return;
    const items=undo.items.filter(item=>current.people.includes(item.person)
      &&(L.parseAmount(item.person.cells?.[item.day]?.amount??0)||0)===item.to);
    setUndo(null);
    if(!items.length)return;
    change(items.map(item=>({person:item.person,day:item.day,amount:item.from})),'Вернуть отменённое').then(()=>setUndo(null));
  }
  function toggle(person,day,el){
    const v=view(person,day);if(!v.editable)return;
    selectDay(day);
    const target=L.toggleTarget(v.amount,v.rate);
    if(target===null)return openEditor(person,day,el);
    change([{person,day,amount:target}],(target?'Выдано: ':'Снято: ')+person.name+' · '+dm(day));
  }
  function payAll(){
    if(pop?.anchor===$('sd-bulk'))return closePop();
    const targets=L.bulkTargets(current,selectedDay);if(!targets.length)return;
    const sum=targets.reduce((total,item)=>total+Math.round(item.amount*100),0)/100, day=selectedDay;
    openPop($('sd-bulk'),box=>{
      box.append(node('p','sd-pop-q','Выдать по ставке '+targets.length+' сотрудникам за смену '+dm(L.previousDay(day))+'? Итого '+fmt(sum)+' сум.'));
      const actions=node('div','sd-pop-actions'), keep=node('button','pr-confirm-keep','Отмена'), yes=node('button','sd-pop-save','Выдать');
      keep.type=yes.type='button';actions.append(keep,yes);box.append(actions);
      keep.addEventListener('click',()=>closePop());
      yes.addEventListener('click',()=>{const work=change(targets.map(({person,amount})=>({person,day,amount})),'Выдано по ставке: '+targets.length+' чел. · '+dm(day));B?.button($('sd-bulk'),work);});
      return yes;
    });
  }

  /* ── Всплывашка у клетки: другая сумма или подтверждение ─────────────── */
  function closePop(){if(pop){pop.close();pop=null;}}
  function openPop(anchor,fill,extra={}){
    closePop();
    const box=node('div','sd-pop');box.setAttribute('role','dialog');document.body.append(box);
    const first=fill(box);
    const place=()=>{
      if(!anchor.isConnected)return closePop();
      const r=anchor.getBoundingClientRect(), w=box.offsetWidth, h=box.offsetHeight;
      const top=r.bottom+h+8>innerHeight?r.top-h-6:r.bottom+6;
      box.style.top=Math.max(8,Math.min(top,innerHeight-h-8))+'px';
      box.style.left=Math.min(Math.max(8,r.left+r.width/2-w/2),innerWidth-w-8)+'px';
    };
    const onKey=event=>{if(event.key==='Escape'){event.preventDefault();closePop();anchor.focus?.({preventScroll:true});}};
    const onDown=event=>{if(!box.contains(event.target)&&!anchor.contains(event.target))closePop();};
    pop={box,anchor,...extra,close(){box.remove();window.removeEventListener('scroll',place,true);window.removeEventListener('resize',place);
      document.removeEventListener('keydown',onKey);document.removeEventListener('pointerdown',onDown,true);anchor.classList.remove('is-editing');}};
    anchor.classList.add('is-editing');
    place();window.addEventListener('scroll',place,true);window.addEventListener('resize',place);
    document.addEventListener('keydown',onKey);document.addEventListener('pointerdown',onDown,true);
    // Фокус — после текущего нажатия, иначе Enter из клетки тут же нажал бы кнопку.
    setTimeout(()=>{if(box.isConnected&&first){first.focus({preventScroll:true});first.select?.();}},0);
    return box;
  }
  function openEditor(person,day,anchor,typed=''){
    const v=view(person,day);if(!v.editable)return;
    selectDay(day);
    const start=typed||(v.amount?fmt(v.amount):v.rate?fmt(v.rate):'');
    let input=null, error=null, ok=null, sure=false;
    const save=async()=>{
      const amount=L.parseAmount(input.value);
      if(amount===null){error.textContent='Введите неотрицательную сумму цифрами, не более двух знаков после запятой.';error.hidden=false;input.focus();return false;}
      // Лишний ноль — частая опечатка: сумму сильно выше ставки переспрашиваем.
      if(!sure&&amount>(v.rate?v.rate*3:3000000)){
        sure=true;ok.textContent='Да, сохранить';error.hidden=false;input.focus();
        error.textContent=(v.rate?'Сумма в '+fmt(Math.round(amount/v.rate))+' раз больше ставки.':'Сумма больше 3 000 000 сум.')+' Проверьте и нажмите ещё раз.';
        return false;
      }
      return change([{person,day,amount}],(amount?'Выдано: ':'Снято: ')+person.name+' · '+dm(day));
    };
    openPop(anchor,box=>{
      box.classList.add('is-editor');
      box.append(node('strong','sd-pop-who',person.name),node('span','sd-pop-sub','Выплата '+dm(day)+' · за смену '+dm(L.previousDay(day))));
      const field=node('label','sd-pop-field');field.append(node('span','','Сумма, сум'));
      input=node('input');input.inputMode='decimal';input.autocomplete='off';input.value=start;input.dataset.clean=v.amount?fmt(v.amount):'';input.dataset.saveIgnore='';
      input.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();save();}});
      input.addEventListener('input',()=>{error.hidden=true;sure=false;ok.textContent='Сохранить';});
      field.append(input);error=node('span','sd-pop-error');error.hidden=true;error.setAttribute('role','alert');
      const quick=node('div','sd-pop-quick');
      if(v.rate){const chip=node('button','sd-pop-chip','По ставке '+fmt(v.rate));chip.type='button';chip.addEventListener('click',()=>change([{person,day,amount:v.rate}],'Выдано: '+person.name+' · '+dm(day)));quick.append(chip);}
      if(v.amount){const chip=node('button','sd-pop-chip is-off','Не выдано');chip.type='button';chip.addEventListener('click',()=>change([{person,day,amount:0}],'Снято: '+person.name+' · '+dm(day)));quick.append(chip);}
      const actions=node('div','sd-pop-actions'), keep=node('button','pr-confirm-keep','Отмена');ok=node('button','sd-pop-save','Сохранить');
      keep.type=ok.type='button';keep.addEventListener('click',()=>{closePop();anchor.focus({preventScroll:true});});ok.addEventListener('click',save);actions.append(keep,ok);
      box.append(field,error);if(quick.children.length)box.append(quick);box.append(actions);
      if(typed)setTimeout(()=>{input.setSelectionRange(input.value.length,input.value.length);},0);
      return input;
    },{save,dirty:()=>input&&L.parseAmount(input.value)!==L.parseAmount(input.dataset.clean)});
  }

  /* ── Клетки: мышь, палец, клавиатура ─────────────────────────────────── */
  const grid=$('sheet-grid');
  const cellOf=target=>target.closest?.('button.sd-tick');
  const targetOf=el=>{const person=personOf(Number(el.dataset.person));return person?{person,day:el.dataset.day}:null;};
  grid.addEventListener('click',event=>{
    const edit=event.target.closest('.sd-edit');
    if(edit){const el=edit.previousElementSibling, t=targetOf(el);if(t)openEditor(t.person,t.day,el);return;}
    const el=cellOf(event.target);if(!el)return;
    if(pressed===el){pressed=null;return;}   // долгое нажатие уже открыло сумму
    if(pop?.anchor===el){closePop();return;} // повторный клик закрывает всплывашку
    const t=targetOf(el);if(t)toggle(t.person,t.day,el);
  });
  grid.addEventListener('contextmenu',event=>{
    const el=cellOf(event.target);if(!el)return;
    event.preventDefault();clearTimeout(pressTimer);
    const t=targetOf(el);if(t){pressed=event.pointerType==='touch'||pressed===el?el:null;openEditor(t.person,t.day,el);}
  });
  grid.addEventListener('pointerdown',event=>{
    const el=cellOf(event.target);pressed=null;clearTimeout(pressTimer);
    if(!el||event.pointerType==='mouse')return;
    pressTimer=setTimeout(()=>{const t=targetOf(el);if(t){pressed=el;navigator.vibrate?.(10);openEditor(t.person,t.day,el);}},450);
  });
  for(const type of ['pointerup','pointercancel','pointerleave'])grid.addEventListener(type,()=>clearTimeout(pressTimer));
  grid.addEventListener('scroll',()=>clearTimeout(pressTimer),true);
  grid.addEventListener('focusin',event=>{const el=cellOf(event.target);if(el)selectDay(el.dataset.day);});
  function move(el,dx,dy){
    const ids=current.people.map(p=>p.id), days=current.days;
    let row=ids.indexOf(Number(el.dataset.person)), col=days.indexOf(el.dataset.day);
    for(let step=0;step<Math.max(ids.length,days.length);step++){
      row+=dy;col+=dx;if(row<0||row>=ids.length||col<0||col>=days.length)return;
      const next=document.getElementById(cellId(ids[row],days[col]));
      if(next&&next.tagName==='BUTTON'){next.focus();next.scrollIntoView({block:'nearest',inline:'nearest'});return;}
    }
  }
  grid.addEventListener('keydown',event=>{
    const el=cellOf(event.target);if(!el||event.altKey||event.ctrlKey||event.metaKey)return;
    const arrows={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]};
    if(arrows[event.key]){event.preventDefault();move(el,...arrows[event.key]);return;}
    const t=targetOf(el);if(!t)return;
    if(/^\d$/.test(event.key)){event.preventDefault();openEditor(t.person,t.day,el,event.key);}
    else if(event.key==='Delete'||event.key==='Backspace'){event.preventDefault();if(view(t.person,t.day).amount)change([{person:t.person,day:t.day,amount:0}],'Снято: '+t.person.name+' · '+dm(t.day));}
  });
  document.addEventListener('keydown',event=>{
    if((event.ctrlKey||event.metaKey)&&!event.shiftKey&&event.key.toLowerCase()==='z'&&undo&&!pop
      &&!event.target.closest?.('input,textarea,select,[contenteditable]')){event.preventDefault();revert();}
  });
  $('sd-bulk').addEventListener('click',payAll);
  $('sd-undo').addEventListener('click',revert);

  function navigate(month){if(current&&globalThis.RetroSave?.confirmLeave()===false){$('month-input').value=current.month;return;}const work=loadMonth(month);if(B&&!$('salary-body').hidden)B.section($('salary-body'),work);}
  $('month-prev').addEventListener('click',()=>navigate(L.shiftMonth($('month-input').value,-1)));
  $('month-next').addEventListener('click',()=>navigate(L.shiftMonth($('month-input').value,1)));
  $('month-input').addEventListener('change',()=>navigate($('month-input').value));
  $('salary-refresh').addEventListener('click',()=>navigate($('month-input').value));
  // «Сохранить» дописывает сумму, набранную во всплывашке; отметки галочками уже записаны.
  globalThis.RetroSave?.register(grid,async()=>pop?.save?await pop.save():true,{dirty:()=>!!pop?.dirty?.()});
  (async()=>{try{today=(await globalThis.RetroConfig).today;const params=new URLSearchParams(location.search);selectedDay=params.get('date');const month=params.get('month')||selectedDay?.slice(0,7)||today.slice(0,7);$('month-input').value=month;await loadMonth(month);}catch(error){$('salary-loading').hidden=true;message(error.message,true);}})();
})();
