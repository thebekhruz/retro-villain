/* Сменные выплаты вводятся бухгалтером напрямую, без автоначисления. */
(() => {
  const $=id=>document.getElementById(id), L=globalThis.SalaryDayLogic, B=globalThis.RetroBusy;
  const formatter=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2});
  const fmt=value=>formatter.format(value), dm=day=>day.slice(8,10)+'.'+day.slice(5,7);
  const node=(tag,cls,text)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text!=null)el.textContent=text;return el;};
  let current=null, today=null, selectedDay=null, sequence=0, controller=null, writes=0, queue=Promise.resolve();
  const inflight=new Map();
  const monthTitle=month=>new Intl.DateTimeFormat('ru-RU',{month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(month+'-01T12:00:00Z'));
  function message(text,error=false){const el=$('salary-message');el.textContent=text;el.hidden=!text;el.classList.toggle('is-error',error);el.setAttribute('role',error?'alert':'status');}
  function controls(){['month-prev','month-next','month-input','salary-refresh'].forEach(id=>$(id).disabled=writes>0);if(today)$('month-next').disabled=writes>0||$('month-input').value>=today.slice(0,7);}
  function dirty(input){return L.parseAmount(input.value)!==L.parseAmount(input.dataset.clean);}
  function totals(){
    const view=L.matrix(current);
    $('salary-total').textContent=fmt(view.total)+' сум';
    $('salary-selected').textContent=fmt(view.perDay[selectedDay]||0)+' сум';
    $('salary-people').textContent=fmt(view.people.length);
    $('selected-title').textContent='Выдано '+dm(selectedDay);
    $('selected-shift').textContent='За смену '+dm(L.previousDay(selectedDay));
    view.people.forEach(person=>{const el=$('sd-person-'+person.id);if(el)el.textContent=fmt(person.paid);});
    current.days.forEach(day=>{const el=$('sd-total-'+day);if(el)el.textContent=view.perDay[day]?fmt(view.perDay[day]):'—';});
    if($('sd-grand'))$('sd-grand').textContent=fmt(view.total);
  }
  function selectDay(day){
    selectedDay=day;
    $('sheet-grid').querySelectorAll('[data-day]').forEach(el=>{
      el.classList.toggle('is-selected',el.dataset.day===day);
      if(el.tagName==='BUTTON')el.setAttribute('aria-pressed',String(el.dataset.day===day));
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
    view.people.forEach(person=>{
      const row=node('div','pr-row is-monthly');row.setAttribute('role','row');
      const who=node('div','pr-c pr-c-name');who.setAttribute('role','rowheader');who.append(node('span','pr-name',person.name),node('span','pr-role',person.role||''));
      if(person.archived)who.append(node('span','pr-role','Архив'));
      const rate=node('div','pr-c pr-c-sum',person.rate==null?'—':fmt(Number(person.rate)));rate.setAttribute('role','cell');row.append(who,rate);
      person.cells.forEach(cell=>{
        const cls='pr-m'+(cell.amount?' is-filled':'')+(cell.day===today?' is-today':'');
        const wrap=node('div','sd-cell');wrap.setAttribute('role','cell');
        if(!cell.editable){const value=node('div',cls+' sd-locked',cell.amount?fmt(cell.amount):'—');value.dataset.day=cell.day;value.title=current.closed?'Месяц закрыт — только для чтения':cell.day<current.entry_start?'История — только для просмотра':cell.day>today?'Будущая выплата':person.archived?'Сотрудник в архиве':'Эта дата доступна только для просмотра.';wrap.append(value);}
        else {
          const input=node('input',cls);input.inputMode='decimal';input.autocomplete='off';input.value=cell.amount?fmt(cell.amount):'';input.dataset.clean=input.value;input.dataset.day=cell.day;input.dataset.person=String(person.id);input.dataset.busyKey='sd:'+person.id+':'+cell.day;
          input.setAttribute('aria-label',person.name+' · выплата '+dm(cell.day)+' за смену '+dm(cell.workDay));
          input.addEventListener('focus',()=>selectDay(cell.day));
          input.addEventListener('change',()=>{const work=commit(person.id,cell.day,input);globalThis.RetroSave?.track(work);});
          input.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();input.blur();}if(event.key==='Escape'){input.value=input.dataset.clean;B?.clear(input.dataset.busyKey);input.blur();}});
          wrap.append(input);
        }
        row.append(wrap);
      });
      const paid=node('div','pr-c pr-c-paid',fmt(person.paid));paid.id='sd-person-'+person.id;paid.setAttribute('role','cell');row.append(paid);grid.append(row);
    });
    const foot=node('div','pr-row is-foot');foot.setAttribute('role','row');
    const title=node('div','pr-c pr-c-label','Выдано за день');title.setAttribute('role','rowheader');foot.append(title,node('div','pr-c pr-c-sum pr-c-sumfoot'));
    current.days.forEach(day=>{const el=node('div','pr-t'+(day===today?' is-today':''));el.id='sd-total-'+day;el.setAttribute('role','cell');foot.append(el);});
    const grand=node('div','pr-c pr-c-grand');grand.id='sd-grand';grand.setAttribute('role','cell');foot.append(grand);grid.append(foot);
    selectDay(selectedDay);
    const entry=new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(current.entry_start+'T12:00:00Z')).replace(/\.$/,'');
    $('entry-note').textContent=current.closed?'Месяц закрыт — только для чтения':'Ручной ввод выплат с '+entry+'. В столбце указана дата выплаты за предыдущую смену. Прежние общие выплаты без сотрудника сохранены в «Финансах дня».';
  }
  function commit(personId,day,input){
    const key=input.dataset.busyKey;if(inflight.has(key))return inflight.get(key);
    if(!current||!dirty(input))return Promise.resolve(true);
    const amount=L.parseAmount(input.value), saved=input.dataset.clean, data=current;
    if(amount===null){input.value=saved;B?.clear(key);message('Введите неотрицательную сумму цифрами, не более двух знаков после запятой.',true);return Promise.resolve(false);}
    const person=data.people.find(p=>p.id===personId);
    if(!person||!L.canEdit(data,person,day)){input.value=saved;message('Эта дата доступна только для просмотра.',true);return Promise.resolve(false);}
    const expected=L.parseAmount(person.cells?.[day]?.amount??0);
    writes++;controls();input.disabled=true;
    const work=queue.then(async()=>{
      try {
        const response=await fetch('/api/accountant/salary-day/cell',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({date:day,employee_id:personId,amount:String(amount),expected_amount:String(expected)})});
        const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Не удалось сохранить выплату.');
        const confirmed=result.amount==null?null:L.parseAmount(result.amount);if(confirmed===null)throw new Error('Не удалось проверить сохранённую сумму. Обновите ведомость.');
        if(current!==data)return true;
        person.cells=person.cells||{};person.cells[day]={amount:String(confirmed),work_day:result.work_day||L.previousDay(day),editable:result.editable!==false};
        input.value=confirmed?fmt(confirmed):'';input.dataset.clean=input.value;totals();message('');return true;
      } catch(error){if(current===data){input.value=saved;input.dataset.clean=saved;message(error.message,true);}return false;}
      finally{writes--;controls();input.disabled=false;inflight.delete(key);}
    });
    queue=work.catch(()=>false);inflight.set(key,work);
    if(B)B.field(input,work,{row:input.closest('.pr-row'),restore:true}).catch(()=>{});
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
      const focus=$('sheet-grid').querySelector('button[data-day="'+scrollDay+'"]');if(focus)$('sheet-scroll').scrollLeft=Math.max(0,focus.parentElement.offsetLeft-380);
      return true;
    }catch(error){if(token===sequence&&error.name!=='AbortError'){if(current)$('month-input').value=current.month;message(error.message,true);}return false;}
    finally{if(token===sequence){$('salary-loading').hidden=true;$('salary-body').setAttribute('aria-busy','false');$('sheet-grid').inert=false;}}
  }
  function navigate(month){if(current&&globalThis.RetroSave?.confirmLeave()===false){$('month-input').value=current.month;return;}const work=loadMonth(month);if(B&&!$('salary-body').hidden)B.section($('salary-body'),work);}
  $('month-prev').addEventListener('click',()=>navigate(L.shiftMonth($('month-input').value,-1)));
  $('month-next').addEventListener('click',()=>navigate(L.shiftMonth($('month-input').value,1)));
  $('month-input').addEventListener('change',()=>navigate($('month-input').value));
  $('salary-refresh').addEventListener('click',()=>navigate($('month-input').value));
  globalThis.RetroSave?.register($('sheet-grid'),async()=>{for(const input of $('sheet-grid').querySelectorAll('input'))if(dirty(input)&&!await commit(Number(input.dataset.person),input.dataset.day,input))return false;return true;},{dirty:()=>[...$('sheet-grid').querySelectorAll('input')].some(dirty)});
  (async()=>{try{today=(await globalThis.RetroConfig).today;const params=new URLSearchParams(location.search);selectedDay=params.get('date');const month=params.get('month')||selectedDay?.slice(0,7)||today.slice(0,7);$('month-input').value=month;await loadMonth(month);}catch(error){$('salary-loading').hidden=true;message(error.message,true);}})();
})();
