/* Расчёты экрана «Финансы дня» без DOM: их легче проверить тестом, чем
   выковыривать из отрисовки. Сервер отдаёт факты, здесь — только их сборка. */
(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.AccountantLogic=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){

  /* Подотчёт Шоха за день.
     `entries` приходит только за выбранный день, а `balance` — уже на его
     конец, поэтому остаток на начало восстанавливаем из конца минус движения
     дня, а не суммированием прошлых записей (их в ответе нет). */
  function shohPosition(shoh){
    const entries=shoh&&shoh.entries?shoh.entries:[];
    const sum=kind=>entries.filter(e=>e.kind===kind)
      .reduce((total,e)=>total+Number(e.amount),0);
    const given=sum('deposit'), accepted=sum('withdrawal');
    const known=shoh&&shoh.balance!==null&&shoh.balance!==undefined;
    const balance=known?Number(shoh.balance):null;
    return {known,given,accepted,balance,start:known?balance-given+accepted:null};
  }

  /* Строки секции «Смена».
     Смену за вчера выдают сегодня, поэтому непогашенные начисления прошлых
     дней показываем всегда — даже когда начисления выбранного дня ещё не
     подтверждены. Итоги «за смену» считаем только по выбранному дню, иначе
     день выглядел бы дороже, чем в нём начислили. */
  function shiftRows(data){
    const ledger=data.ledger, day=data.date, confirmed=!!ledger.payroll_confirmed;
    const employees=data.employees||[], accruals=ledger.accruals||[];
    const byName=new Map(employees.map(row=>[row.name,row]));
    const fromAccrual=row=>{
      const employee=byName.get(row.name)||{};
      const own=row.work_day===day;
      return {
        id:row.id, name:row.name, role:employee.role||row.group||'',
        status:row.status||employee.status,
        // Время входа известно только для выбранного дня: реестр приходит за него.
        first_entry:own?(employee.first_entry===undefined?null:employee.first_entry):null,
        showEntry:own, rate:row.rate, accrued:row.amount, paid:row.paid, debt:row.debt,
        day:row.work_day, noHik:employee.hikvision_registered===false,
      };
    };
    const stale=accruals.filter(row=>row.work_day<day&&Number(row.debt)>0)
      .sort((a,b)=>a.work_day.localeCompare(b.work_day)||a.name.localeCompare(b.name,'ru'))
      .map(fromAccrual);
    const own=confirmed
      ?accruals.filter(row=>row.work_day===day)
        .sort((a,b)=>a.name.localeCompare(b.name,'ru')).map(fromAccrual)
      :employees.map(row=>({
        id:null, name:row.name, role:row.role, status:row.status, first_entry:row.first_entry,
        showEntry:true, rate:row.rate, accrued:row.payable, paid:null, debt:null,
        day:day, noHik:row.hikvision_registered===false,
      }));
    const rows=stale.concat(own);
    const total=(list,key)=>list.reduce((sum,row)=>sum+Number(row[key]||0),0);
    return {confirmed, stale, own, rows, totals:{
      accrued:total(own,'accrued'), paid:total(own,'paid'),
      owed:total(rows,'debt'), staleOwed:total(stale,'debt'),
      settled:own.filter(row=>Number(row.debt)===0).length, count:own.length,
    }};
  }

  /* Срезы-фильтры над строками смены. */
  function shiftTabs(rows){
    return [
      ['all','Все',rows.length],
      ['owed','К выдаче',rows.filter(r=>Number(r.debt)>0).length],
      ['late','Опоздали',rows.filter(r=>r.status==='late').length],
      ['missing','Не пришли',rows.filter(r=>r.status==='missing').length],
      ['norate','Без ставки',rows.filter(r=>r.rate===null).length],
    ].filter(([key,,count])=>key==='all'||count>0);
  }

  function matchesTab(row,tab){
    if(tab==='all')return true;
    if(tab==='owed')return Number(row.debt)>0;
    if(tab==='norate')return row.rate===null;
    return row.status===tab;
  }

  /* Проверки дня.
     Сознательно НЕ проверяем переплату, выдачу поверх нулевого начисления и
     сумму мимо ставки: сервер их не пропускает (выплата больше долга — 422, у
     «не пришёл» начисление 0), а ставка и сумма пишутся в начисление одним
     куском. «Зарплата без привязки» тоже не ошибка — так проходят оклады. */
  function dayChecks(data){
    const ledger=data.ledger, items=[];
    const add=(level,text,sub,accrualId)=>items.push({level,text,sub,
      accrualId:accrualId===undefined?null:accrualId});
    if(ledger.cash_balance!==null&&Number(ledger.cash_balance)<0)
      add('bad','Остаток ушёл в минус',{amount:ledger.cash_balance});
    const stale=(ledger.accruals||[]).filter(row=>row.work_day<data.date&&Number(row.debt)>0);
    if(stale.length)
      add('warn','Невыданные смены',{count:stale.length,
        amount:stale.reduce((sum,row)=>sum+Number(row.debt),0)},stale[0].id);
    if(data.missing_rates)
      add('warn','Без ставки',{count:data.missing_rates});
    if(!ledger.payroll_confirmed&&data.payroll.unknown_count===0&&(data.employees||[]).length)
      add('warn','Смена не подтверждена',{});
    if(Number(ledger.manual_debt_total)>0)
      add('warn','Неоплаченные расходы',{amount:ledger.manual_debt_total});
    if(data.expected_cashier===null)
      add('warn','Касса не передана',{});
    // Недельную цель ставит учредитель; отставание — повод отложить сегодня.
    const dividends=data.dividends_week;
    if(dividends&&dividends.behind)
      add('warn','Отстаём от недельных дивидендов',{amount:Number(dividends.due)-Number(dividends.collected)});
    return items;
  }

  /* Выплату нельзя записать без данных кассира за день — сервер откажет,
     поэтому строки запираем заранее, вместе с «Выдать всем». */
  function payLock(ledger,row){
    if(ledger.cash_balance===null)return 'no-cash';
    if(row&&!(Number(row.debt)>0))return 'settled';
    return null;
  }

  /* ── Экран «Финансы дня» (макет 2a) ──────────────────────────────────────
     Выбранная дата — день выплат P. Секция смены показывает вчерашнюю смену
     S = P − 1: её выдают сегодня. Всё ниже — чистые функции над ответами
     /day?date=P, /staff?date=S и /shokh/purchases?date=P. */

  const num=v=>Number(v||0);
  const ABSENT=new Set(['missing','manual_absent']);
  const MANUAL=new Set(['manual_present','manual_absent']);

  function shiftIso(day,delta){
    const d=new Date(day+'T12:00:00Z'); d.setUTCDate(d.getUTCDate()+delta);
    return d.toISOString().slice(0,10);
  }
  const dm=iso=>iso?iso.slice(8,10)+'.'+iso.slice(5,7):'';
  function plural(n,one,few,many){
    const a=Math.abs(n)%100, b=a%10;
    if(a>10&&a<20)return many; if(b===1)return one; if(b>1&&b<5)return few; return many;
  }
  // Время входа в ответе уже в часовом поясе Ташкента (+05:00), поэтому часы
  // берём из самой строки: браузер в другом поясе не должен их сдвигать.
  function entryClock(iso){ return iso?iso.slice(11,16):null; }
  function lateMinutes(iso){
    if(!iso)return 0; const h=Number(iso.slice(11,13)), m=Number(iso.slice(14,16));
    return Math.max(0,h*60+m-600);
  }

  /* Выплаты зарплаты за день приходят движениями «Метка · Имя · за ГГГГ-ММ-ДД».
     По ним узнаём, сколько по начислению выдано именно сегодня: уменьшить
     выдачу можно только удалив сегодняшние выплаты, прошлые дни закрыты. */
  function todaySalaryPayments(movements){
    const map=new Map();
    (movements||[]).filter(m=>m.type==='salary_payment').forEach(m=>{
      const hit=/^.* · (.+) · за (\d{4}-\d{2}-\d{2})$/.exec(m.description||'');
      if(!hit)return;
      const key=hit[1]+'|'+hit[2], list=map.get(key)||[];
      list.push({id:m.id,amount:num(m.amount)}); map.set(key,list);
    });
    return map;
  }

  function shiftBoard({payday,staff,accruals,movements}){
    const S=shiftIso(payday,-1);
    const staffRows=(staff&&staff.employees)||[];
    const byId=new Map(staffRows.map(r=>[r.employee_id,r]));
    const all=accruals||[];
    const own=all.filter(a=>a.work_day===S);
    const confirmed=own.length>0;
    const pays=todaySalaryPayments(movements);
    const build=(src)=>{
      const today=pays.get(src.name+'|'+src.day)||[];
      const paidToday=today.reduce((s,p)=>s+p.amount,0);
      const accrued=src.accrued===null||src.accrued===undefined?null:num(src.accrued);
      const paid=num(src.paid), debt=src.debt===null?(accrued||0):num(src.debt);
      const row={...src,accrued,paid,paidToday,paidBefore:Math.max(0,paid-paidToday),debt,
        todayPayments:today,noHik:MANUAL.has(src.status),
        time:src.own?entryClock(src.entry):null,late:src.status==='late'?lateMinutes(src.entry):0};
      let kind='ok', note='';
      // Без ставки — ошибка реестра; «нет данных» Hikvision объясняет полоса
      // над таблицей, строку ошибкой не считаем.
      if(accrued===null){kind=src.rate===null?'warn':'none';}
      else if(paid>0&&accrued===0){kind='err'; note='Входа нет — выдавать не нужно';}
      else if(paid>0&&paid!==accrued){kind='warn';
        note=(paid>accrued?'+':'−')+Math.abs(paid-accrued).toLocaleString('ru-RU')+' к ставке';}
      else if(debt>0)kind='todo';
      else if(accrued===0)kind='none';
      row.kind=kind; row.note=note; return row;
    };
    const ownRows=confirmed
      ?own.map(a=>{const e=byId.get(a.employee_id)||{};
        return build({key:'a'+a.id,accrualId:a.id,employeeId:a.employee_id,name:a.name,
          role:e.role||a.group||'',status:a.status,entry:e.first_entry||null,rate:a.rate,
          accrued:a.amount,paid:a.paid,debt:a.debt,day:S,own:true});})
      :staffRows.map(e=>build({key:'e'+e.employee_id,accrualId:null,employeeId:e.employee_id,
          name:e.name,role:e.role,status:e.status,entry:e.first_entry,rate:e.rate,
          accrued:e.payable,paid:0,debt:e.payable===null?0:e.payable,day:S,own:true}));
    const other=all.filter(a=>a.work_day!==S&&(num(a.debt)>0||pays.has(a.name+'|'+a.work_day)))
      .sort((a,b)=>a.work_day.localeCompare(b.work_day)||a.name.localeCompare(b.name,'ru'))
      .map(a=>{const e=byId.get(a.employee_id)||{};
        return build({key:'a'+a.id,accrualId:a.id,employeeId:a.employee_id,name:a.name,
          role:e.role||a.group||'',status:a.status,entry:null,rate:a.rate,
          accrued:a.amount,paid:a.paid,debt:a.debt,day:a.work_day,own:false});});
    const rows=other.concat(ownRows);
    const toPay=rows.filter(r=>r.debt>0&&r.accrued>0);
    const payable=ownRows.filter(r=>r.accrued>0);
    return {S,confirmed,rows,own:ownRows,other,toPay,
      totals:{accrued:ownRows.reduce((s,r)=>s+(r.accrued||0),0),
        paid:ownRows.reduce((s,r)=>s+r.paid,0),
        paidCount:ownRows.filter(r=>r.paid>0).length,payableCount:payable.length,
        toPaySum:toPay.reduce((s,r)=>s+r.debt,0),
        unconfirmedDebt:confirmed?0:ownRows.reduce((s,r)=>s+(r.accrued||0),0)}};
  }

  const BOARD_TABS=[['all','Все'],['late','Опоздали'],['todo','Не выдано'],['err','Ошибки'],['nohik','Без Hikvision']];
  function boardMatch(row,tab){
    return tab==='all'||(tab==='late'&&row.status==='late')||(tab==='todo'&&row.kind==='todo')
      ||(tab==='err'&&(row.kind==='err'||row.kind==='warn'))||(tab==='nohik'&&row.noHik);
  }
  function boardTabs(rows){ return BOARD_TABS.map(([k,l])=>[k,l,rows.filter(r=>boardMatch(r,k)).length]); }

  /* Почему смену нельзя подтвердить. Сервер откажет и сам, но причину
     показываем заранее — вместо полей, которые молча не работают. */
  function shiftBlocker(staff,board){
    if(board.confirmed||!staff)return null;
    const p=staff.payroll||{};
    if(!(staff.employees||[]).length)return 'empty';
    if(p.unavailable_count>0)return 'hikvision';
    if(staff.missing_rates>0||(staff.employees||[]).some(e=>e.payable===null))return 'rates';
    return null;
  }

  /* Журнал: каждое списание строкой. Зарплата и выдачи Шоху собираются в
     одну «Авто»-строку, долг, заведённый сегодня, — одной строкой с суммой,
     оплатой и остатком, а не тремя движениями. */
  const GROUP_SHORT={income:'Приходы',salary:'Зарплата',administrative:'Административные',
    operations:'Операционные',marketing:'Маркетинг',utilities:'Коммунальные / охрана',
    procurement:'Закуп',distributions:'Дивиденды / переводы',reserves:'Резервы'};
  function catalogIndex(groups){
    const index={};
    (groups||[]).forEach(g=>g.items.forEach(i=>{index[i.code]={group:g.code,label:i.label,
      short:i.code==='reserve_dividends_transfer'?GROUP_SHORT.distributions:(GROUP_SHORT[g.code]||g.label)};}));
    return index;
  }
  function stripItem(description,label){
    if(label&&description.startsWith(label+' · '))return description.slice(label.length+3);
    return description;
  }
  function journal(data,index){
    const l=data.ledger, mv=l.movements||[], rows=[], hidden=new Set();
    const created=l.debts_created_today||[];
    created.forEach(d=>{
      const own=mv.filter(m=>m.type==='other_expense'&&m.item_code===d.item_code
        &&(m.description===d.description||m.description===d.description+' · погашение долга'));
      own.forEach(m=>hidden.add(m.id));
      const info=index[d.item_code]||{};
      rows.push({kind:'debt',cat:info.short||'Расход',name:stripItem(d.description,info.label),
        title:d.description,amount:num(d.total),paid:num(d.paid),debt:num(d.debt),debtId:d.id,
        ops:own.map(m=>({operation:'movement',id:m.id}))});
    });
    const bySalaryDay=new Map();
    mv.filter(m=>m.type==='salary_payment').forEach(m=>{
      const hit=/^.* · (.+) · за (\d{4}-\d{2}-\d{2})$/.exec(m.description||'');
      const day=hit?hit[2]:'', list=bySalaryDay.get(day)||[];
      list.push({id:m.id,name:hit?hit[1]:m.description,amount:num(m.amount)}); bySalaryDay.set(day,list);
    });
    const auto=[];
    [...bySalaryDay.entries()].sort((a,b)=>b[0].localeCompare(a[0])).forEach(([day,list])=>{
      const sum=list.reduce((s,p)=>s+p.amount,0);
      auto.push({kind:'auto',group:'salary:'+day,cat:'Зарплата',
        name:'Сменные за '+dm(day)+' · '+list.length+' чел.',amount:sum,paid:sum,debt:0,children:list});
    });
    const monthly=mv.filter(m=>m.type==='other_expense'&&m.item_code==='salary_monthly');
    if(monthly.length){const sum=monthly.reduce((s,m)=>s+num(m.amount),0);
      auto.push({kind:'auto',group:'monthly',cat:'Зарплата',name:'Оклады · частичные выплаты',
        amount:sum,paid:sum,debt:0});}
    const kassa=(data.cashier_shokh_gives&&data.cashier_shokh_gives.gives)||[];
    if(kassa.length){const sum=kassa.reduce((s,g)=>s+num(g.amount),0);
      auto.push({kind:'auto',group:'kassa',cat:'Закуп',name:'Шоху от кассира · уже вычтено из передачи',
        amount:sum,paid:sum,debt:0,info:true});}
    const gives=mv.filter(m=>m.type==='procurement_advance');
    if(gives.length){const sum=gives.reduce((s,m)=>s+num(m.amount),0);
      auto.push({kind:'auto',group:'shoh',cat:'Закуп',name:'Выдано Шоху на закуп · подотчёт',
        amount:sum,paid:sum,debt:0});}
    mv.forEach(m=>{
      if(hidden.has(m.id))return;
      if(m.type==='other_expense'&&m.item_code!=='salary_monthly'){
        const info=index[m.item_code]||{};
        rows.push({kind:'expense',cat:info.short||'Расход',name:stripItem(m.description,info.label),
          title:m.description,amount:num(m.amount),paid:num(m.amount),debt:0,
          ops:[{operation:'movement',id:m.id}]});
      } else if(m.type==='other_receipt'){
        const info=index[m.item_code]||{};
        rows.push({kind:'income',cat:'Приходы',name:stripItem(m.description,info.label),
          title:m.description,amount:num(m.amount),paid:null,debt:0,
          ops:m.id===null?[]:[{operation:'movement',id:m.id}]});
      } else if(m.type==='reserve_transfer'){
        rows.push({kind:'expense',cat:GROUP_SHORT.distributions,name:m.description||'Отложено в сейф',
          title:'Отложено в сейф',amount:num(m.amount),paid:num(m.amount),debt:0,
          ops:m.id===null?[]:[{operation:'reserve_transfer',id:m.id}]});
      }
    });
    // Резервы вне кассы: доллары и выдача из сейфа — видны, но не «списаны».
    const res=data.reserves||{};
    [['usd','USD'],['dividends','сум']].forEach(([account,unit])=>{
      ((res[account]&&res[account].entries)||[]).filter(e=>e.id!==null&&(e.kind==='deposit'||e.kind==='withdrawal'))
        .forEach(e=>rows.push({kind:'reserve',cat:'Резервы',
          name:account==='usd'?(e.kind==='deposit'?'Поступили USD':'Выданы USD'):'Выдано из сейфа',note:e.note||'',
          amount:num(e.amount),unit,paid:null,debt:0,ops:[]}));
    });
    // Долги прошлых дней: оплатить можно отсюда же, щелчком по сумме долга.
    const carried=(l.manual_debts||[]).filter(d=>d.day<data.date).map(d=>{
      const info=index[d.item_code]||{};
      return {kind:'carried',cat:info.short||'Расход',name:stripItem(d.description,info.label),
        since:d.day,title:d.description,amount:num(d.total),paid:num(d.paid),debt:num(d.debt),debtId:d.id,ops:[]};
    });
    return {rows:auto.concat(rows,carried),
      total:num(l.cash_flow&&l.cash_flow.salary_paid)+num(l.cash_flow&&l.cash_flow.other_outflows)};
  }

  /* Деньги на расходы: строки тёмной карточки. Оклады и выдачи Шоху выделены
     из «прочих», как в макете; сумма строк сходится с остатком сервера. */
  function cashCard(data){
    const l=data.ledger, cf=l.cash_flow||{}, mv=l.movements||[];
    const sumOf=f=>mv.filter(f).reduce((s,m)=>s+num(m.amount),0);
    const monthly=sumOf(m=>m.type==='other_expense'&&m.item_code==='salary_monthly');
    const shoh=sumOf(m=>m.type==='procurement_advance');
    const other=num(cf.other_outflows)-monthly-shoh;
    return {end:l.cash_balance===null?null:num(l.cash_balance),
      opening:cf.opening_balance===null||cf.opening_balance===undefined?null:num(cf.opening_balance),
      cashier:data.expected_cashier===null?null:num(data.expected_cashier),
      // Передача кассы: получено (время нажатия кассира) или ожидается (подсказка).
      handedAt:data.cashier_handover&&data.cashier_handover.handed_at?String(data.cashier_handover.handed_at).slice(11,16):null,
      expected:data.cashier_handover&&data.cashier_handover.amount===null&&data.cashier_handover.expected!==null
        &&data.cashier_handover.expected!==undefined?num(data.cashier_handover.expected):null,
      receipts:num(cf.other_receipts),shift:num(cf.salary_paid),monthly,shoh,other};
  }

  function monthlyBoard(data){
    const mp=data.monthly_payments||{paid_by_employee:{},today:[]};
    const staff=data.monthly_employees||[];
    const byId=new Map(staff.map(e=>[e.id,e]));
    const paidTo=id=>num(mp.paid_by_employee[String(id)]);
    const people=staff.map(e=>({id:e.id,name:e.name,role:e.role,salary:num(e.salary),paid:paidTo(e.id),
      left:num(e.salary)-paidTo(e.id)}));
    const today=(mp.today||[]).map(p=>{const e=byId.get(p.employee_id);
      const salary=e?num(e.salary):0, left=salary-paidTo(p.employee_id);
      return {id:p.id,employeeId:p.employee_id,name:p.name,role:e?e.role:'',amount:num(p.amount),left};});
    return {people,today,remain:people.reduce((s,p)=>s+Math.max(0,p.left),0),
      todaySum:today.reduce((s,p)=>s+p.amount,0),
      overpaid:people.filter(p=>p.paid>p.salary)};
  }

  function shohBoard(shoh,purchases,movements,transfers,cashierGives){
    const pos=shohPosition(shoh), list=purchases||[];
    // Перечисления поставщику: безнал, не меняют ни подотчёт Шоха, ни кассу.
    const trs=(transfers||[]).map(t=>({id:t.id,supplier:t.supplier||'',item:t.item||t.purpose||t.note||'',
      point:t.point||'',amount:num(t.amount)}));
    const spent=list.reduce((s,p)=>s+num(p.total),0);
    const flags=p=>{
      if(p.accepted_at!==null&&p.accepted_at!==undefined)return [];
      const f=[];
      if(p.price_above_usual)f.push({t:'Цена'+(p.price_delta_percent?' +'+Math.round(num(p.price_delta_percent))+'%':' выше'),
        text:'Цена выше обычной: '+p.item,
        sub:num(p.price).toLocaleString('ru-RU')+' за '+p.unit+(p.usual_price!==null?' при обычной '+num(p.usual_price).toLocaleString('ru-RU'):'')});
      if(!p.has_photo)f.push({t:'Нет фото',text:'Покупка без фото: '+p.item,
        sub:'Шох · '+(p.created_at||'').slice(11,16)+' · '+num(p.total).toLocaleString('ru-RU')+' сум'});
      if(p.iiko&&!['synced','legacy'].includes(p.iiko.status))f.push({t:'iiko не проведено',text:'Накладная не проведена в iiko: '+p.item,sub:''});
      return f;
    };
    const gives=(movements||[]).filter(m=>m.type==='procurement_advance').map(m=>({id:m.id,amount:num(m.amount),note:m.description,
      time:m.created_at?String(m.created_at).slice(11,16):''}));
    // Выдачи Шоху из кассы: уже вошли в подотчёт и уже вычтены из передачи
    // кассы, поэтому из денег бухгалтера второй раз не списываются.
    ((cashierGives&&cashierGives.gives)||[]).forEach(g=>gives.push({id:g.id,amount:num(g.amount),fromKassa:true,
      time:g.created_at?String(g.created_at).slice(11,16):''}));
    gives.sort((a,b)=>(a.time||'99').localeCompare(b.time||'99'));
    return {known:pos.known,start:pos.start,given:pos.given,spent,count:list.length,
      hand:pos.known?pos.start+pos.given-spent:null,gives,trs,trSum:trs.reduce((s,t)=>s+t.amount,0),
      buys:list.map(p=>({...p,flags:flags(p)}))};
  }

  const LEVEL_ORDER={err:0,warn:1,todo:2};
  function financeIssues({data,board,blocker,monthly,shoh,cash}){
    const out=[], add=(lvl,text,sub,target)=>out.push({lvl,text,sub:sub||'',target:target||null});
    const fmt=v=>Number(v).toLocaleString('ru-RU');
    board.own.forEach(r=>{
      if(r.kind==='err')add('err','Выдано без входа: '+r.name,'Смена '+dm(r.day)+' · '+fmt(r.paid)+' сум','row:'+r.key);
      else if(r.kind==='warn'&&r.accrued!==null)add('warn',(r.paid>r.accrued?'Больше ставки: ':'Меньше ставки: ')+r.name,
        'Выдано '+fmt(r.paid)+' при ставке '+fmt(r.accrued),'row:'+r.key);
      else if(r.kind==='warn'&&r.rate===null)add('warn','Нет ставки: '+r.name,'Смена '+dm(r.day)+' не начисляется','row:'+r.key);
    });
    board.other.forEach(r=>{
      if(r.debt>0)add('warn','Смена '+dm(r.day)+' не выдана: '+r.name,'Долг '+fmt(r.debt)+' сум','row:'+r.key);
    });
    monthly.overpaid.forEach(p=>add('err','Переплата оклада: '+p.name,'Выдано '+fmt(p.paid)+' из '+fmt(p.salary),'month:'+p.id));
    shoh.buys.forEach(b=>b.flags.forEach(f=>add('warn',f.text,f.sub,'buy:'+b.id)));
    if(shoh.hand!==null&&shoh.hand<0)add('err','Шох потратил больше, чем получил','Баланс '+fmt(shoh.hand)+' сум','shoh');
    if(cash.end!==null&&cash.end<0)add('err','Остаток ушёл в минус',fmt(cash.end)+' сум на конец дня','cash');
    const dv=data.dividends_week;
    if(dv&&dv.behind)add('warn','Отстаём от недельных дивидендов',
      'Отложено '+fmt(num(dv.collected))+' из '+fmt(num(dv.target))+' · по плану к этому дню '+fmt(Math.round(num(dv.pace))),'dividends');
    if(blocker==='hikvision')add('warn','Смену '+dm(board.S)+' нельзя подтвердить','Данные Hikvision за этот день неполные','shift');
    if(blocker==='rates')add('warn','Смену '+dm(board.S)+' нельзя подтвердить','Не у всех сотрудников указана ставка','shift');
    const todo=board.own.filter(r=>r.kind==='todo');
    if(todo.length)add('todo',todo.length+' '+plural(todo.length,'сменный ждёт','сменных ждут','сменных ждут')+' выплату за '+dm(board.S),
      fmt(todo.reduce((s,r)=>s+r.debt,0))+' сум','todo');
    if(data.expected_cashier===null)add('todo','Кассир ещё не передал кассу','Касса за '+dm(data.date)+' не записана','cash');
    return out.sort((a,b)=>LEVEL_ORDER[a.lvl]-LEVEL_ORDER[b.lvl]);
  }

  return {shohPosition,shiftRows,shiftTabs,matchesTab,dayChecks,payLock,
    shiftIso,plural,entryClock,lateMinutes,todaySalaryPayments,shiftBoard,boardTabs,boardMatch,
    shiftBlocker,catalogIndex,journal,cashCard,monthlyBoard,shohBoard,financeIssues,GROUP_SHORT};
});
