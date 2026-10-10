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
      // Отставание — от плана к сегодняшнему дню (pace, Функционал §3.7), как в 2a.
      add('warn','Отстаём от недельных дивидендов',{amount:Math.round(Number(dividends.pace)-Number(dividends.collected))});
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

  function shiftBoard({payday,staff,accruals,movements,accountingStart}){
    const S=shiftIso(payday,-1);
    const staffRows=accountingStart&&S<accountingStart?[]:(staff&&staff.employees)||[];
    const byId=new Map(staffRows.map(r=>[r.employee_id,r]));
    const all=(accruals||[]).filter(a=>!accountingStart||a.work_day>=accountingStart);
    const own=all.filter(a=>a.work_day===S);
    const pays=todaySalaryPayments(movements);
    const build=(src)=>{
      const today=pays.get(src.name+'|'+src.day)||[];
      const paidToday=today.reduce((s,p)=>s+p.amount,0);
      const accrued=src.accrued===null||src.accrued===undefined?null:num(src.accrued);
      const paid=num(src.paid), debt=src.debt===null?(accrued||0):num(src.debt);
      const row={...src,accrued,paid,paidToday,paidBefore:Math.max(0,paid-paidToday),debt,
        todayPayments:today,noHik:src.noHik||src.status==='unlinked'||MANUAL.has(src.status),
        manualAttendance:MANUAL.has(src.status),
        time:src.own?entryClock(src.entry):null,late:src.status==='late'?lateMinutes(src.entry):0};
      let kind='ok', note='';
      // Начисление не посчитать — строка заблокирована своей причиной, а
      // остальным выдавать можно: подтверждение смены частичное.
      // Причину отдаёт сервер (`blocker`); без неё выводим по ставке и статусу.
      const BLOCK={missing_rate:'rate',unlinked:'unlinked',unavailable:'hikvision',unknown:'unknown'};
      row.block=src.blocker?(BLOCK[src.blocker]||'unknown')
        :accrued===null?(src.rate===null?'rate':src.status==='unlinked'?'unlinked'
        :src.status==='unavailable'?'hikvision':'unknown'):null;
      if(row.block)kind='blocked';
      else if(paid>0&&accrued===0){kind='err'; note='Входа нет — выдавать не нужно';}
      else if(paid>0&&paid!==accrued){kind='warn';
        note=(paid>accrued?'+':'−')+Math.abs(paid-accrued).toLocaleString('ru-RU')+' к ставке';}
      else if(debt>0)kind='todo';
      else if(accrued===0)kind='none';
      row.kind=kind; row.note=note; return row;
    };
    // Смена может быть подтверждена частично: у кого начисление уже есть —
    // строка из начисления, у остальных — из реестра дня.
    const ownAcc=new Map(own.map(a=>[a.employee_id,a]));
    const fromAcc=a=>{const e=byId.get(a.employee_id)||{};
      return build({key:'a'+a.id,accrualId:a.id,employeeId:a.employee_id,name:a.name,
        role:e.role||a.group||'',status:a.status,entry:e.first_entry||null,rate:a.rate,noHik:e.hikvision_registered===false,
        accrued:a.amount,paid:a.paid,debt:a.debt,day:S,own:true});};
    const ownRows=staffRows.map(e=>ownAcc.has(e.employee_id)?fromAcc(ownAcc.get(e.employee_id))
      :build({key:'e'+e.employee_id,accrualId:null,employeeId:e.employee_id,
        name:e.name,role:e.role,status:e.status,entry:e.first_entry,rate:e.rate,blocker:e.blocker||null,noHik:e.hikvision_registered===false,
        accrued:e.payable,paid:0,debt:e.payable===null?0:e.payable,day:S,own:true}))
      .concat(own.filter(a=>!byId.has(a.employee_id)).map(fromAcc));
    const confirmed=ownRows.length>0&&ownRows.every(r=>r.accrualId);
    const partial=!confirmed&&ownRows.some(r=>r.accrualId);
    const other=all.filter(a=>a.work_day!==S&&(num(a.debt)>0||pays.has(a.name+'|'+a.work_day)))
      .sort((a,b)=>a.work_day.localeCompare(b.work_day)||a.name.localeCompare(b.name,'ru'))
      .map(a=>{const e=byId.get(a.employee_id)||{};
        return build({key:'a'+a.id,accrualId:a.id,employeeId:a.employee_id,name:a.name,
          role:e.role||a.group||'',status:a.status,entry:null,rate:a.rate,noHik:e.hikvision_registered===false,
          accrued:a.amount,paid:a.paid,debt:a.debt,day:a.work_day,own:false});});
    const rows=other.concat(ownRows);
    const toPay=rows.filter(r=>r.debt>0&&r.accrued>0);
    // «Выдать пришедшим» (Функционал 2a): ставку всем пришедшим за вчера, у
    // кого ещё нет выплаты. Частично выданных и долги прошлых смен не трогаем —
    // их выдают строкой, иначе кнопка молча доплатила бы то, что решили иначе.
    const handOut=ownRows.filter(r=>r.accrued>0&&r.paid===0&&!r.block);
    const payable=ownRows.filter(r=>r.accrued>0);
    const blocked=ownRows.filter(r=>r.block);
    return {S,confirmed,partial,rows,own:ownRows,other,toPay,handOut,blocked,
      totals:{accrued:ownRows.reduce((s,r)=>s+(r.accrued||0),0),
        paid:ownRows.reduce((s,r)=>s+r.paid,0),
        paidCount:ownRows.filter(r=>r.paid>0).length,payableCount:payable.length,
        toPaySum:toPay.reduce((s,r)=>s+r.debt,0),
        unconfirmedDebt:ownRows.filter(r=>!r.accrualId).reduce((s,r)=>s+(r.accrued||0),0)}};
  }

  /* «Ошибки» — только настоящие ошибки выдачи (выдано без входа, сумма мимо
     ставки): те же, что красные и жёлтые пункты «Проверок». Строки, которые
     нельзя начислить (нет ставки, нет привязки Hikvision, нет данных), — своя
     вкладка «Не начислено»: это не ошибка, а причина, по которой ждут. */
  const BOARD_TABS=[['all','Все'],['late','Опоздали'],['todo','Не выдано'],['err','Ошибки'],['blocked','Не начислено'],['nohik','Без Hikvision']];
  function boardMatch(row,tab){
    return tab==='all'||(tab==='late'&&row.status==='late')||(tab==='todo'&&row.kind==='todo')
      ||(tab==='err'&&(row.kind==='err'||row.kind==='warn'))||(tab==='blocked'&&row.kind==='blocked')
      ||(tab==='nohik'&&row.noHik);
  }
  function boardTabs(rows){ return BOARD_TABS.map(([k,l])=>[k,l,rows.filter(r=>boardMatch(r,k)).length]); }

  /* Почему смену нельзя подтвердить. Сервер откажет и сам, но причину
     показываем заранее — вместо полей, которые молча не работают. */
  /* Кого из смены нельзя начислить и почему — по строкам, а не на всю
     смену: подтверждение частичное, остальным выдают сразу. */
  function shiftBlocker(staff,board){
    const list=(board&&board.blocked)||[];
    if(!list.length)return null;
    const count=kind=>list.filter(r=>r.block===kind).length;
    return {total:list.length,rate:count('rate'),unlinked:count('unlinked'),hikvision:count('hikvision'),unknown:count('unknown')};
  }

  /* Журнал: каждое списание строкой. Зарплата и выдачи Шоху собираются в
     одну «Авто»-строку, долг, заведённый сегодня, — одной строкой с суммой,
     оплатой и остатком, а не тремя движениями. */
  const GROUP_SHORT={income:'Приходы',salary:'Зарплата',administrative:'Административные',
    operations:'Операционные',marketing:'Маркетинг',utilities:'Коммунальные / охрана',
    procurement:'Закуп',distributions:'Дивиденды / переводы',reserves:'Резервы',dividends:'Дивиденды'};
  /* Дивиденды (ТЗ 09.10, Б-08) — три разные операции, и в списке категорий
     они названы по тому, откуда уходят деньги: из кассы в сейф, из сейфа
     учредителю (касса не меняется) и учредителю прямо из кассы. В журнале у
     всех трёх категория «Дивиденды», а не «Административные» или «Резервы». */
  const DIVIDEND_OPTIONS=[['reserve_dividends_transfer','Отложить дивиденды в сейф'],
    ['reserve_dividends_withdrawal','Выдать дивиденды из сейфа'],['distribution_dividends','Выдать дивиденды из кассы']];
  const DIVIDEND_CODES=new Set(DIVIDEND_OPTIONS.map(([code])=>code));
  function catalogIndex(groups){
    const index={};
    (groups||[]).forEach(g=>g.items.forEach(i=>{index[i.code]={group:g.code,label:i.label,
      short:DIVIDEND_CODES.has(i.code)?GROUP_SHORT.dividends:(GROUP_SHORT[g.code]||g.label)};}));
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
        // × удаляет ошибочную запись целиком: долг вместе с оплатой этого дня.
        ops:[{operation:'debt',id:d.id}]});
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
      // Раскрывается, как «Сменные»: кому и сколько выдано сегодня.
      const people=((data.monthly_payments||{}).today||[]).map(p=>({id:'m'+p.id,name:p.name,amount:num(p.amount),readonly:true}));
      auto.push({kind:'auto',group:'monthly',cat:'Зарплата',name:'Оклады · частичные выплаты'+(people.length?' · '+people.length+' чел.':''),
        amount:sum,paid:sum,debt:0,children:people.length?people:undefined});}
    // Доп. выплаты из «Зарплата · день» (Б-05) — так же: одна строка, раскрывается по людям.
    const extras=mv.filter(m=>m.type==='other_expense'&&m.item_code==='salary_extra_payout');
    if(extras.length){const sum=extras.reduce((s,m)=>s+num(m.amount),0);
      const people=(data.extra_payouts||[]).map(p=>({id:'x'+p.id,amount:num(p.amount),readonly:true,
        name:[p.name,p.temporary?'временный':'','смена '+dm(p.work_day),p.note].filter(Boolean).join(' · ')}));
      auto.push({kind:'auto',group:'extra',cat:'Зарплата',name:'Доп. выплаты'+(people.length?' · '+people.length+' чел.':''),
        amount:sum,paid:sum,debt:0,children:people.length?people:undefined});}
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
      if(m.type==='other_expense'&&m.item_code==='distribution_dividends'){
        // Дивиденды из кассы: «Выдано из кассы», рядом — что ввёл человек.
        const info=index[m.item_code]||{}, note=stripItem(m.description,info.label);
        rows.push({kind:'expense',cat:GROUP_SHORT.dividends,name:'Выдано из кассы',note:note===info.label?'':note,
          title:m.description,amount:num(m.amount),paid:num(m.amount),debt:0,
          ops:[{operation:'movement',id:m.id}]});
      } else if(m.type==='other_expense'&&m.item_code!=='salary_monthly'&&m.item_code!=='salary_extra_payout'){
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
        // Из кассы в сейф: деньги ушли из остатка, учредителю ещё не выданы.
        rows.push({kind:'expense',cat:GROUP_SHORT.dividends,name:'Отложено в сейф',note:m.description||'',
          title:'Отложено в сейф',amount:num(m.amount),paid:num(m.amount),debt:0,
          ops:m.id===null?[]:[{operation:'reserve_transfer',id:m.id}]});
      }
    });
    // Резервы вне кассы: доллары и выдача из сейфа — видны, но не «списаны».
    const res=data.reserves||{};
    [['usd','USD'],['dividends','сум']].forEach(([account,unit])=>{
      ((res[account]&&res[account].entries)||[]).filter(e=>e.id!==null&&(e.kind==='deposit'||e.kind==='withdrawal'))
        .forEach(e=>rows.push({kind:'reserve',cat:account==='usd'?'Резервы':GROUP_SHORT.dividends,
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
    // Расход «Закуп · Шох» из журнала — тоже выдача в подотчёт (сервер кладёт
    // его в баланс Шоха), поэтому он в строке «Шоху», а не в «прочих».
    const shoh=sumOf(m=>m.type==='procurement_advance'||(m.type==='other_expense'&&m.item_code==='proc_shoh'));
    // Доп. зарплата и доп. выплаты сменным — к сменным, а не к прочим (как day_flow на сервере).
    const extra=sumOf(m=>m.type==='other_expense'&&(m.item_code==='salary_extra'||m.item_code==='salary_extra_payout'));
    const other=num(cf.other_outflows)-monthly-shoh-extra;
    return {end:l.cash_balance===null?null:num(l.cash_balance),
      opening:cf.opening_balance===null||cf.opening_balance===undefined?null:num(cf.opening_balance),
      cashier:data.expected_cashier===null?null:num(data.expected_cashier),
      // Передача кассы: получено (время нажатия кассира) или ожидается (подсказка).
      handedAt:data.cashier_handover&&data.cashier_handover.handed_at?String(data.cashier_handover.handed_at).slice(11,16):null,
      expected:data.cashier_handover&&data.cashier_handover.amount===null&&data.cashier_handover.expected!==null
        &&data.cashier_handover.expected!==undefined?num(data.cashier_handover.expected):null,
      // Подтверждение бухгалтера: сколько реально получено и недостача к расчёту.
      // Недостачу считает сервер (ledger.handover_state) от ТЕКУЩЕГО расчёта
      // кассы — то же число в тосте, карточке, «Проверках», у учредителя и в Excel.
      // Ручная запись прихода сверяется так же (checked), без «Изменить».
      confirmedAt:data.cashier_handover&&data.cashier_handover.confirmed_at?String(data.cashier_handover.confirmed_at).slice(11,16):null,
      // Когда бухгалтер нажал «Подтвердить» или записал приход сам (время
      // Ташкента): это время записи, а не доказанное время передачи денег.
      confirmedStamp:data.cashier_handover&&data.cashier_handover.confirmed_at?String(data.cashier_handover.confirmed_at):null,
      recordedStamp:data.cashier_handover&&data.cashier_handover.source==='accountant'&&data.cashier_handover.handed_at
        ?String(data.cashier_handover.handed_at):null,
      checked:!!(data.cashier_handover&&data.cashier_handover.checked),
      // Ручной приход при кассире, который в панели не работал, не сверяется.
      unchecked:!!(data.cashier_handover&&data.cashier_handover.source==='accountant'
        &&!data.cashier_handover.confirmed_at&&data.cashier_handover.cashier_active===false),
      calculation:data.cashier_handover&&data.cashier_handover.calculation!=null?num(data.cashier_handover.calculation):null,
      shortfall:data.cashier_handover&&data.cashier_handover.shortfall!=null?num(data.cashier_handover.shortfall):0,
      // Кассир изменил день после подтверждения: расчёт тогда и сейчас.
      changed:!!(data.cashier_handover&&data.cashier_handover.expected_changed),
      confirmedCalc:data.cashier_handover&&data.cashier_handover.expected_amount!=null?num(data.cashier_handover.expected_amount):null,
      receipts:num(cf.other_receipts),shift:num(cf.salary_paid)+extra,monthly,shoh,other};
  }

  /* Строки дэшборда под итогом кассы (ТЗ 09.10, Б-09). Итог сходится со
     строками: остаток на начало + подтверждённые поступления − фактические
     списания. Касса, которую ещё не подтвердили, видна (expected), но в
     итог не входит. Выдачи Шоху из кассы кассира уже вычтены из передачи —
     их здесь нет, второй раз они не вычитаются. Дивиденды прямо из кассы
     и отложенные в сейф — своими строками, не внутри «Прочих расходов». */
  function cashLines(data,cash,opts){
    const o=opts||{}, l=data.ledger||{}, flow=l.day_flow||{}, cf=l.cash_flow||{};
    const state=cf.handover_status||'none', counted=state==='confirmed'||state==='accountant';
    // «06.10 в 15:07» не разрывается: на узкой колонке переносится целиком.
    const when=iso=>iso?' '+dm(iso.slice(0,10))+'\u00a0в\u00a0'+iso.slice(11,16):'';
    const shown=cash.cashier!==null?cash.cashier:cash.expected;
    // Исторический день подписан своей датой: «Остаток на начало 06.10».
    const of=data.date===o.today?'дня':dm(data.date);
    const lines=[{key:'opening',label:'Остаток на начало '+of,value:cash.opening,sign:1}];
    lines.push({key:'handover',label:'+ Касса за '+dm(data.cashier_date||shiftIso(data.date,-1)),
      value:counted?num(flow.handover_counted):shown,sign:1,expected:!counted,
      sub:state==='confirmed'?'Подтверждено бухгалтером'+when(cash.confirmedStamp)
        :state==='accountant'?'Записано бухгалтером'+when(cash.recordedStamp)
        :shown!==null?'ожидается · не в остатке':'ожидается'});
    if(num(flow.receipts))lines.push({key:'receipts',label:'+ Прочие поступления',value:num(flow.receipts),sign:1});
    const dividends=num(flow.other_dividends);
    lines.push({key:'salary',label:'− Зарплаты · сменные и оклады',value:num(flow.salary)+num(flow.monthly),sign:-1},
      {key:'shoh',label:'− Выдано Шоху',value:num(flow.shoh),sign:-1});
    if(dividends)lines.push({key:'dividends',label:'− Дивиденды · из кассы',value:dividends,sign:-1});
    if(num(flow.transfers))lines.push({key:'safe',label:'− Дивиденды · в сейф',value:num(flow.transfers),sign:-1});
    lines.push({key:'other',label:'− Прочие расходы',value:num(flow.other)-dividends,sign:-1});
    // Итог формулы — та же касса, что крупной цифрой сверху.
    lines.push({key:'closing',label:'= Остаток на конец '+of,value:cash.end,sign:0,total:true});
    return lines;
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
    // Долг по окладам — за прошлый месяц: текущий месяц ещё не отработан.
    const pm=mp.previous||null;
    const previous=pm&&{month:pm.month,available:!!pm.available,
      remain:pm.available?staff.reduce((s,e)=>s+Math.max(0,num(e.salary)-num((pm.paid_by_employee||{})[String(e.id)])),0):null};
    return {people,today,remain:people.reduce((s,p)=>s+Math.max(0,p.left),0),
      todaySum:today.reduce((s,p)=>s+p.amount,0),
      overpaid:people.filter(p=>p.paid>p.salary),previous};
  }

  function shohBoard(shoh,purchases,movements,transfers,cashierGives,pocket){
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
      // T-399: товар не из справочника iiko — накладной нет, её проводит бухгалтер.
      if(p.iiko&&p.iiko.status==='manual')f.push({t:'Нет в iiko — заведите товар и проведите накладную вручную',
        text:'Нет в iiko — заведите товар и проведите накладную вручную: '+p.item,
        sub:[p.point,p.supplier,p.storage].filter(Boolean).join(' · ')});
      else if(p.iiko&&!['synced','legacy'].includes(p.iiko.status))f.push({t:'iiko не проведено',text:'Накладная не проведена в iiko: '+p.item,sub:''});
      return f;
    };
    const gives=(movements||[]).filter(m=>m.type==='procurement_advance').map(m=>({id:m.id,amount:num(m.amount),note:m.description,
      time:m.created_at?String(m.created_at).slice(11,16):''}));
    // Строка журнала «Закуп · Шох» тоже пополняет подотчёт — видна среди выдач.
    (movements||[]).filter(m=>m.type==='other_expense'&&m.item_code==='proc_shoh').forEach(m=>gives.push({id:m.id,amount:num(m.amount),
      note:m.description,fromJournal:true,time:m.created_at?String(m.created_at).slice(11,16):''}));
    // Выдачи Шоху из кассы: уже вошли в подотчёт и уже вычтены из передачи
    // кассы, поэтому из денег бухгалтера второй раз не списываются.
    ((cashierGives&&cashierGives.gives)||[]).forEach(g=>gives.push({id:g.id,amount:num(g.amount),fromKassa:true,
      time:g.created_at?String(g.created_at).slice(11,16):''}));
    gives.sort((a,b)=>(a.time||'99').localeCompare(b.time||'99'));
    // «На руках» считает сервер (shokh.store.pocket_position) — та же сумма,
    // что у Шоха в 3a и у учредителя. Без неё — по дню, как раньше.
    const server=pocket&&pocket.pocket!==undefined;
    const known=server?pocket.pocket!==null:pos.known;
    const start=server?(pocket.day_start===null?null:num(pocket.day_start)):pos.start;
    const given=server?num(pocket.given_today):pos.given;
    const hand=server?(pocket.pocket===null?null:num(pocket.pocket)):(pos.known?pos.start+pos.given-spent:null);
    return {known,start,given,spent:server?num(pocket.spent_day):spent,count:list.length,hand,
      reported:server?pocket.reported_percent:null,gives,trs,trSum:trs.reduce((s,t)=>s+t.amount,0),
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
    if(blocker&&blocker.rate)add('warn',blocker.rate+' '+plural(blocker.rate,'сотрудник','сотрудника','сотрудников')+' без ставки',
      'Смена '+dm(board.S)+' им не начисляется — укажите ставку','blocked');
    if(blocker&&blocker.unlinked)add('warn',blocker.unlinked+' '+plural(blocker.unlinked,'сотрудник','сотрудника','сотрудников')+' без привязки Hikvision',
      'Отметьте их вручную — тогда смену можно начислить','blocked');
    if(blocker&&blocker.hikvision)add('warn',blocker.hikvision+' '+plural(blocker.hikvision,'сотрудник','сотрудника','сотрудников')+' без данных Hikvision',
      'Входы за '+dm(board.S)+' ещё не пришли','blocked');
    const todo=board.own.filter(r=>r.kind==='todo');
    if(todo.length)add('todo',todo.length+' '+plural(todo.length,'сменный ждёт','сменных ждут','сменных ждут')+' выплату за '+dm(board.S),
      fmt(todo.reduce((s,r)=>s+r.debt,0))+' сум','todo');
    if(cash.shortfall>0)add('err','От кассира получено меньше расчёта',
      'Расчёт '+fmt(cash.calculation)+' · получено '+fmt(cash.cashier)+' · не хватает '+fmt(cash.shortfall)+' сум','cash');
    if(cash.changed)add('warn','Касса изменилась после подтверждения',
      'Было '+fmt(cash.confirmedCalc)+' · сейчас '+fmt(cash.calculation)+' сум — подтвердите снова','cash');
    if(data.expected_cashier===null)add('todo','Кассир ещё не передал кассу','Касса за '+dm(data.cashier_date||shiftIso(data.date,-1))+' не записана','cash');
    // ТЗ 02.10: сумма от кассира входит в остаток только подтверждённой.
    const handover=data.ledger&&data.ledger.cash_flow&&data.ledger.cash_flow.handover_status;
    if(handover==='pending')add('warn','Сумма от кассира не подтверждена',
      'В остаток не входит, отчёт дня не сдаётся — подтвердите в дэшборде','cash');
    return out.sort((a,b)=>LEVEL_ORDER[a.lvl]-LEVEL_ORDER[b.lvl]);
  }

  /* Бейдж «Проверок»: «N ошибок» — только красные (в Excel это «Ошибка»),
     одни жёлтые — «N замечаний» («Внимание» в Excel), ничего — «чисто».
     Пункты «ждёт» (todo) — дела, а не ошибки: в бейдж не входят. */
  function checksBadge(issues){
    const errors=(issues||[]).filter(i=>i.lvl==='err').length;
    const warns=(issues||[]).filter(i=>i.lvl==='warn').length;
    if(errors)return {text:errors+' '+plural(errors,'ошибка','ошибки','ошибок'),tone:'err',errors,warns};
    if(warns)return {text:warns+' '+plural(warns,'замечание','замечания','замечаний'),tone:'warn',errors,warns};
    return {text:'чисто',tone:'clean',errors,warns};
  }

  return {shohPosition,shiftRows,shiftTabs,matchesTab,dayChecks,payLock,
    shiftIso,plural,entryClock,lateMinutes,todaySalaryPayments,shiftBoard,boardTabs,boardMatch,
    shiftBlocker,catalogIndex,journal,cashCard,cashLines,monthlyBoard,shohBoard,financeIssues,checksBadge,
    GROUP_SHORT,DIVIDEND_OPTIONS};
});
