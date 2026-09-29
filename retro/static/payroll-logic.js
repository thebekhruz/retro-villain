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

  /* Ячейка сменного в сетке 2b. Колонка — день смены, выдают на следующий
     день, поэтому «к выдаче» — только вчерашняя смена, а всё, что старше и не
     выдано, — «✕ смена не выдана».
       pending — вчерашняя смена, которую ещё не подтвердили в «Финансах дня»:
       начисления нет, есть только проход (из /staff за вчера). */
  function gridCell(cell,{rate,day,today,pending}={}){
    const yesterday=today?addDays(today,-1):null;
    const late=(cell&&cell.status==='late')||(!cell&&pending&&pending.status==='late');
    const base={day,late,text:'',payable:false,debt:0,accrualId:cell?cell.accrual_id:null};
    if(!cell){
      if(pending){
        if(ABSENT.has(pending.status))return {...base,kind:'missing',text:'н/я'};
        if(PRESENT.has(pending.status)&&n(pending.payable)>0)
          return {...base,kind:'pending',text:'к выдаче',debt:n(pending.payable)};
      }
      return {...base,kind:today&&day>=today?'future':'empty'};
    }
    const amount=n(cell.amount), paid=n(cell.paid), debt=n(cell.debt);
    const r=n(rate);
    if(ABSENT.has(cell.status)){
      if(paid>0)return {...base,kind:'nopass',text:'!',paid};
      return {...base,kind:'missing',text:'н/я'};
    }
    if(paid>0&&(paid!==r||debt>0))
      return {...base,kind:'odd',text:fmt(paid),paid,debt,payable:debt>0};
    if(paid>0)return {...base,kind:'paid',text:'✓',paid};
    if(debt>0){
      const fresh=yesterday&&day>=yesterday;
      return {...base,kind:fresh?'topay':'unpaid',text:fresh?'к выдаче':'✕',debt,payable:true};
    }
    if(amount===0)return {...base,kind:'empty'};
    return {...base,kind:'paid',text:'✓',paid};
  }

  /* Строки сменных для сетки. pending — {employee_id: строка /staff за вчера}. */
  function shiftRows(data,{today,pending}={}){
    const days=data.days||[], yesterday=today?addDays(today,-1):null;
    // Порядок — как в реестре (по номеру сотрудника), а не по алфавиту:
    // так строки стоят на тех же местах, что и в макете и в «Сотрудниках».
    return byId(data.shift||[],'employee_id').map(person=>{
      const id=person.employee_id, cells=person.cells||{};
      const wait=pending&&pending[id];
      let extra=0;
      const row=days.map(day=>{
        const cell=cells[day]||null;
        const item=gridCell(cell,{rate:person.rate,day,today,pending:day===yesterday&&!cell?wait:null});
        if(item.kind==='pending')extra+=item.debt;
        return item;
      });
      const manual=Object.values(cells).some(c=>String(c.status).startsWith('manual_'))
        ||(wait&&String(wait.status).startsWith('manual_'));
      const rest=n(person.debt)+extra;
      return {id,name:person.name,group:person.group||'',rate:n(person.rate),
        paid:n(person.paid),accrued:n(person.accrued),rest,noHik:!!manual,cells:row};
    });
  }

  /* Строки помесячных: оклад выдают частями, ячейка — сумма выплат за день.
     ops — сами выплаты дня (id движения и сумма): по ним ячейку можно
     уменьшить или очистить. Если сервер их не прислал — null. */
  function monthlyRows(data,{today}={}){
    const days=data.days||[], cellsById=data.monthly_cells||{}, opsById=data.monthly_cell_ops;
    return byId(data.monthly||[],'id').map(person=>{
      const own=cellsById[String(person.id)]||{}, salary=n(person.salary);
      const ownOps=opsById?opsById[String(person.id)]||{}:null;
      let run=0;
      const cells=days.map(day=>{
        const amount=n(own[day]);
        run+=amount;
        return {day,amount,over:amount>0&&run>salary,future:!!today&&day>today,today:day===today,
          ops:ownOps?(ownOps[day]||[]).map(op=>({id:op.id,amount:n(op.amount)})):null};
      });
      const paid=run, rest=salary-paid;
      return {id:person.id,name:person.name,role:person.role||'',salary,paid,rest,
        over:rest<0,closed:rest===0&&salary>0,none:paid===0,cells};
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

  function monthlyPaidTotal(data){
    const cells=data.monthly_cells;
    if(!cells)return n(data.monthly_paid);
    return Object.values(cells).reduce((total,own)=>
      total+Object.values(own).reduce((a,v)=>a+n(v),0),0);
  }

  /* Итоги месяца: сменные и оклады отдельно, как в карточках 2b. */
  function totals(data){
    const sum=key=>(data.shift||[]).reduce((total,person)=>total+n(person[key]),0);
    const accrued=sum('accrued'), paid=sum('paid'), debt=sum('debt');
    const people=(data.shift||[]).length;
    const owing=(data.shift||[]).filter(person=>n(person.debt)>0).length;
    const monthlyFund=n(data.monthly_total), monthlyPaid=monthlyPaidTotal(data);
    return {accrued,paid,debt,people,owing,monthlyPaid,monthlyFund,
      monthlyRest:Math.max(0,monthlyFund-monthlyPaid),
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
        else if(c.kind==='odd')out.push({lvl:'warn',text:'Сумма ≠ ставке '+dm(c.day)+': '+p.name,
          sub:'Выдано '+fmt(c.paid)+' при ставке '+fmt(p.rate),row:'s'+p.id,day:c.day});
      });
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

  /* День выдачи за смену: на следующий день, но не позже сегодняшнего. */
  function payday(day,today){
    const next=addDays(day,1);
    return today&&next>today?today:next;
  }

  return {cellState,gridCell,shiftRows,monthlyRows,monthlyEditPlan,sheet,totals,dayTotals,checks,shiftMonth,payday,addDays};
});
