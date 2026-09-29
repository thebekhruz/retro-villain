/* Расчёты ведомости месяца без DOM. Сетка «сотрудник × день» — это только
   раскладка присланных начислений и выплат, поэтому её удобнее проверять
   тестом, чем глазами. */
(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.PayrollLogic=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){

  const n=value=>Number(value||0);
  const fmt=value=>new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(n(value));
  const dm=day=>day.slice(8,10)+'.'+day.slice(5,7);
  function addDays(day,step){
    const moved=new Date(day+'T12:00:00Z');
    moved.setUTCDate(moved.getUTCDate()+step);
    return moved.toISOString().slice(0,10);
  }
  const ABSENT=new Set(['missing','manual_absent']);
  const byId=(list,key)=>list.slice().sort((a,b)=>n(a[key])-n(b[key]));
  const PRESENT=new Set(['on_time','late','manual_present']);

  /* Состояние ячейки дня (старая классификация, на ней держится подвал).
     Пустая ячейка — смены не было: начисления за этот день у человека нет.
     «Не пришёл» — это ноль по делу, а не невыданный долг. */
  function cellState(cell){
    if(!cell)return {kind:'empty'};
    const amount=n(cell.amount), paid=n(cell.paid), debt=n(cell.debt);
    if(ABSENT.has(cell.status)&&amount===0)return {kind:'missing',amount,paid,debt};
    if(debt<=0)return {kind:'paid',amount,paid,debt};
    if(paid>0)return {kind:'partial',amount,paid,debt};
    return {kind:cell.status==='late'?'late':'owed',amount,paid,debt};
  }

  /* Почему начисление за день ждёт: код из /staff (blocker) → подпись ячейки. */
  const BLOCKER={missing_rate:'нет ставки',unlinked:'нет привязки',unavailable:'нет данных',unknown:'не начислено'};

  /* Ячейка сменного в сетке 2b. Колонка — день смены, выдают на следующий
     день, поэтому «к выдаче» — только вчерашняя смена, а всё, что старше и не
     выдано, — «✕ смена не выдана».
       pending — строка /staff за этот день, если начисления у человека ещё нет:
       день не закрыт (вчерашний или начислен частично). Смену начисляют по
       людям: кто без препятствий — «к выдаче», у кого blocker — ждёт. */
  function gridCell(cell,{rate,day,today,pending,waiting}={}){
    const yesterday=today?addDays(today,-1):null;
    const late=(cell&&cell.status==='late')||(!cell&&pending&&pending.status==='late');
    const base={day,late,text:'',payable:false,debt:0,accrualId:cell?cell.accrual_id:null,
      payments:cell&&Array.isArray(cell.payments)?cell.payments.map(p=>({id:p.id,day:p.day,amount:n(p.amount)})):[]};
    if(!cell){
      if(pending&&!pending.accrued){
        if(ABSENT.has(pending.status))return {...base,kind:'missing',text:'н/я'};
        if(pending.blocker)return {...base,kind:'blocked',text:BLOCKER[pending.blocker]||BLOCKER.unknown,blocker:pending.blocker};
        if(PRESENT.has(pending.status)&&n(pending.payable)>0)
          return {...base,kind:'pending',text:'к выдаче',debt:n(pending.payable)};
      }
      // День начислен частично, а данных по человеку нет — он ждёт начисления.
      if(waiting)return {...base,kind:'blocked',text:'ждёт',blocker:'unknown'};
      return {...base,kind:today&&day>=today?'future':'empty'};
    }
    const amount=n(cell.amount), paid=n(cell.paid), debt=n(cell.debt);
    // «Сумма ≠ ставке» — со ставкой, что действовала в день смены (она в
    // начислении), а не с сегодняшней ставкой человека.
    const r=n(cell.rate!=null&&cell.rate!==''?cell.rate:rate);
    // Выданное можно отменить нажатием (есть сами выплаты — их и удаляем).
    base.cancellable=paid>0&&base.payments.length>0;
    if(ABSENT.has(cell.status)){
      if(paid>0)return {...base,kind:'nopass',text:'!',paid,rate:r};
      return {...base,kind:'missing',text:'н/я'};
    }
    if(paid>0&&(paid!==r||debt>0))
      return {...base,kind:'odd',text:fmt(paid),paid,debt,payable:debt>0,rate:r};
    if(paid>0)return {...base,kind:'paid',text:'✓',paid,rate:r};
    if(debt>0){
      const fresh=yesterday&&day>=yesterday;
      return {...base,kind:fresh?'topay':'unpaid',text:fresh?'к выдаче':'✕',debt,payable:true};
    }
    if(amount===0)return {...base,kind:'empty'};
    return {...base,kind:'paid',text:'✓',paid,rate:r};
  }

  /* Строки сменных для сетки. pending — {день: {employee_id: строка /staff}}
     для открытых дней (вчерашний неподтверждённый и начисленные частично).
     Закрытые дни (confirmed_days) берутся только из начислений. */
  function shiftRows(data,{today,pending}={}){
    const days=data.days||[];
    const closed=new Set(data.confirmed_days||[]), partial=new Set(data.partial_days||[]);
    // Порядок — как в реестре (по номеру сотрудника), а не по алфавиту:
    // так строки стоят на тех же местах, что и в макете и в «Сотрудниках».
    // Кому за месяц ещё ничего не начислено (новые в реестре, все смены ждут),
    // в ведомости нет — добавляем по строкам /staff открытых дней.
    const people=[...(data.shift||[])], known=new Set(people.map(person=>person.employee_id));
    Object.values(pending||{}).forEach(rows=>Object.entries(rows||{}).forEach(([key,row])=>{
      const id=Number(row.employee_id??key);
      if(known.has(id))return;
      known.add(id);
      people.push({employee_id:id,name:row.name,group:row.group||'',rate:row.rate,accrued:0,paid:0,debt:0,cells:{}});
    }));
    return byId(people,'employee_id').map(person=>{
      const id=person.employee_id, cells=person.cells||{};
      let extra=0, manual=Object.values(cells).some(c=>String(c.status).startsWith('manual_'));
      const row=days.map(day=>{
        const cell=cells[day]||null;
        const open=!cell&&!closed.has(day);
        const wait=open&&pending&&pending[day]?pending[day][id]||null:null;
        if(wait&&String(wait.status).startsWith('manual_'))manual=true;
        const item=gridCell(cell,{rate:person.rate,day,today,pending:wait,waiting:open&&!wait&&partial.has(day)});
        if(item.kind==='pending')extra+=item.debt;
        return item;
      });
      const rest=n(person.debt)+extra;
      const noRate=person.rate==null||person.rate==='';
      return {id,name:person.name,group:person.group||'',rate:noRate?null:n(person.rate),
        paid:n(person.paid),accrued:n(person.accrued),rest,noHik:!!manual,cells:row};
    });
  }

  /* Строки помесячных: оклад выдают частями, ячейка — сумма выплат за день.
     ops — сами выплаты дня (id движения и сумма): по ним ячейку можно
     уменьшить или очистить. Если сервер их не прислал — null. */
  function monthlyRows(data,{today}={}){
    const days=data.days||[], cellsById=data.monthly_cells||{}, opsById=data.monthly_cell_ops;
    // Выплаты сотруднику, которого уже нет в реестре, не пропадают из месяца:
    // деньги ушли (так же и в XLSX). Его строка только для чтения, без оклада.
    // Имя удалённого (он в архиве) сервер присылает в monthly_archived.
    const known=new Set((data.monthly||[]).map(person=>String(person.id)));
    const archived=new Map((data.monthly_archived||[]).map(person=>[String(person.id),person]));
    const gone=Object.keys(cellsById).filter(key=>!known.has(key)&&Object.values(cellsById[key]||{}).some(v=>n(v)))
      .map(key=>{
        const old=archived.get(key);
        return {id:Number(key)||key,name:old?old.name:'Сотрудник удалён · №'+key,
          role:old&&old.role?old.role+' · удалён из реестра':'удалён из реестра',salary:null,gone:true,
          no_hikvision:!!(old&&old.no_hikvision)};
      });
    return byId(data.monthly||[],'id').concat(gone).map(person=>{
      const own=cellsById[String(person.id)]||{}, salary=n(person.salary), gone=!!person.gone;
      const ownOps=opsById?opsById[String(person.id)]||{}:null;
      let run=0;
      const cells=days.map(day=>{
        const amount=n(own[day]);
        run+=amount;
        return {day,amount,over:!gone&&amount>0&&run>salary,future:!!today&&day>today,today:day===today,
          ops:ownOps?(ownOps[day]||[]).map(op=>({id:op.id,amount:n(op.amount)})):null};
      });
      const paid=run, rest=salary-paid;
      if(gone)return {id:person.id,name:person.name,role:person.role,salary:null,paid,rest:null,
        over:false,closed:false,none:false,gone:true,noHik:!!person.no_hikvision,cells};
      return {id:person.id,name:person.name,role:person.role||'',salary,paid,rest,
        over:rest<0,closed:rest===0&&salary>0,none:paid===0,noHik:!!person.no_hikvision,cells};
    });
  }

  /* Прежняя сетка сменных (оставлена для совместимости и тестов). */
  function sheet(data){
    const days=data.days||[];
    return (data.shift||[]).map(person=>({
      name:person.name, group:person.group, rate:person.rate,
      accrued:person.accrued, paid:person.paid, debt:person.debt,
      cells:days.map(day=>{
        const cell=person.cells?person.cells[day]:null;
        return Object.assign({day,accrualId:cell?cell.accrual_id:null},cellState(cell));
      }),
    }));
  }

  /* Оклады выплачены — весь расход «Месячная заработная плата» за месяц с
     сервера (monthly_paid), включая записи без сотрудника: так же, как итог
     в XLSX. Старый ответ без monthly_paid — сумма ячеек. */
  function monthlyPaidTotal(data){
    const cells=Object.values(data.monthly_cells||{}).reduce((total,own)=>
      total+Object.values(own).reduce((a,v)=>a+n(v),0),0);
    return data.monthly_paid!=null?Math.max(n(data.monthly_paid),cells):cells;
  }

  /* Итоги месяца: сменные и оклады отдельно, как в карточках 2b. */
  function totals(data){
    const sum=key=>(data.shift||[]).reduce((total,person)=>total+n(person[key]),0);
    const accrued=sum('accrued'), paid=sum('paid'), debt=sum('debt');
    const people=(data.shift||[]).length;
    const owing=(data.shift||[]).filter(person=>n(person.debt)>0).length;
    const monthlyFund=n(data.monthly_total), monthlyPaid=monthlyPaidTotal(data);
    // Осталось выдать — сумма остатков по людям: переплата одному не
    // уменьшает то, что должны другим. Переплата — отдельным числом.
    let monthlyRest=0, monthlyOver=0;
    monthlyRows(data).forEach(m=>{
      if(m.gone)return;
      if(m.rest>0)monthlyRest+=m.rest; else monthlyOver+=-m.rest;
    });
    return {accrued,paid,debt,people,owing,monthlyPaid,monthlyFund,monthlyRest,monthlyOver,
      monthlyPeople:(data.monthly||[]).length};
  }

  /* Выдано из кассы в каждый день месяца: сменные (по дню выдачи) + части окладов. */
  function dayTotals(data){
    const perDay=data.paid_per_day||{}, monthly=data.monthly_cells||{};
    return (data.days||[]).map(day=>{
      let amount=n(perDay[day]);
      Object.values(monthly).forEach(own=>{amount+=n(own[day]);});
      return {day,amount};
    });
  }

  /* «Проверки за месяц»: ошибки (красные), предупреждения (жёлтые),
     напоминания (серые). Ключ ведёт к строке и дню в сетке. */
  const LEVEL={err:0,warn:1,todo:2};
  const BLOCKER_NOTE={missing_rate:'Нет ставки — укажите её в «Сотрудниках»',unlinked:'Нет привязки Hikvision — отметьте «был» вручную в «Финансах дня»',
    unavailable:'Данные Hikvision за день неполные',unknown:'Смена начислена не всем — ждёт начисления'};
  function checks(data,{today,pending}={}){
    const out=[], yesterday=today?addDays(today,-1):null;
    monthlyRows(data,{today}).forEach(m=>{
      if(m.over)out.push({lvl:'err',text:'Переплата оклада: '+m.name,
        sub:'Выдано '+fmt(m.paid)+' из '+fmt(m.salary),row:'m'+m.id,day:null});
    });
    shiftRows(data,{today,pending}).forEach(p=>{
      p.cells.forEach(c=>{
        if(c.kind==='nopass')out.push({lvl:'err',text:'Выдано без входа '+dm(c.day)+': '+p.name,
          sub:fmt(c.paid)+' сум',row:'s'+p.id,day:c.day});
        else if(c.kind==='unpaid'&&(!yesterday||c.day<yesterday))out.push({lvl:'warn',
          text:'Смена '+dm(c.day)+' не выдана: '+p.name,sub:'Ставка '+fmt(p.rate)+' сум',row:'s'+p.id,day:c.day});
        else if(c.kind==='blocked')out.push({lvl:'warn',text:'Не начислено '+dm(c.day)+': '+p.name,
          sub:BLOCKER_NOTE[c.blocker]||BLOCKER_NOTE.unknown,row:'s'+p.id,day:c.day});
        else if(c.kind==='odd')out.push({lvl:'warn',text:'Сумма ≠ ставке '+dm(c.day)+': '+p.name,
          sub:'Выдано '+fmt(c.paid)+' при ставке '+fmt(c.rate!=null?c.rate:p.rate),row:'s'+p.id,day:c.day});
      });
    });
    (data.negative_cash||[]).forEach(item=>{
      out.push({lvl:'err',text:'Остаток ушёл в минус '+dm(item.day),
        sub:'На конец дня '+(n(item.balance)<0?'−':'')+fmt(Math.abs(n(item.balance)))+' сум',row:null,day:item.day});
    });
    monthlyRows(data,{today}).forEach(m=>{
      if(m.none&&m.salary>0)out.push({lvl:'todo',text:'Нет выплат с начала месяца: '+m.name,
        sub:'Оклад '+fmt(m.salary)+' сум',row:'m'+m.id,day:null});
    });
    return out.sort((a,b)=>LEVEL[a.lvl]-LEVEL[b.lvl]);
  }

  function shiftMonth(month,step){
    const [year,index]=month.split('-').map(Number);
    const moved=new Date(Date.UTC(year,index-1+step,1));
    return moved.toISOString().slice(0,7);
  }

  /* Правка части оклада прямо в ячейке. Ячейка — итог выплат человеку за
     день: больше — доплачиваем разницу; меньше — уменьшаем последние выплаты
     (частично — правкой суммы, целиком — удалением); ноль — удаляем все.
     null — выплат в данных меньше, чем в ячейке (данные устарели). */
  function monthlyEditPlan(ops,current,target){
    const now=n(current), next=Math.max(0,n(target));
    if(next===now)return [];
    if(next>now)return [{action:'add',amount:next-now}];
    let excess=now-next;
    const plan=[];
    for(const op of (ops||[]).slice().reverse()){
      if(excess<=0)break;
      const amount=n(op.amount);
      if(amount<=excess){plan.push({action:'delete',id:op.id});excess-=amount;}
      else{plan.push({action:'update',id:op.id,amount:amount-excess});excess=0;}
    }
    return excess>0?null:plan;
  }

  /* Сумма из ячейки оклада: только цифры (пробелы-разделители можно).
     Пусто — 0 (очистить ячейку). Всё остальное — null: «1,5 млн», «abc» или
     «-5» не должны молча стать 15 сумами или удалением выплаты. */
  function parseAmount(raw){
    const text=String(raw==null?'':raw).replace(/[\s\u00a0\u202f]/g,'');
    if(text==='')return 0;
    return /^\d{1,12}$/.test(text)?Number(text):null;
  }

  /* Сколько выйдет сверх оклада, если ячейку исправить на next. 0 — не выйдет
     (или ячейку уменьшают: уменьшение переплату не создаёт). */
  function overpayAfter(person,cell,next){
    if(person.salary==null||n(next)<=n(cell.amount))return 0;
    return Math.max(0,n(person.paid)-n(cell.amount)+n(next)-n(person.salary));
  }

  /* Дата выплаты из сетки — сегодня: деньги уходят из кассы сегодня, и
     старая невыданная смена не должна менять кассу прошлого дня. В обычном
     случае (вчерашняя смена) это и есть «день смены + 1». Задним числом —
     только в «Финансах дня» с явной датой. Без today — день смены + 1. */
  function payday(day,today){
    return today||addDays(day,1);
  }

  /* День, в котором смену выдают по плану (смена + 1, не позже сегодня), —
     его и открывает номер дня в шапке: там блок «Смена {вчера}». */
  function shiftScreenDay(day,today){
    const next=addDays(day,1);
    return today&&next>today?today:next;
  }

  return {BLOCKER,cellState,gridCell,shiftRows,monthlyRows,monthlyEditPlan,parseAmount,overpayAfter,sheet,totals,dayTotals,checks,shiftMonth,payday,shiftScreenDay,addDays};
});
