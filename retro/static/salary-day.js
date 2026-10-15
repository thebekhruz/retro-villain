/* «Зарплата · день»: клетка = день смены, новая выдача = расход сегодня.
   Первый клик — ✓ выдано по ставке смены. Второй клик — поле суммы прямо в клетке
   (опоздал, штраф — выдать меньше). Третий клик — пустая клетка, выплаты нет. Опоздавшие за смену
   подсвечены розовым, ставка при этом не меняется. Пишется сразу.
   До выдачи в клетке — посещаемость смены, как в «Сотрудниках»: время входа, «нет», «?»
   или «был»; выдано — галочка, а смена остаётся в подсказке. */
(() => {
  const $=id=>document.getElementById(id), L=globalThis.SalaryDayLogic, B=globalThis.RetroBusy;
  const formatter=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2});
  const fmt=value=>formatter.format(value), dm=day=>day.slice(8,10)+'.'+day.slice(5,7);
  const node=(tag,cls,text)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text!=null)el.textContent=text;return el;};
  let current=null, today=null, selectedDay=null, sequence=0, controller=null, writes=0, queue=Promise.resolve();
  let editing=null;
  let loading=false, calendarDay=null, dateCheck=null;
  // Группы — как в «Сотрудниках»; порядок тот же, незнакомые — в конце. T-470: группы —
  // сворачиваемые заголовки в самой таблице (сначала раскрыты все), поиск — по всем группам.
  const GROUP_ORDER=['Управление','Встреча гостей','Кухня','Обслуживание зала','Бар','Присмотр за детьми','Уборка','Охрана'];
  const filter={q:''}, collapsed=new Set(), heads=new Map(), E=globalThis.EmployeesLogic;
  let groups=[];
  const groupOf=person=>person.group||'Без группы';
  // Свёрнутая группа — не фильтр: её люди входят в итоги, фильтрует только поиск.
  const visible=person=>!E||E.matchesQuery(filter.q,person.name,person.role);
  const isFiltered=()=>!!filter.q.trim();
  const inflight=new Map();
  const monthTitle=month=>new Intl.DateTimeFormat('ru-RU',{month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(month+'-01T12:00:00Z'));
  function message(text,error=false){
    const el=$('salary-message');el.textContent=text;el.hidden=!text;el.classList.toggle('is-error',error);el.setAttribute('role',error?'alert':'status');
    // Клетку жмут внизу таблицы — строка сверху не видна, ошибку дублирует тост.
    if(error&&text)globalThis.RetroToast?.show(text,'error');
  }
  // Успех — тостом: строка над таблицей остаётся для ошибок и предупреждений.
  const done=text=>{message('');globalThis.RetroToast?.show(text,'ok');};
  function controls(){['month-prev','month-next','month-input','salary-refresh','salary-download'].forEach(id=>$(id).disabled=writes>0);$('month-prev').disabled=writes>0||$('month-input').value<='2026-10';if(today)$('month-next').disabled=writes>0||$('month-input').value>=today.slice(0,7);}
  const personOf=id=>current?.people.find(p=>p.id===id);
  const cellId=(personId,day)=>'sd-c-'+personId+'-'+day;
  function view(person,day){
    const amount=L.parseAmount(person.cells?.[day]?.amount??0)||0, rate=L.rateOf(person,day);
    return {amount,rate,state:L.displayState(person,day),editable:L.canEdit(current,person,day),attendance:L.attendanceOf(person,day),
      extra:L.extraOf(current,person.id,day),outside:L.isOutside(person,day)};
  }
  const MARK_CLASS={on_time:' is-ontime',absent:' is-absent',unknown:' is-unknown',manual:' is-manual'};
  /* Клетка — ячейка «Зарплаты · месяц» (.pr-m): белая, сегодняшний столбец подсвечен,
     ✓ по ставке — на зелёном, другая сумма — число на жёлтом, опоздал — розовая.
     Не выдано — посещаемость смены: вовремя — время зелёным, опоздал — время на розовом,
     «нет» — не пришёл, «?» — нет данных, «был» — отмечен вручную. Подсказка — словами. */
  function paintCell(person,day,el=document.getElementById(cellId(person.id,day))){
    if(!el)return;
    const v=view(person,day), cell=person.cells?.[day]||{};
    const shift=v.attendance, late=shift?.status==='late'&&v.state!=='odd';
    const mark=v.state==='off'?L.shiftMark(shift):'';
    const keep=[...el.classList].filter(cls=>cls.startsWith('rm-')).map(cls=>' '+cls).join('');
    el.className='pr-m sd-m'+(v.state==='on'?' is-filled is-tick':v.state==='odd'?' is-odd':'')+(late?' is-late':'')
      +(mark&&!late?MARK_CLASS[shift.status]||'':'')
      // Смена вне периода временного (T-434) — заперта и серая, как будущая клетка.
      +(day===today?' is-today':'')+(day>today||v.outside?' is-future':'')+(v.editable?'':' is-locked')+(v.extra?' has-extra':'')+keep;
    el.textContent=v.state==='on'?'✓':v.state==='odd'?fmt(v.amount):mark;
    if(v.amount&&cell.paid_days?.length)el.append(node('small','sd-payout-date','выдано '+cell.paid_days.map(dm).join(', ')));
    // Доп. выплата (записанная до T-470) в клетку не входит — «+» в углу и подсказка, сумма — в «Выдано».
    const dates='Смена '+dm(day)+' · '+(v.amount?'выдано '+(cell.paid_days||[cell.paid_day]).filter(Boolean).map(dm).join(', '):'новая выплата — расход '+dm(today));
    const title=[dates,v.outside?L.outsideTitle(person):'',L.shiftTitle(day,shift,v.amount,'shift'),v.rate?'Ставка за смену: '+fmt(v.rate)+' сум':'',v.extra?'Доп. выплата '+fmt(v.extra)+' сум — входит в «Выдано» и в Excel':'']
      .filter(Boolean).join('\n');
    if(title)el.title=title;else el.removeAttribute('title');
    if(v.editable){
      el.setAttribute('aria-pressed',String(v.state!=='off'));
      el.setAttribute('aria-label',person.name+' · '+dates
        +' · '+(v.amount?'выдано '+fmt(v.amount)+' сум':'не выдано')+(shift?' · '+L.shiftText(shift):'')
        +(v.extra?' · доп. выплата '+fmt(v.extra)+' сум':''));
    }
  }
  function makeCell(person,day){
    const editable=L.canEdit(current,person,day), el=node(editable?'button':'div');
    el.id=cellId(person.id,day);el.dataset.day=day;
    if(editable){el.type='button';el.dataset.person=String(person.id);el.dataset.busyKey='sd:'+person.id+':'+day;}
    paintCell(person,day,el);
    return el;
  }
  /* Заголовок группы: «Кухня · 12», по клику — свернуть или раскрыть. */
  function groupHead(group){
    const row=node('div','pr-section sd-group');row.setAttribute('role','row');
    const cell=node('div','sd-group-cell');cell.setAttribute('role','rowheader');
    const button=node('button','sd-group-btn');button.type='button';
    const label=node('span','sd-group-label');button.append(node('span','sd-group-caret'),label);
    button.addEventListener('click',()=>{if(collapsed.has(group.name))collapsed.delete(group.name);else collapsed.add(group.name);applyFilter();});
    cell.append(button);row.append(cell);
    heads.set(group.name,{row,button,label});
    return row;
  }
  /* Отфильтрованные строки прячутся; итог по дням внизу — по видимым. */
  function applyFilter(){
    const query=filter.q.trim();let shown=0;
    groups.forEach(group=>{
      const state=L.groupState(group,query,collapsed,visible), head=heads.get(group.name);
      if(head){head.row.hidden=state.hidden;head.button.setAttribute('aria-expanded',String(state.open));head.label.replaceChildren(node('span','',group.name),node('small','sd-group-count',state.count));
        // С поиском группы раскрывает он сам; свернуть вручную — после сброса поиска.
        head.button.disabled=!!query;}
      group.people.forEach(person=>{const row=$('sd-row-'+person.id);if(!row)return;const ok=visible(person);row.hidden=!ok||!state.open;if(ok)shown++;});
    });
    $('sd-nothing').hidden=shown>0||!current.people.length;
    const fold=$('sd-fold-all');fold.hidden=groups.length<2||!!query;
    fold.textContent=groups.length&&groups.every(group=>collapsed.has(group.name))?'Развернуть все':'Свернуть все';
    totals();
  }
  function totals(){
    const view=L.matrix(current);
    $('salary-total').textContent=fmt(view.total)+' сум';
    $('salary-selected').textContent=fmt(view.perDay[selectedDay]||0)+' сум';
    $('salary-people').textContent=fmt(view.people.length);
    $('selected-title').textContent='Выдано за смену '+dm(selectedDay);
    $('selected-shift').textContent='Новые выплаты — расход '+dm(today);
    view.people.forEach(person=>{const el=$('sd-person-'+person.id);if(el)el.textContent=fmt(person.paid);});
    const filtered=isFiltered();
    const shown=view.people.filter((summary,index)=>visible(current.people[index]));
    // Итог столбца — обычные и дополнительные выплаты за эту смену.
    current.days.forEach((day,col)=>{const el=$('sd-total-'+day);if(!el)return;
      const sum=shown.reduce((total,person)=>total+Math.round((person.cells[col].amount+person.cells[col].extra)*100),0)/100;el.textContent=sum?fmt(sum):'';});
    // Итог месяца уже в карточке сверху; внизу он нужен только по найденным.
    if($('sd-grand'))$('sd-grand').textContent=filtered?fmt(shown.reduce((total,person)=>total+Math.round(person.paid*100),0)/100)+' сум':'';
    if($('sd-foot-label'))$('sd-foot-label').textContent=filtered?'За смену · по поиску':'Выдано за смену';
  }
  function selectDay(day){
    selectedDay=day;
    $('sheet-grid').querySelectorAll('.pr-day').forEach(el=>{el.classList.toggle('is-selected',el.dataset.day===day);el.setAttribute('aria-pressed',String(el.dataset.day===day));});
    totals();
  }
  function render(){
    editing=null;rateEdit=null;
    const grid=$('sheet-grid'), view=L.matrix(current);grid.replaceChildren();
    grid.style.setProperty('--days',String(current.days.length));
    // Пустая ведомость — шапка и строка «＋ Добавить сотрудника» всё равно видны.
    $('sheet-empty').hidden=!!view.people.length;
    // Шапка — как у «Зарплаты · месяц»: число и день недели, «сегодня», вчера светло-зелёным.
    const head=node('div','pr-row is-head');head.setAttribute('role','row');
    for(const [cls,title] of [['pr-c pr-c-name','Сотрудник'],['pr-c pr-c-sum','Ставка']]){const el=node('div',cls,title);el.setAttribute('role','columnheader');head.append(el);}
    const yesterday=L.previousDay(today);
    current.days.forEach(day=>{
      const cell=node('div','sd-header-cell');cell.setAttribute('role','columnheader');
      const future=day>today, button=node(future?'div':'button','pr-day'+(day===today?' is-today':'')+(day===yesterday?' is-shift':'')+(future?' is-future':''));button.dataset.day=day;
      button.append(node('strong','',String(Number(day.slice(8,10)))),node('span','',day===today?'сегодня':new Intl.DateTimeFormat('ru-RU',{weekday:'short',timeZone:'UTC'}).format(new Date(day+'T12:00:00Z'))));
      button.append(node('small','sd-paid-date','смена'));
      if(!future){button.type='button';button.setAttribute('aria-label','Смена '+dm(day));button.addEventListener('click',()=>selectDay(day));}
      cell.append(button);head.append(cell);
    });
    const paidHead=node('div','pr-c pr-c-paid','Выдано');paidHead.setAttribute('role','columnheader');head.append(paidHead);grid.append(head);
    // В матрице клетки — массив для подсчёта; рисуем и пишем по исходным данным.
    // current.people уже разложен по группам (loadMonth) — строки идут под своими заголовками.
    heads.clear();
    const firstOf=new Map(groups.map(group=>[group.people[0]?.id,group]));
    view.people.forEach((summary,index)=>{
      const person=current.people[index];
      if(firstOf.has(person.id))grid.append(groupHead(firstOf.get(person.id)));
      const row=node('div','pr-row is-monthly');row.setAttribute('role','row');row.id='sd-row-'+person.id;row.dataset.group=groupOf(person);
      const who=node('div','pr-c pr-c-name');who.setAttribute('role','rowheader');
      // Должность, «временный», период и «архив» — отдельными узлами: переводчик берёт их по одному,
      // а строка переносится между ними, а не посреди «08.10–10.10».
      const role=node('span','pr-role');[person.role,...L.typeTag(person).split(' · '),person.archived?'архив':''].filter(Boolean).forEach((part,index)=>{if(index)role.append(' · ');role.append(node('span','',part));});
      who.append(node('span','pr-name',person.name),role);
      if(person.no_hikvision)who.append(cameTag(person));
      const rate=rateCell(person);rate.setAttribute('role','cell');row.append(who,rate);
      summary.cells.forEach(cell=>{const wrap=node('div','sd-cell');wrap.setAttribute('role','cell');wrap.append(makeCell(person,cell.day));row.append(wrap);});
      const paid=node('div','pr-c pr-c-paid rm-num',fmt(summary.paid));paid.id='sd-person-'+person.id;paid.setAttribute('role','cell');row.append(paid);grid.append(row);
    });
    grid.append(addRow());
    const foot=node('div','pr-row is-foot');foot.setAttribute('role','row');
    const title=node('div','pr-c pr-c-label','Выдано за смену');title.id='sd-foot-label';title.setAttribute('role','rowheader');foot.append(title,node('div','pr-c pr-c-sum pr-c-sumfoot'));
    current.days.forEach(day=>{const el=node('div','pr-t rm-num'+(day===today?' is-today':''));el.id='sd-total-'+day;el.setAttribute('role','cell');foot.append(el);});
    const grand=node('div','pr-c pr-c-grand rm-num');grand.id='sd-grand';grand.setAttribute('role','cell');foot.append(grand);grid.append(foot);
    applyFilter();paintAddHint();
    selectDay(selectedDay);
    // Правила клика и дат — в «?» у легенды (T-470); над таблицей — только «месяц закрыт».
    $('entry-note').textContent=current.closed?'Месяц закрыт — только для чтения':'';
    $('entry-note').hidden=!current.closed;
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
        const response=await fetch('/api/accountant/salary-day/shift-cell',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({work_day:day,employee_id:person.id,amount:String(amount),expected_amount:String(before)})});
        const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Не удалось сохранить выплату.');
        const confirmed=result.amount==null?null:L.parseAmount(result.amount);if(confirmed===null)throw new Error('Не удалось проверить сохранённую сумму. Обновите ведомость.');
        if(current===data)person.cells[day]={...cell,amount:String(confirmed),work_day:result.work_day,paid_days:confirmed?[result.date]:[],paid_day:confirmed?result.date:today,editable:result.editable!==false};
        globalThis.RetroCashToday?.refresh();
        return true;
      } catch(error){if(current===data){person.cells[day]=cell;message(error.message,true);}return false;}
      finally{writes--;inflight.delete(key);controls();if(current===data){paintCell(person,day);totals();}}
    });
    queue=work.catch(()=>false);inflight.set(key,work);
    globalThis.RetroSave?.track(work);
    return work;
  }
  async function loadMonth(month,{asOf=today,followLatest=false,preserveDraft=false}={}){
    if(writes){if(current)$('month-input').value=current.month;message('Дождитесь сохранения выплат.',true);return false;}
    if(!/^\d{4}-(0[1-9]|1[0-2])$/.test(month)||month<'2026-10'||month>asOf.slice(0,7)){if(current)$('month-input').value=current.month;message('Выберите текущий или прошедший месяц.',true);return false;}
    const requestedOn=L.tashkentDay(new Date());loading=true;
    const token=++sequence;controller?.abort();controller=new AbortController();$('salary-body').setAttribute('aria-busy','true');$('sheet-grid').inert=true;
    try{
      const response=await fetch('/api/accountant/salary-day/month?month='+encodeURIComponent(month)+'&basis=shift',{signal:controller.signal,cache:'no-store'});
      const data=await response.json();if(!response.ok)throw new Error(data.detail||'Не удалось загрузить ведомость.');
      if(token!==sequence)return false;
      if(preserveDraft&&(editing||rateEdit||writes||addBusy()||globalThis.RetroSave?.pending().length))return false;
      groups=L.groupPeople(data.people,GROUP_ORDER,groupOf);data.people=groups.flatMap(group=>group.people);
      current=data;today=data.today;calendarDay=requestedOn;
      const latest=today;
      selectedDay=followLatest&&data.days.includes(latest)?latest:data.days.includes(selectedDay)?selectedDay:data.days.includes(latest)?latest:data.days.at(-1);
      $('month-input').value=data.month;$('month-input').max=today.slice(0,7);$('month-label').textContent=monthTitle(month);$('crumb-month').textContent='Зарплата · день';
      render();$('salary-body').hidden=false;message('');globalThis.RetroCashToday?.refresh();$('connection').classList.remove('rm-status-skel');$('connection').textContent='Ручная ведомость · '+monthTitle(month);controls();
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
    finally{if(token===sequence){loading=false;$('salary-loading').hidden=true;$('salary-body').setAttribute('aria-busy','false');$('sheet-grid').inert=false;}}
  }

  /* Оставленная на ночь ведомость обновляет сегодняшнюю дату расхода и смены.
     Дату подтверждает сервер; часы устройства только подсказывают, когда проверить.
     Начатую сумму, нового сотрудника и запись в пути не трогаем. Исторический день не переключаем. */
  function dateRefreshBlocked(){
    return !current||document.hidden||loading||writes>0||!!editing||!!rateEdit||addBusy()
      ||!!globalThis.RetroSave?.pending().length;
  }
  async function refreshDate(force=false){
    if(dateCheck)return dateCheck;
    if(dateRefreshBlocked()||(!force&&calendarDay===L.tashkentDay(new Date())))return false;
    dateCheck=(async()=>{
      try{
        const response=await fetch('/api/config',{cache:'no-store'});
        if(!response.ok)throw new Error('Не удалось определить настройки сервера.');
        const config=await response.json();
        // За время запроса бухгалтер мог начать ввод или переключить месяц.
        if(dateRefreshBlocked())return false;
        if(config.today===today){calendarDay=L.tashkentDay(new Date());return false;}
        const followLatest=selectedDay===today&&current.month===today.slice(0,7);
        return await loadMonth(followLatest?config.today.slice(0,7):current.month,{asOf:config.today,followLatest,preserveDraft:true});
      }catch(error){message(error.message,true);return false;}
      finally{dateCheck=null;}
    })();
    return dateCheck;
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
    input.setAttribute('aria-label',person.name+' · за смену '+dm(day)+', сум');
    el.replaceWith(input);
    editing={person,day,input,sure:false,clean:v.amount,typed:!!typed};
    input.focus({preventScroll:true});
    if(typed)input.setSelectionRange(input.value.length,input.value.length);else input.select();
    input.addEventListener('keydown',event=>{
      if(event.key==='Enter'){event.preventDefault();finishEdit(true);}
      else if(event.key==='Escape'){event.preventDefault();closeEdit(true);}
    });
    input.addEventListener('input',()=>{if(editing){editing.sure=false;editing.typed=true;}});
    // Третий клик: пока в поле ничего не набрано, клик по нему снимает выплату — клетка пустая.
    // Начал набирать — клик просто ставит курсор, набранное не теряется.
    input.addEventListener('pointerdown',event=>{
      if(!editing||editing.input!==input||editing.typed)return;
      event.preventDefault();
      const {person:who,day:when}=editing;closeEdit(true);write(who,when,0);
    });
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

  /* ── «＋ Добавить сотрудника» (T-470): строка после последнего сотрудника ──
     Имя, должность, группа и ставка вписываются прямо в таблицу. Тот же POST,
     что в «Сотрудниках»; похожие в реестре — тот же вопрос «Это другой человек?».
     После записи ведомость перечитывается — новый человек сразу в своей группе. */
  let adding=null;
  const addBusy=()=>!!adding&&(adding.saving||!!(adding.name+adding.role+adding.rate).trim());
  const groupChoices=()=>[...new Set([...GROUP_ORDER,...groups.map(group=>group.name)])].filter(name=>name!=='Без группы');
  function addRow(){
    const row=node('div','pr-section sd-add');row.setAttribute('role','row');
    const box=node('div','sd-add-box');box.setAttribute('role','cell');row.append(box);
    if(!adding){
      const open=node('button','sd-add-open','＋ Добавить сотрудника');open.type='button';open.disabled=!!current.closed;
      open.addEventListener('click',()=>{adding={name:'',role:'',group:'',rate:'',groupTouched:false,confirm:false,matches:null,error:'',saving:false};redrawAdd(true);});
      box.append(open);return row;
    }
    const form=node('form','sd-add-form');form.noValidate=true;
    const group=node('select','sd-add-group');group.setAttribute('aria-label','Группа');
    group.append(new Option('Группа',''),...groupChoices().map(name=>new Option(name,name)));group.value=adding.group;
    group.addEventListener('change',()=>{adding.group=group.value;adding.groupTouched=true;adding.error='';paintAddHint();});
    const field=(key,placeholder,label)=>{
      const input=node('input','sd-add-'+key);input.value=adding[key];input.placeholder=placeholder;input.autocomplete='off';
      input.setAttribute('aria-label',label);
      input.addEventListener('input',()=>{
        adding[key]=input.value;adding.confirm=false;adding.matches=null;adding.error='';
        // Группа подсказывается по должности, пока её не выбрали руками — как в «Сотрудниках».
        if(key==='role'&&!adding.groupTouched){const guess=E?.groupForRole(input.value);if(guess){adding.group=guess;group.value=guess;}}
        paintAddHint();
      });
      return input;
    };
    const name=field('name','Имя и фамилия','Имя нового сотрудника'), role=field('role','Должность','Должность');
    const roles=node('datalist');roles.id='sd-add-roles';role.setAttribute('list',roles.id);
    [...new Set(current.people.map(person=>person.role).filter(Boolean))].sort((a,b)=>a.localeCompare(b,'ru')).forEach(text=>roles.append(new Option(text,text)));
    const rate=field('rate','Ставка за смену','Ставка за смену, сум');rate.inputMode='decimal';rate.dataset.moneyHint='off';
    const submit=node('button','sd-add-submit','Добавить');submit.type='submit';
    const cancel=node('button','sd-add-cancel','Отмена');cancel.type='button';
    cancel.addEventListener('click',()=>{adding=null;redrawAdd();});
    form.append(name,role,roles,group,rate,submit,cancel);
    form.addEventListener('submit',event=>{event.preventDefault();submitAdd();});
    form.addEventListener('keydown',event=>{if(event.key==='Escape'&&!adding?.saving){event.preventDefault();adding=null;redrawAdd();}});
    const hint=node('div','sd-add-hint');hint.id='sd-add-hint';hint.setAttribute('aria-live','polite');
    box.append(form,hint);
    return row;
  }
  function redrawAdd(focus=false){
    const old=$('sheet-grid').querySelector('.sd-add');if(!old||!current)return;
    const row=addRow();old.replaceWith(row);paintAddHint();
    if(focus)row.querySelector('.sd-add-name')?.focus();
    else if(!adding)row.querySelector('.sd-add-open')?.focus({preventScroll:true});
  }
  /* Под строкой: ошибка, похожие в реестре или подсказка, что будет. */
  function paintAddHint(){
    const box=$('sd-add-hint');if(!box||!adding)return;
    box.replaceChildren();box.className='sd-add-hint';
    const submit=$('sheet-grid').querySelector('.sd-add-submit');
    if(submit){submit.disabled=adding.saving;submit.textContent=adding.matches?'Это другой человек — добавить':'Добавить';submit.classList.toggle('is-danger',!!adding.matches);}
    if(adding.error){box.textContent=adding.error;box.classList.add('is-bad');return;}
    if(adding.matches){
      box.classList.add('is-bad');
      box.append(node('span','','Похожие уже есть в реестре: '),
        ...adding.matches.map((person,index)=>node('span','',(index?'; ':'')+person.name+(E?.namesakeLine?' ('+E.namesakeLine(person)+')':''))),
        node('span','','. Если это тот же человек — нажмите «Отмена».'));
      return;
    }
    box.textContent='Появится в ведомости сразу. Hikvision и подробности карточки — в «Сотрудниках».';
  }
  async function submitAdd(){
    if(!adding)return true;
    if(adding.saving)return false;
    const draft=L.newPerson(adding);
    if(draft.error){adding.error=draft.error;paintAddHint();return false;}
    if(adding.matches)adding.confirm=true;
    adding.saving=true;paintAddHint();
    const button=$('sheet-grid').querySelector('.sd-add-submit');
    const work=(async()=>{
      const response=await fetch('/api/accountant/employees',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({...draft.body,confirm_new:adding.confirm})});
      const result=await response.json().catch(()=>({}));
      if(response.status===409&&Array.isArray(result.matches))return {matches:result.matches};
      if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Не удалось добавить сотрудника.');
      return {employee:result.employee};
    })();
    globalThis.RetroSave?.track(work);
    if(B&&button)B.button(button,work,{done:false}).catch(()=>{});
    try{
      const result=await work;
      if(result.matches){adding.saving=false;adding.matches=result.matches;paintAddHint();return false;}
      const who=draft.body.name;adding=null;
      // Новый человек должен быть виден: его группа раскрыта, поиск сброшен.
      collapsed.delete(result.employee.group||'Без группы');filter.q='';$('sd-search').value='';
      await loadMonth(current.month);
      loadPhotos();
      const row=$('sd-row-'+result.employee.id);
      if(row){row.classList.add('is-focus');row.scrollIntoView({block:'center',behavior:'smooth'});setTimeout(()=>row.classList.remove('is-focus'),2400);}
      done('Добавлен: '+who+'.');
      return true;
    }catch(error){if(adding){adding.saving=false;adding.error=error.message;paintAddHint();}return false;}
  }

  /* ── Ставка за смену (T-472): клик по ставке — поле прямо в ячейке ────────
     Enter или уход из поля — запись в реестр (та же карточка, что в «Сотрудниках»);
     Esc — отмена. Новая ставка действует с сегодняшней смены, прошлые клетки
     остаются по ставке своего дня. */
  const rateEditable=person=>!!current&&!current.closed&&!person.archived;
  function rateCell(person){
    const has=person.rate!=null&&!!Number(person.rate), text=has?fmt(Number(person.rate)):'нет ставки';
    if(!rateEditable(person))return node('div','pr-c pr-c-sum'+(has?' rm-num':' is-norate'),text);
    const cell=node('div','pr-c pr-c-sum sd-rate-cell'+(has?' rm-num':' is-norate'));
    const button=node('button','sd-rate',text);button.type='button';button.dataset.rate=String(person.id);
    button.title='Изменить ставку за смену';button.setAttribute('aria-label',person.name+' · ставка за смену '+text+' · изменить');
    cell.append(button);
    return cell;
  }
  let rateEdit=null;
  function startRate(person,cell){
    if(rateEdit||!rateEditable(person))return;
    const input=node('input','sd-rate-input');input.inputMode='decimal';input.autocomplete='off';input.dataset.moneyHint='off';
    input.value=person.rate&&Number(person.rate)?String(Number(person.rate)):'';
    input.setAttribute('aria-label',person.name+' · ставка за смену, сум');
    cell.replaceChildren(input);
    rateEdit={person,cell,input,saving:false};
    input.focus();input.select();
    input.addEventListener('keydown',event=>{
      if(event.key==='Enter'){event.preventDefault();saveRate();}
      else if(event.key==='Escape'){event.preventDefault();closeRate(true);}
    });
    input.addEventListener('blur',()=>{if(rateEdit&&rateEdit.input===input&&!rateEdit.saving)saveRate();});
  }
  function closeRate(refocus){
    if(!rateEdit)return;
    const {person,cell}=rateEdit;rateEdit=null;
    const fresh=rateCell(person);fresh.setAttribute('role','cell');cell.replaceWith(fresh);
    if(refocus)fresh.querySelector('button')?.focus({preventScroll:true});
  }
  async function saveRate(){
    if(!rateEdit||rateEdit.saving)return true;
    const {person,input}=rateEdit, amount=L.parseAmount(input.value), before=person.rate&&Number(person.rate)?Number(person.rate):null;
    if(!input.value.trim()||amount===before){closeRate(false);return true;}
    if(!amount){message('Ставку введите цифрами, больше нуля.',true);input.focus();return false;}
    rateEdit.saving=true;input.disabled=true;
    const work=(async()=>{
      const response=await fetch('/api/accountant/employees/'+encodeURIComponent(person.id),{method:'PATCH',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({rate:String(amount),group:person.group||null,reason:'Изменено в «Зарплате · день»'})});
      const result=await response.json().catch(()=>({}));
      if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Не удалось изменить ставку.');
    })();
    globalThis.RetroSave?.track(work);
    try{
      await work;
      rateEdit=null;
      // Ставки клеток — по версии реестра на день смены: месяц перечитываем целиком.
      await loadMonth(current.month);
      done(person.name+': ставка '+fmt(amount)+' сум.');
      return true;
    }catch(error){
      message(error.message,true);
      if(rateEdit){rateEdit.saving=false;input.disabled=false;input.focus();}
      return false;
    }
  }

  /* ── «Нет в Hikvision» (T-471): приход за сегодня отмечает бухгалтер ──────
     Жёлтая метка у имени и галочка «пришёл». Отметка пишется сразу и видна в
     клетке сегодняшнего дня: «был» или «нет». Денег не хватило сегодня — завтра
     выдают по этой отметке. Отметку не изменить, когда начисление за день уже
     подтверждено (сервер скажет почему). */
  const canMark=person=>!!current&&!current.closed&&!person.archived&&current.days.includes(today);
  function cameTag(person){
    const tag=node('span','sd-nohik');tag.append(node('span','sd-nohik-text','Нет в Hikvision'));
    if(canMark(person)){
      const button=node('button','sd-came');button.type='button';button.dataset.person=String(person.id);
      paintCame(button,person);tag.append(button);
    }
    return tag;
  }
  function paintCame(button,person){
    const came=person.today_mark===true;
    button.classList.toggle('is-on',came);button.classList.toggle('is-off',person.today_mark===false);
    button.setAttribute('aria-pressed',String(came));
    button.replaceChildren(node('span','sd-came-box',came?'✓':''),node('span','',person.today_mark===false?'не пришёл':'пришёл'));
    button.title=came?'Отмечено: пришёл сегодня, '+dm(today)+'. Нажмите, чтобы снять.':'Отметить: пришёл сегодня, '+dm(today);
    button.setAttribute('aria-label',person.name+' · '+button.title);
  }
  const marking=new Set();
  async function markCame(person,button){
    if(marking.has(person.id))return;
    const present=person.today_mark!==true, data=current;
    marking.add(person.id);
    const work=(async()=>{
      const response=await fetch('/api/accountant/manual-attendance',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({date:today,employee_id:person.id,present})});
      const result=await response.json().catch(()=>({}));
      if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Не удалось отметить приход.');
    })();
    globalThis.RetroSave?.track(work);
    if(B)B.button(button,work,{done:false}).catch(()=>{});
    try{
      await work;
      if(current!==data)return;
      person.today_mark=present;
      const cell=person.cells?.[today];
      if(cell)cell.attendance={status:present?'manual':'absent',time:null,source:present?'manual_present':'manual_absent'};
      paintCell(person,today);
      const fresh=$('sheet-grid').querySelector('.sd-came[data-person="'+person.id+'"]');if(fresh)paintCame(fresh,person);
      done(person.name+(present?' — пришёл сегодня.':' — не пришёл сегодня.'));
    }catch(error){message(error.message,true);}
    finally{marking.delete(person.id);}
  }

  /* ── Фото при наведении на имя (T-471) — те же снимки, что в «Сотрудниках» ── */
  let photos={};
  async function loadPhotos(){
    try{
      const response=await fetch('/api/accountant/photos',{cache:'no-store'});
      const data=response.ok?await response.json():null;
      if(data&&data.photos&&typeof data.photos==='object')photos=data.photos;
    }catch{/* без фото — просто без всплывающего снимка */}
  }
  let photoPop=null;
  function showPhoto(nameCell){
    const id=nameCell.closest('.pr-row')?.id?.replace('sd-row-',''), url=id&&photos[id];
    if(!url)return hidePhoto();
    if(!photoPop){photoPop=node('div','sd-photo-pop');photoPop.setAttribute('aria-hidden','true');photoPop.append(node('img'));document.body.append(photoPop);}
    const image=photoPop.firstChild;
    if(image.getAttribute('src')!==url){image.src=url;image.alt='';}
    image.onerror=()=>hidePhoto();
    const r=nameCell.getBoundingClientRect(), size=168;
    const top=Math.min(Math.max(8,r.top+r.height/2-size/2),innerHeight-size-8);
    photoPop.style.top=top+'px';photoPop.style.left=Math.min(r.right+10,innerWidth-size-8)+'px';
    photoPop.hidden=false;
  }
  function hidePhoto(){if(photoPop)photoPop.hidden=true;}

  /* ── Клетки: мышь, палец, клавиатура ─────────────────────────────────── */
  const grid=$('sheet-grid');
  const cellOf=target=>target.closest?.('button.sd-m');
  const targetOf=el=>{const person=personOf(Number(el.dataset.person));return person?{person,day:el.dataset.day}:null;};
  grid.addEventListener('click',event=>{
    const rateButton=event.target.closest?.('.sd-rate');
    if(rateButton){const person=personOf(Number(rateButton.dataset.rate));if(person)startRate(person,rateButton.parentElement);return;}
    const came=event.target.closest?.('.sd-came');
    if(came){const person=personOf(Number(came.dataset.person));if(person)markCame(person,came);return;}
    const el=cellOf(event.target);if(!el)return;
    const t=targetOf(el);if(!t)return;
    const v=view(t.person,t.day);if(!v.editable)return;
    selectDay(t.day);
    // Первый клик — ✓ по ставке; второй (или нет ставки) — поле суммы.
    if(!v.amount&&v.rate)write(t.person,t.day,v.rate);
    else startEdit(t.person,t.day,el);
  });
  grid.addEventListener('mouseover',event=>{
    const cell=event.target.closest?.('.pr-row.is-monthly>.pr-c-name');
    if(cell&&!cell.contains(event.relatedTarget))showPhoto(cell);
  });
  grid.addEventListener('mouseout',event=>{
    const cell=event.target.closest?.('.pr-row.is-monthly>.pr-c-name');
    if(cell&&!cell.contains(event.relatedTarget))hidePhoto();
  });
  $('sheet-scroll').addEventListener('scroll',hidePhoto,{passive:true});
  window.addEventListener('scroll',hidePhoto,{passive:true});
  function move(el,dx,dy){
    const ids=current.people.map(p=>p.id), days=current.days;
    let row=ids.indexOf(Number(el.dataset.person)), col=days.indexOf(el.dataset.day);
    for(;;){
      row+=dy;col+=dx;if(row<0||row>=ids.length||col<0||col>=days.length)return;
      const next=document.getElementById(cellId(ids[row],days[col]));
      if(next&&next.tagName==='BUTTON'&&!next.closest('.pr-row')?.hidden){next.focus();next.scrollIntoView({block:'nearest',inline:'nearest'});selectDay(days[col]);return;}
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

  /* «Скачать Excel» (Б-07): вся ведомость или то, что сейчас найдено поиском. */
  async function download(){
    if(!current)return false;
    const params=new URLSearchParams({month:current.month,basis:'shift'});
    if(filter.q.trim())params.set('q',filter.q.trim());
    try{
      const response=await fetch('/api/accountant/salary-day/export?'+params,{cache:'no-store'});
      if(!response.ok)throw new Error('Не удалось скачать ведомость.');
      const url=URL.createObjectURL(await response.blob()), link=document.createElement('a');
      link.href=url;link.download='Retro-salary-day-'+current.month+(isFiltered()?'-selection':'')+'.xlsx';
      document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
      done(isFiltered()?'Скачана выборка ведомости.':'Ведомость скачана.');
      return true;
    }catch(error){message(error.message,true);return false;}
  }
  $('salary-download').addEventListener('click',()=>{const work=download();if(B)B.button($('salary-download'),work).catch(()=>{});});
  $('sd-fold-all').addEventListener('click',()=>{
    if(groups.every(group=>collapsed.has(group.name)))collapsed.clear();else groups.forEach(group=>collapsed.add(group.name));
    applyFilter();
  });
  function navigate(month){if(current&&globalThis.RetroSave?.confirmLeave()===false){$('month-input').value=current.month;return;}const work=loadMonth(month);if(B&&!$('salary-body').hidden)B.section($('salary-body'),work);}
  $('sd-search').addEventListener('input',event=>{filter.q=event.target.value;if(current)applyFilter();});
  $('month-prev').addEventListener('click',()=>navigate(L.shiftMonth($('month-input').value,-1)));
  $('month-next').addEventListener('click',()=>navigate(L.shiftMonth($('month-input').value,1)));
  $('month-input').addEventListener('change',()=>navigate($('month-input').value));
  $('salary-refresh').addEventListener('click',()=>navigate($('month-input').value));
  // «Сохранить» дописывает сумму, набранную в клетке; галочки уже записаны.
  globalThis.RetroSave?.register(grid,async()=>editing?await finishEdit(true):true,
    {dirty:()=>!!editing&&L.parseAmount(editing.input.value)!==editing.clean});
  // Ставка в поле: «Сохранить» записывает её.
  globalThis.RetroSave?.register(grid,()=>saveRate(),{dirty:()=>!!rateEdit&&!rateEdit.saving});
  // Набранный новый сотрудник: «Сохранить» добавляет его, уход со страницы — переспрашивает.
  globalThis.RetroSave?.register(grid,()=>submitAdd(),{dirty:()=>addBusy()&&!adding.saving});
  window.addEventListener('focus',()=>refreshDate(true));
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshDate(true);});
  setInterval(()=>refreshDate(),30000);
  (async()=>{try{today=(await globalThis.RetroConfig).today;const params=new URLSearchParams(location.search);selectedDay=params.get('date');const requested=params.get('month')||selectedDay?.slice(0,7);const month=requested&&requested>='2026-10'&&requested<=today.slice(0,7)?requested:today.slice(0,7);$('month-input').value=month;loadPhotos();await loadMonth(month);}catch(error){$('salary-loading').hidden=true;message(error.message,true);}})();
})();
